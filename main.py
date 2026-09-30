"""
Entry point. Run hourly (via GitHub Actions cron, or manually):

    python main.py

Each run does two things in sequence:
  1. Retries every changeset sitting in the pending Overpass-recheck
     queue (from a previous run where Overpass was unavailable), using
     whatever Overpass availability exists right now.
  2. Processes at most one hour of new #tt_event changeset activity,
     worldwide -- see determine_window() for why this is capped.

Findings from both are appended to the same rotating CSV under data/.
Slack posting is wired in but stays a no-op until switched on in
config.py.
"""
import logging
from datetime import datetime, timedelta, timezone

import config
import fetch
import checks
import storage
import slack_notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# If fetching a pending changeset's own metadata fails this many times in a
# row (deleted, hidden, or persistently unreachable -- NOT an Overpass
# problem), give up on it rather than retrying forever. Overpass-unavailable
# retries, by contrast, are never capped -- they stay queued until Overpass
# genuinely answers.
MAX_META_FETCH_FAILURES = 20


def determine_window(state):
    """
    Every run processes AT MOST one hour of data, starting from wherever
    the last run left off -- never more, regardless of how far behind
    the schedule has fallen.

    Why this matters: if this simply ran from "last stop point" to "now"
    (as an earlier version did), a single missed scheduled trigger would
    make the next run's window balloon to cover the whole gap -- more
    changesets, more Overpass calls, a longer run, which makes THAT run
    more likely to overrun into the next scheduled slot, causing another
    missed run and an even bigger window next time. Capping the window
    to exactly one hour breaks that spiral: if there's backlog, this run
    only takes the oldest unprocessed hour and stops there; the next run
    picks up the following hour, and so on, catching up one clean,
    bounded hour at a time instead of swallowing the backlog in one go.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    if state.get("last_run_end_utc"):
        start = datetime.fromisoformat(state["last_run_end_utc"])
    else:
        start = now - timedelta(hours=1)
    end = min(start + timedelta(hours=1), now)
    return start, end


def retry_pending(pending_list):
    """
    Retries just the Overpass-dependent checks for changesets sitting in
    the queue, using an ID-only re-fetch of that changeset's metadata
    and diff. Returns (rows, still_pending).

    Two safeguards keep this bounded no matter how large the backlog
    grows:

    1. Only the first config.MAX_RETRY_PER_RUN items are attempted this
       run -- the rest are left untouched and simply stay queued for a
       later run. Without this, a backlog of thousands of items could
       make a single run take hours even when Overpass is perfectly
       healthy, starving the new hourly window of a chance to run.

    2. The moment the Overpass circuit breaker trips (see fetch.py),
       processing stops immediately for the rest of THIS batch. Every
       remaining item would just fail the same way and get re-queued
       anyway, so there's no point paying for their metadata/diff
       fetches once we already know Overpass is down for this run.
    """
    rows = []
    still_pending = []

    to_process = pending_list[:config.MAX_RETRY_PER_RUN]
    remainder = pending_list[config.MAX_RETRY_PER_RUN:]

    stopped_early_at = None
    for i, entry in enumerate(to_process):
        if fetch.overpass_circuit_is_open():
            stopped_early_at = i
            still_pending.extend(to_process[i:])
            break

        cs_id = entry["changeset_id"]
        cs_meta = fetch.fetch_changeset_meta(cs_id)
        if cs_meta is None:
            entry["meta_fetch_failures"] = entry.get("meta_fetch_failures", 0) + 1
            if entry["meta_fetch_failures"] < MAX_META_FETCH_FAILURES:
                still_pending.append(entry)
            else:
                log.warning(
                    "Giving up on changeset %s after %d failed metadata fetches "
                    "(likely deleted/hidden) -- dropping from the recheck queue",
                    cs_id, entry["meta_fetch_failures"],
                )
            continue

        try:
            diff = fetch.fetch_changeset_diff(cs_id)
            new_ways = [e for e in diff["create"] + diff["modify"] if e["type"] == "way"]
            overpass_issues, incomplete = checks.run_overpass_dependent_checks(cs_meta, new_ways, diff, fetch)
        except Exception:
            log.exception("Retry failed for pending changeset %s -- keeping it queued", cs_id)
            entry["overpass_attempts"] = entry.get("overpass_attempts", 0) + 1
            still_pending.append(entry)
            continue

        if incomplete:
            entry["overpass_attempts"] = entry.get("overpass_attempts", 0) + 1
            still_pending.append(entry)
        else:
            log.info(
                "Recheck succeeded for changeset %s (%s) after %d attempt(s): %d issue(s)",
                cs_id, cs_meta.get("user"), entry.get("overpass_attempts", 0) + 1, len(overpass_issues),
            )
            rows.extend(checks.to_row(cs_meta, issue) for issue in overpass_issues)

    if stopped_early_at is not None:
        log.warning(
            "Overpass circuit breaker tripped while processing the retry queue -- "
            "stopped after %d item(s), %d left untouched this run (still queued)",
            stopped_early_at, len(to_process) - stopped_early_at,
        )
    if remainder:
        log.info("Retry queue larger than the per-run cap (%d) -- %d item(s) deferred to a later run",
                  config.MAX_RETRY_PER_RUN, len(remainder))

    still_pending.extend(remainder)
    return rows, still_pending


def retry_pending_scans(pending_scans):
    """
    Retries time-ranges that a previous run couldn't scan for changesets
    because the underlying OSM API query itself persistently failed
    (see fetch_changesets_in_window's `unresolved` return value).

    Capped at config.MAX_SCAN_RETRY_PER_RUN per run -- same reasoning as
    the Overpass retry queue's cap: without one, enough stuck slices
    accumulating at once could make a single run slow, even though each
    individual slice is expected to be rare. Anything beyond the cap is
    left untouched and simply retried on a later run. Never capped on
    ATTEMPTS though: a range only leaves this queue once it's actually
    been scanned successfully, however many runs that takes.

    Returns (rows, still_pending, newly_pending_overpass) -- rows are
    findings from any newly discovered changesets in a range that
    finally resolved; newly_pending_overpass are changesets from those
    resolved ranges whose OWN Overpass-dependent checks couldn't
    complete and need to go into the separate Overpass retry queue.
    """
    rows = []
    still_pending = []
    newly_pending_overpass = []

    to_process = pending_scans[:config.MAX_SCAN_RETRY_PER_RUN]
    remainder = pending_scans[config.MAX_SCAN_RETRY_PER_RUN:]

    for entry in to_process:
        start_dt = datetime.fromisoformat(entry["start"])
        end_dt = datetime.fromisoformat(entry["end"])
        try:
            changesets, unresolved = fetch.fetch_changesets_in_window(start_dt, end_dt)
        except Exception:
            log.exception("Retry failed for pending scan range %s to %s -- keeping it queued",
                           entry["start"], entry["end"])
            entry["attempts"] = entry.get("attempts", 0) + 1
            still_pending.append(entry)
            continue

        if unresolved:
            entry["attempts"] = entry.get("attempts", 0) + 1
            still_pending.append(entry)
            continue

        log.info("Resolved pending scan range %s to %s after %d attempt(s): %d changeset(s) found",
                  entry["start"], entry["end"], entry.get("attempts", 0) + 1, len(changesets))
        for cs in changesets:
            try:
                cs_rows, incomplete = process_changeset(cs)
                rows.extend(cs_rows)
                if incomplete:
                    newly_pending_overpass.append({
                        "changeset_id": cs["id"],
                        "overpass_attempts": 1,
                        "meta_fetch_failures": 0,
                        "first_flagged_utc": datetime.now(timezone.utc).isoformat(),
                    })
            except Exception:
                log.exception("Failed processing changeset %s from a resolved scan range -- skipping it", cs.get("id"))

    if remainder:
        log.info("Scan-retry queue larger than the per-run cap (%d) -- %d range(s) deferred to a later run",
                  config.MAX_SCAN_RETRY_PER_RUN, len(remainder))

    still_pending.extend(remainder)
    return rows, still_pending, newly_pending_overpass


def process_changeset(cs_meta):
    diff = fetch.fetch_changeset_diff(cs_meta["id"])
    issues, incomplete = checks.run_all_checks(cs_meta, diff, fetch)
    rows = [checks.to_row(cs_meta, issue) for issue in issues]
    return rows, incomplete


def run():
    state = storage.load_state()
    pending = storage.load_pending_rechecks()
    pending_scans = storage.load_pending_scans()

    retry_rows, still_pending = retry_pending(pending)
    if pending:
        log.info("Retried %d pending changeset(s); %d still pending", len(pending), len(still_pending))

    scan_rows, still_pending_scans, scan_newly_pending_overpass = retry_pending_scans(pending_scans)
    if pending_scans:
        log.info("Retried %d pending scan range(s); %d still pending", len(pending_scans), len(still_pending_scans))
    still_pending.extend(scan_newly_pending_overpass)

    # Save retry-queue progress NOW, before attempting to scan new
    # changesets -- if that next step crashes (e.g. a transient OSM API
    # hiccup), the real work already completed resolving retry-queue
    # items this run must not be thrown away along with it.
    retry_path, n_retry_written = storage.append_issues(state, retry_rows + scan_rows)
    storage.save_state(state)
    storage.save_pending_rechecks(still_pending)
    storage.save_pending_scans(still_pending_scans)
    if n_retry_written:
        log.info("Wrote %d issue row(s) from the retry queues to %s", n_retry_written, retry_path)

    start, end = determine_window(state)
    log.info("Scanning #%s changesets worldwide from %s to %s UTC", config.HASHTAG, start, end)

    changesets, unresolved = fetch.fetch_changesets_in_window(start, end)
    log.info("Found %d candidate changeset(s)", len(changesets))

    all_issues = []
    newly_pending = list(still_pending)
    total = len(changesets)
    for idx, cs in enumerate(changesets, start=1):
        try:
            rows, incomplete = process_changeset(cs)
            all_issues.extend(rows)
            if incomplete:
                newly_pending.append({
                    "changeset_id": cs["id"],
                    "overpass_attempts": 1,
                    "meta_fetch_failures": 0,
                    "first_flagged_utc": datetime.now(timezone.utc).isoformat(),
                })
                log.info("[%d/%d] Changeset %s (%s): Overpass unavailable, queued for retry",
                         idx, total, cs["id"], cs.get("user"))
            else:
                log.info("[%d/%d] Changeset %s (%s): %d issue(s)",
                         idx, total, cs["id"], cs.get("user"), len(rows))
        except Exception:
            log.exception("[%d/%d] Failed processing changeset %s -- skipping it, continuing with the rest",
                           idx, total, cs.get("id"))

    newly_pending_scans = list(still_pending_scans)
    if unresolved:
        for u_start, u_end in unresolved:
            newly_pending_scans.append({
                "start": u_start, "end": u_end, "attempts": 1,
                "first_flagged_utc": datetime.now(timezone.utc).isoformat(),
            })
        log.warning(
            "%d changeset-discovery slice(s) could not be scanned this run and were queued for "
            "retry -- the window still advances normally; those slices will be re-checked until "
            "they succeed, so nothing in them is silently skipped.",
            len(unresolved),
        )

    path, n_written = storage.append_issues(state, all_issues)
    # The window advances regardless of whether any slice was left
    # unresolved -- that's the fix. An unresolved slice is tracked
    # separately (above) and retried independently until it succeeds;
    # it must never be allowed to block all future progress the way it
    # did before this was added.
    state["last_run_end_utc"] = end.isoformat()
    storage.save_state(state)
    storage.save_pending_rechecks(newly_pending)
    storage.save_pending_scans(newly_pending_scans)

    log.info("Wrote %d issue row(s) to %s (%d changeset(s) pending Overpass recheck, %d scan range(s) pending)",
              n_written, path, len(newly_pending), len(newly_pending_scans))

    slack_notify.post_summary(start, end, retry_rows + scan_rows + all_issues, csv_path=path)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        # By this point, retry-queue progress has already been checkpointed
        # to disk (see the save immediately after retry_pending() above),
        # and if this failure happened before the new window was ever
        # determined, last_run_end_utc was never advanced -- so the next
        # run will simply retry the exact same window from scratch.
        # Nothing is lost either way. Logging and exiting cleanly (rather
        # than crashing with a non-zero exit code) avoids marking every
        # brief external hiccup as a failed Action run when the system is
        # already designed to absorb it gracefully.
        log.exception("Run did not complete due to an unexpected error -- "
                       "already-saved progress is safe; the next run will retry what's left")
