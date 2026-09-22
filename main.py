"""
Entry point. Run hourly (via GitHub Actions cron, or manually):

    python main.py

Each run processes AT MOST one hour of #tt_event changeset activity,
worldwide -- see the docstring on determine_window() for why this is
capped rather than open-ended. Findings are appended to a rotating CSV
under data/. Slack posting is wired in but stays a no-op until it's
switched on in config.py.
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


def process_changeset(cs_meta):
    cs_id = cs_meta["id"]
    diff = fetch.fetch_changeset_diff(cs_id)
    issues = checks.run_all_checks(cs_meta, diff, fetch)
    return [checks.to_row(cs_meta, issue) for issue in issues]


def run():
    state = storage.load_state()
    start, end = determine_window(state)
    log.info("Scanning #%s changesets worldwide from %s to %s UTC", config.HASHTAG, start, end)

    changesets = fetch.fetch_changesets_in_window(start, end)
    log.info("Found %d candidate changeset(s)", len(changesets))

    all_issues = []
    for cs in changesets:
        try:
            rows = process_changeset(cs)
            all_issues.extend(rows)
            log.info("Changeset %s (%s): %d issue(s)", cs["id"], cs.get("user"), len(rows))
        except Exception:
            log.exception("Failed processing changeset %s -- skipping it, continuing with the rest", cs.get("id"))

    path, n_written = storage.append_issues(state, all_issues)
    state["last_run_end_utc"] = end.isoformat()
    storage.save_state(state)

    log.info("Wrote %d issue row(s) to %s", n_written, path)

    slack_notify.post_summary(start, end, all_issues, csv_path=path)


if __name__ == "__main__":
    run()
