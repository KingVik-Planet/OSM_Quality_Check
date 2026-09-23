"""
CSV storage with automatic rotation once a file approaches the 40MB
limit (quality_check_1.csv -> quality_check_2.csv -> ...), a tiny state
file that remembers the UTC timestamp of the last successful run so
every run picks up exactly where the previous one left off, and a
pending-recheck queue for changesets whose Overpass-dependent checks
couldn't complete (outage/circuit breaker) and need to be retried on a
later run rather than silently treated as "checked, nothing found".
"""
import csv
import json
import os

import config

FIELDNAMES = [
    "s_no", "error_type", "username", "user_id", "osm_location_link",
    "changeset_id", "changeset_link", "osm_object_type", "osm_object_id",
    "time_utc", "country", "detail",
]

PENDING_RECHECK_FILE = os.path.join(config.DATA_DIR, "pending_overpass_recheck.json")


def ensure_data_dir():
    os.makedirs(config.DATA_DIR, exist_ok=True)


def load_state():
    ensure_data_dir()
    if not os.path.exists(config.STATE_FILE):
        return {"last_run_end_utc": None, "current_csv_index": 1, "next_s_no": 1}
    with open(config.STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    ensure_data_dir()
    with open(config.STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_pending_rechecks():
    """
    Returns the list of changesets waiting for their Overpass-dependent
    checks to be retried, each as {"changeset_id", "overpass_attempts",
    "meta_fetch_failures", "first_flagged_utc"}. Empty list if the file
    doesn't exist yet (nothing pending) or is unreadable.
    """
    ensure_data_dir()
    if not os.path.exists(PENDING_RECHECK_FILE):
        return []
    try:
        with open(PENDING_RECHECK_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_pending_rechecks(pending_list):
    ensure_data_dir()
    with open(PENDING_RECHECK_FILE, "w") as f:
        json.dump(pending_list, f, indent=2)


def _csv_path(index):
    return os.path.join(config.DATA_DIR, f"{config.CSV_BASENAME}_{index}.csv")


def _current_csv_path(state):
    path = _csv_path(state["current_csv_index"])
    if os.path.exists(path) and os.path.getsize(path) >= config.CSV_MAX_BYTES:
        state["current_csv_index"] += 1
        path = _csv_path(state["current_csv_index"])
    return path


def append_issues(state, issues):
    """
    issues: list of dicts matching FIELDNAMES minus 's_no' (assigned
    here so numbering stays continuous across files and across runs).
    Returns (path_written, rows_written). Writing an empty list still
    returns (None, 0) without touching any file.
    """
    if not issues:
        return None, 0
    ensure_data_dir()
    path = _current_csv_path(state)
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if is_new:
            writer.writeheader()
        for row in issues:
            row = dict(row)
            row["s_no"] = state["next_s_no"]
            state["next_s_no"] += 1
            writer.writerow(row)
    return path, len(issues)
