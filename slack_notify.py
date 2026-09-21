"""
Slack posting for the hourly summary.

INTENTIONALLY NOT WIRED IN YET: main.py imports and calls
post_summary() every run, but it's a silent no-op until
config.SLACK_ENABLED is turned on (env var QC_SLACK_ENABLED=1) and
SLACK_BOT_TOKEN / SLACK_CHANNEL_ID are set. That way, turning Slack on
later is a one-line config change, not a code change.
"""
from collections import Counter

import requests

import config


def build_summary_text(window_start, window_end, issues):
    counts = Counter(i["error_type"] for i in issues)
    lines = [
        f"*OSM #{config.HASHTAG} quality check* — "
        f"{window_start:%Y-%m-%d %H:%M} to {window_end:%H:%M} UTC",
        f"Total issues found: *{len(issues)}*",
    ]
    for error_type, n in counts.most_common():
        lines.append(f"• {error_type}: {n}")
    if not issues:
        lines.append("No issues found in this window. ✅")
    return "\n".join(lines)


def post_summary(window_start, window_end, issues, csv_path=None):
    if not config.SLACK_ENABLED:
        return
    if not config.SLACK_BOT_TOKEN or not config.SLACK_CHANNEL_ID:
        return

    text = build_summary_text(window_start, window_end, issues)
    if csv_path:
        text += f"\nFull detail written to: `{csv_path}`"

    requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
        json={"channel": config.SLACK_CHANNEL_ID, "text": text},
        timeout=30,
    )
