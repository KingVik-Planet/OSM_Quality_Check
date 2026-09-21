# OSM #tt_event hourly quality check

Scans OpenStreetMap worldwide, once an hour, for changesets tagged
`#tt_event`, and flags a set of common quality problems into a
rotating CSV under `data/`. Slack posting is fully coded but disabled
until you switch it on (see the bottom of this file) — this is
intentional, per the current stage of the project.

## What it checks

**Changeset-level**
- Mass create / modify / delete (configurable threshold, default 10,000 objects)
- Mass delete with no revert-tool signature in `created_by`/comment (i.e. likely not an intentional revert)
- Unclear / empty / generic changeset comment

**Tagging**
- Feature mapped with tags but no recognised primary tag
- Untagged way
- "Wrong tagging" — a value that belongs to one key showing up under another
  (e.g. `highway=building`, `area=building`, `name=building`)

**Geometry** (checked against both other objects in the same upload
and existing nearby OSM data pulled from Overpass)
- Overlapping buildings
- Building inside building
- Crossing buildings
- Duplicated node
- Duplicated way
- Crossing ways / crossing highways
- Overlapping highway (two ways running along the same alignment)
- Node connecting a highway to a building
- Way end-node near another way (undershoot/overshoot)

See the docstring at the top of `checks.py` for the honesty note on
scope: these are pragmatic v1 rules, not a full JOSM-validator
replacement, and the thresholds in `config.py` are meant to be tuned
once you see real results.

## Project layout

```
osm_quality_check/
├── config.py          # all thresholds, whitelists, env-driven settings
├── geo_utils.py        # geometry math + OSM link builders
├── geocode.py           # cached reverse geocoding -> country
├── fetch.py             # talks to the OSM API, Overpass, osmcha
├── checks.py            # the rule engine
├── storage.py           # rotating CSV + run-state
├── slack_notify.py       # ready, but not called on by default yet
├── main.py               # entry point: one hourly run
├── requirements.txt
├── data/                 # quality_check_1.csv, quality_check_2.csv, ..., state.json
└── .github/workflows/hourly.yml
```

## CSV columns

`s_no, error_type, username, user_id, osm_location_link, changeset_id,
changeset_link, osm_object_type, osm_object_id, time_utc, country,
detail`

Numbering (`s_no`) is continuous across files and across hourly runs.
Each file is capped at 40MB; once a file would exceed that, a new one
starts (`quality_check_2.csv`, `_3`, ...). `data/state.json` tracks
which file is currently active and the UTC end-time of the last run,
so each run picks up exactly where the last one left off — if it runs
at 2pm it reports on 1pm–2pm, at 3pm it reports on 2pm–3pm, and so on,
even if a run is occasionally missed.

## Running locally (PyCharm or terminal)

```bash
cd osm_quality_check
pip install -r requirements.txt

# optional, for osmcha enrichment (mass-edit / suspicion flags)
export OSMCHA_TOKEN=your_token_here

python main.py
```

First run with no `data/state.json` present scans the last 1 hour
from "now". Every subsequent run continues from where it left off.

## Running automatically: GitHub Actions

1. Push this whole `osm_quality_check/` folder (including the
   `.github/workflows/hourly.yml` file — note `.github` must be at
   your **repo root**, not nested inside `osm_quality_check/`, or move
   its contents up when you push) to a GitHub repo.
2. Repo → Settings → Secrets and variables → Actions → add:
   - `OSMCHA_TOKEN` (optional, for enrichment)
   - `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` (ready for when Slack is switched on)
3. That's it — it runs every hour on the hour and commits new CSV rows
   back into `osm_quality_check/data/` automatically. You can also
   trigger a run manually from the Actions tab (workflow_dispatch).

## Turning Slack posting on later

Nothing to rewrite. In the workflow env (or your local shell), set:

```
QC_SLACK_ENABLED=1
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=C0123456789
```

`slack_notify.post_summary()` is already called at the end of every
run in `main.py` — it just silently does nothing while `QC_SLACK_ENABLED`
is unset/false.

## Dashboard reuse

Since `data/quality_check_*.csv` is committed straight to the repo,
your dashboard can simply read the latest file(s) via the repo's raw
GitHub URL on an hourly refresh, in lockstep with the workflow —
no separate database needed.

## Known limitations to be aware of

- The OSM `/changesets` endpoint has no native hashtag filter, so this
  pages through changesets in the time window and filters client-side
  by hashtag; very high global edit volume in a given hour could mean
  more than a couple of pages, which is handled, but is worth knowing
  about if a run ever looks slow.
- Overpass calls are best-effort with a fallback mirror; if both are
  down for an hour, geometry-context checks for that hour are skipped
  (logged, not silently lost — check the run log).
- Nominatim reverse geocoding is rate-limited to ~1 request/second per
  its usage policy; this is fine at expected #tt_event volumes but
  would need a self-hosted Nominatim if volume grows dramatically.
