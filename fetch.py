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

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "osm-tt-event-quality-check/1.0"}


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


def fetch_changeset_diff(changeset_id):
    """
    Downloads and parses the osmChange for a changeset. Returns:
    {"create": [...], "modify": [...], "delete": [...]}
    where each element is a dict with at least "type" and "id", plus
    "tags", "lat"/"lon" for nodes, and "nodes" (ordered ref list) for ways.
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
                item["lat"] = float(el.get("lat"))
                item["lon"] = float(el.get("lon"))
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
    that weren't themselves edited in this changeset.
    Returns {node_id: (lon, lat)}.
    """
    coords = {}
    missing = []
    for nid in node_ids:
        n = changeset_nodes.get(nid)
        if n is not None:
            coords[nid] = (n["lon"], n["lat"])
        else:
            missing.append(nid)

    for i in range(0, len(missing), 700):  # OSM API batch-friendly chunk size
        batch = missing[i:i + 700]
        try:
            resp = _get(f"{config.OSM_API_BASE}/nodes.json", params={"nodes": ",".join(map(str, batch))})
            for el in resp.json().get("elements", []):
                coords[el["id"]] = (el["lon"], el["lat"])
        except requests.RequestException as e:
            log.warning("Could not resolve %d node coords: %s", len(batch), e)
    return coords


def fetch_overpass_context(min_lat, min_lon, max_lat, max_lon, exclude_way_ids, exclude_node_ids):
    """
    Pulls existing buildings/highways near a changeset's bounding box so
    new edits can be checked against surrounding, previously-mapped
    geometry -- not just against other objects in the same upload.
    Returns (ways: [{"id","nodes","tags"}], nodes: {id: (lon, lat)}).
    Fails soft (returns empty) if every Overpass mirror is unreachable.
    """
    buf_deg = config.OVERPASS_CONTEXT_BUFFER_M / 111000  # rough metres->degrees
    s, w, n, e = min_lat - buf_deg, min_lon - buf_deg, max_lat + buf_deg, max_lon + buf_deg
    query = f"""
    [out:json][timeout:60];
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
        try:
            resp = requests.post(endpoint, data={"data": query}, headers=HEADERS, timeout=90)
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException as e:
            last_err = e
            continue

    if data is None:
        log.warning("All Overpass endpoints failed, skipping context for this changeset: %s", last_err)
        return [], {}

    nodes, ways = {}, []
    for el in data.get("elements", []):
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])
        elif el["type"] == "way" and el["id"] not in exclude_way_ids:
            ways.append({"id": el["id"], "nodes": el.get("nodes", []), "tags": el.get("tags", {})})
    return ways, nodes


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
