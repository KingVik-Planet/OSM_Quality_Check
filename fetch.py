"""
Talks to the OSM API, Overpass, and (optionally) osmcha to find
#tt_event changesets in a given UTC time window, worldwide, and pull
down the data needed to run checks on them.
"""
import logging
from datetime import datetime

import requests
import xml.etree.ElementTree as ET

import config
import geo_utils

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "osm-tt-event-quality-check/1.0"}


class OverpassUnavailable(Exception):
    """
    Raised when Overpass context could not be fetched -- either every
    mirror failed every retry, or the circuit breaker is currently open.
    Distinct from "Overpass answered and there's genuinely nothing
    nearby" (which returns an empty result normally, not an exception),
    so callers can tell the difference between "no context exists" and
    "we couldn't find out" -- the latter needs to be retried later, the
    former is a final, correct answer.
    """
    pass


# --- Overpass circuit breaker -----------------------------------------------
# If Overpass fails completely (every mirror, every retry) this many times
# IN A ROW within a single run, stop calling it entirely for the rest of
# that run and skip straight to raising OverpassUnavailable for every
# remaining changeset. Without this, a sustained Overpass outage (which
# does happen -- sometimes for an hour or more) means every single
# changeset in the run pays the full multi-minute, multi-mirror retry
# cost for nothing. This resets automatically every run, since each run
# is a fresh process -- so a recovered Overpass gets a fair fresh
# attempt again next time, no persistence needed.
_OVERPASS_CIRCUIT_THRESHOLD = 2
_overpass_consecutive_failures = 0


def _get(url, params=None, headers=None, timeout=60):
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    resp = requests.get(url, params=params, headers=h, timeout=timeout)
    resp.raise_for_status()
    return resp


def _parse_osm_dt(s):
    return datetime.strptime(s.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")


def fetch_changesets_in_window(start_dt, end_dt):
    """
    Returns a list of changeset dicts (id, uid, user, created_at,
    closed_at, min_lat/lon, max_lat/lon, tags{}) for every changeset
    worldwide that intersects [start_dt, end_dt) UTC and mentions the
    configured hashtag.

    The OSM API's /changesets endpoint returns at most 100 results per
    call and has no native hashtag filter, so this pages backwards
    through the window using the `time` parameter and filters
    client-side against the changeset's `comment` and `hashtags` tags.
    """
    found = {}
    cursor_end = end_dt
    hashtag_needle = f"#{config.HASHTAG}".lower()

    while True:
        params = {
            "time": f"{start_dt.isoformat()}Z,{cursor_end.isoformat()}Z",
            "closed": "true",
        }
        resp = _get(f"{config.OSM_API_BASE}/changesets.json", params=params)
        data = resp.json().get("changesets", [])
        if not data:
            break

        for cs in data:
            tags = cs.get("tags", {})
            haystack = " ".join([tags.get("comment", ""), tags.get("hashtags", "")]).lower()
            if hashtag_needle in haystack:
                found[cs["id"]] = cs

        if len(data) < 100:
            break  # reached the last page

        oldest_seen = min((c.get("closed_at") or c.get("created_at")) for c in data)
        new_cursor_end = _parse_osm_dt(oldest_seen)
        if new_cursor_end <= start_dt or new_cursor_end >= cursor_end:
            break
        cursor_end = new_cursor_end

    return list(found.values())


def fetch_changeset_meta(changeset_id):
    """
    Fetches a single changeset's own metadata (user, bbox, tags, etc.)
    directly by ID. Used when retrying a changeset from the pending
    Overpass-recheck queue, since fetch_changesets_in_window only
    searches within a specific time window and won't find an older
    changeset on demand by ID.
    Returns None if the changeset can't be fetched (deleted, hidden,
    network error) -- callers should treat that as a separate, distinct
    failure from "Overpass is down" (see OverpassUnavailable).
    """
    try:
        resp = _get(f"{config.OSM_API_BASE}/changeset/{changeset_id}.json")
        return resp.json().get("changeset")
    except requests.RequestException as e:
        log.warning("Could not fetch metadata for pending changeset %s: %s", changeset_id, e)
        return None


def fetch_changeset_diff(changeset_id):
    """
    Downloads and parses the osmChange for a changeset. Returns:
    {"create": [...], "modify": [...], "delete": [...]}
    where each element is a dict with at least "type" and "id", plus
    "tags", "nodes" (ordered ref list) for ways.

    For nodes, "lat"/"lon" are set to None (not skipped) if the OSM API
    didn't include coordinate attributes for that element -- this does
    happen in practice for some deleted/historic elements -- so callers
    must treat None as "coordinates unknown" rather than assume every
    node dict has usable numbers.
    """
    resp = _get(f"{config.OSM_API_BASE}/changeset/{changeset_id}/download")
    root = ET.fromstring(resp.content)
    out = {"create": [], "modify": [], "delete": []}
    for action in root:
        if action.tag not in out:
            continue
        for el in action:
            if el.tag not in ("node", "way", "relation"):
                continue
            item = {"type": el.tag, "id": int(el.get("id"))}
            if el.tag == "node":
                lat_str, lon_str = el.get("lat"), el.get("lon")
                item["lat"] = float(lat_str) if lat_str is not None else None
                item["lon"] = float(lon_str) if lon_str is not None else None
            if el.tag == "way":
                item["nodes"] = [int(nd.get("ref")) for nd in el.findall("nd")]
            item["tags"] = {t.get("k"): t.get("v") for t in el.findall("tag")}
            out[action.tag].append(item)
    return out


def fetch_node_coords(node_ids, changeset_nodes):
    """
    Resolves lon/lat for a set of node ids, preferring nodes already
    present in the same changeset diff (changeset_nodes: id -> node dict
    with lat/lon), falling back to the live OSM API for referenced nodes
    that weren't themselves edited in this changeset -- or whose diff
    entry had no usable coordinates.
    Returns {node_id: (lon, lat)}.
    """
    coords = {}
    missing = []
    for nid in node_ids:
        n = changeset_nodes.get(nid)
        if n is not None and n.get("lat") is not None and n.get("lon") is not None:
            coords[nid] = (n["lon"], n["lat"])
        else:
            missing.append(nid)

    for i in range(0, len(missing), 700):  # OSM API batch-friendly chunk size
        batch = missing[i:i + 700]
        try:
            resp = _get(f"{config.OSM_API_BASE}/nodes.json", params={"nodes": ",".join(map(str, batch))})
            for el in resp.json().get("elements", []):
                lon, lat = el.get("lon"), el.get("lat")
                if lon is not None and lat is not None:
                    coords[el["id"]] = (lon, lat)
                # else: a redacted/hidden node with no coordinates -- leave it
                # out of coords; build_way_geometry already treats a missing
                # node as "can't build this way's geometry" and returns None
                # rather than crashing, so the rest of this changeset's
                # checks still run normally.
        except requests.RequestException as e:
            log.warning("Could not resolve %d node coords: %s", len(batch), e)
    return coords


def fetch_overpass_context(min_lat, min_lon, max_lat, max_lon, exclude_way_ids, exclude_node_ids):
    """
    Pulls existing buildings/highways near a changeset's bounding box so
    new edits can be checked against surrounding, previously-mapped
    geometry -- not just against other objects in the same upload.
    Returns (ways: [{"id","nodes","tags"}], nodes: {id: (lon, lat)}) on
    success (which may legitimately be empty if nothing is nearby).

    Raises OverpassUnavailable if every mirror failed every retry, or if
    the circuit breaker is currently open -- callers must catch this and
    treat it as "unknown, try again later", never as "confirmed empty".
    """
    global _overpass_consecutive_failures

    if _overpass_consecutive_failures >= _OVERPASS_CIRCUIT_THRESHOLD:
        log.info(
            "Overpass circuit breaker open (%d consecutive full failures this run) "
            "-- skipping context fetch instantly instead of retrying",
            _overpass_consecutive_failures,
        )
        raise OverpassUnavailable("circuit breaker open")

    buf_deg = config.OVERPASS_CONTEXT_BUFFER_M / 111000  # rough metres->degrees
    s, w, n, e = min_lat - buf_deg, min_lon - buf_deg, max_lat + buf_deg, max_lon + buf_deg
    query = f"""
    [out:json][timeout:{config.OVERPASS_QUERY_TIMEOUT_S}];
    (
      way["building"]({s},{w},{n},{e});
      way["highway"]({s},{w},{n},{e});
    );
    out body;
    >;
    out skel qt;
    """

    data = None
    last_err = None
    for endpoint in config.OVERPASS_ENDPOINTS:
        for attempt in range(1, config.OVERPASS_RETRIES + 1):
            try:
                resp = requests.post(
                    endpoint, data={"data": query}, headers=HEADERS,
                    timeout=config.OVERPASS_HTTP_TIMEOUT_S,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.RequestException as e:
                last_err = e
                log.info("Overpass %s attempt %d/%d failed: %s",
                         endpoint, attempt, config.OVERPASS_RETRIES, e)
        if data is not None:
            break

    if data is None:
        _overpass_consecutive_failures += 1
        log.warning(
            "All Overpass endpoints failed after retries (%d consecutive full "
            "failures this run): %s", _overpass_consecutive_failures, last_err,
        )
        raise OverpassUnavailable(str(last_err))

    _overpass_consecutive_failures = 0  # a mirror answered -- reset the breaker
    nodes, ways = {}, []
    for el in data.get("elements", []):
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])
        elif el["type"] == "way" and el["id"] not in exclude_way_ids:
            ways.append({"id": el["id"], "nodes": el.get("nodes", []), "tags": el.get("tags", {})})
    return ways, nodes


def fetch_live_way_geometry(way_id):
    """
    Re-fetches a way's CURRENT geometry directly from the live OSM API --
    not Overpass, which can lag a few minutes behind the live database.

    Used as a double-check before actually flagging a candidate overlap
    or crossing against a way that came from Overpass context (i.e. an
    existing feature, not something touched in this changeset). Without
    this, a mapper who fixes an overlap by editing one building in an
    early changeset and a neighbouring building in the very next one can
    get a false positive: the second changeset's new geometry looks
    fine, but Overpass's copy of the first building hasn't caught up
    with the fix yet, so a genuinely-resolved overlap still shows up as
    an issue in our comparison.

    Returns None on any failure (way deleted, network error, etc.) --
    callers should treat None as "couldn't verify, fall back to the
    original context result" rather than as proof the overlap is real.
    """
    try:
        resp = _get(f"{config.OSM_API_BASE}/way/{way_id}/full.json")
        elements = resp.json().get("elements", [])
        way = next((e for e in elements if e["type"] == "way" and e["id"] == way_id), None)
        if way is None:
            return None
        node_coords = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
        return geo_utils.build_way_geometry(way.get("nodes", []), node_coords, way.get("tags"))
    except Exception as e:
        log.info("Live re-check of way %s failed (non-fatal, keeping original result): %s", way_id, e)
        return None


def fetch_osmcha_flags(changeset_id):
    """
    Best-effort enrichment from osmcha (e.g. whether it's already been
    reviewed, extra suspicion reasons). Returns {} if OSMCHA_TOKEN isn't
    set or the request fails -- this is enrichment only. The mass-edit
    and revert checks in checks.py are computed directly from the OSM
    changeset metadata either way, so nothing depends on this succeeding.
    """
    if not config.OSMCHA_TOKEN:
        return {}
    try:
        resp = _get(
            f"{config.OSMCHA_API_BASE}/changesets/{changeset_id}/",
            headers={"Authorization": f"Token {config.OSMCHA_TOKEN}"},
        )
        return resp.json()
    except requests.RequestException as e:
        log.info("osmcha lookup failed for %s (non-fatal): %s", changeset_id, e)
        return {}
