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
    Retries just the Overpass-dependent checks for every changeset
    sitting in the queue, using an ID-only re-fetch of that changeset's
    metadata and diff. Returns (rows, still_pending) -- entries that
    succeed are dropped from the queue; entries that fail again (still
    Overpass-unavailable) stay queued indefinitely; entries whose
    changeset metadata itself can't be fetched are dropped after
    MAX_META_FETCH_FAILURES attempts, since that's a "this changeset is
    gone" problem, not an "Overpass is down" problem.
    """
    rows = []
    still_pending = []

    for entry in pending_list:
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

    return rows, still_pending


def process_changeset(cs_meta):
    diff = fetch.fetch_changeset_diff(cs_meta["id"])
    issues, incomplete = checks.run_all_checks(cs_meta, diff, fetch)
    rows = [checks.to_row(cs_meta, issue) for issue in issues]
    return rows, incomplete


def run():
    state = storage.load_state()
    pending = storage.load_pending_rechecks()

    retry_rows, still_pending = retry_pending(pending)
    if pending:
        log.info("Retried %d pending changeset(s); %d still pending", len(pending), len(still_pending))

    start, end = determine_window(state)
    log.info("Scanning #%s changesets worldwide from %s to %s UTC", config.HASHTAG, start, end)

    changesets = fetch.fetch_changesets_in_window(start, end)
    log.info("Found %d candidate changeset(s)", len(changesets))

    all_issues = list(retry_rows)
    newly_pending = still_pending
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

    path, n_written = storage.append_issues(state, all_issues)
    state["last_run_end_utc"] = end.isoformat()
    storage.save_state(state)
    storage.save_pending_rechecks(newly_pending)

    log.info("Wrote %d issue row(s) to %s (%d changeset(s) still pending Overpass recheck)",
              n_written, path, len(newly_pending))

    slack_notify.post_summary(start, end, all_issues, csv_path=path)


if __name__ == "__main__":
    run()
