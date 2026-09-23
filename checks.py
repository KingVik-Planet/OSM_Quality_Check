"""
Rule engine: turns a changeset's metadata + osmChange diff + nearby
existing geometry into a list of Issue objects, one per problem found.

Honesty note: this is a first, pragmatic version of each geometry check
-- not a full replacement for JOSM's validator or Osmose, which are
mature tools built over years. It's tuned to catch the categories you
asked for with a low false-positive rate, using thresholds in
config.py that can be adjusted as you see real-world results.

Structure note: checks that need Overpass context (comparing a new
object against pre-existing map data) are split into
run_overpass_dependent_checks() so they can be retried on their own
later if Overpass was unavailable, without re-running -- and
duplicating -- the checks that don't need it and already succeeded.
"""
from dataclasses import dataclass
from shapely.geometry import Point, LineString, Polygon
from shapely.strtree import STRtree

import config
import geo_utils
import geocode
from fetch import OverpassUnavailable


@dataclass
class Issue:
    issue_type: str
    osm_type: str
    osm_id: int
    lat: float
    lon: float
    detail: str = ""


# ---------------------------------------------------------------------------
# Changeset-level checks (no geometry needed)
# ---------------------------------------------------------------------------

def check_mass_edit_and_revert(cs_meta, diff):
    issues = []
    n_create, n_modify, n_delete = len(diff["create"]), len(diff["modify"]), len(diff["delete"])
    tags = cs_meta.get("tags", {})
    created_by = tags.get("created_by", "")
    comment = tags.get("comment", "")
    looks_like_revert = any(
        sig.lower() in created_by.lower() or sig.lower() in comment.lower()
        for sig in config.REVERT_SIGNATURES
    )
    lat, lon = _changeset_centroid(cs_meta)

    if n_delete >= config.MASS_DELETE_THRESHOLD and not looks_like_revert:
        issues.append(Issue("mass delete without revert tag", "changeset", cs_meta["id"], lat, lon,
                             detail=f"{n_delete} objects deleted, no revert signature found"))
    if n_create >= config.MASS_CREATE_THRESHOLD:
        issues.append(Issue("mass upload (create)", "changeset", cs_meta["id"], lat, lon,
                             detail=f"{n_create} objects created"))
    if n_modify >= config.MASS_MODIFY_THRESHOLD:
        issues.append(Issue("mass upload (modify)", "changeset", cs_meta["id"], lat, lon,
                             detail=f"{n_modify} objects modified"))
    return issues


def check_comment_quality(cs_meta):
    tags = cs_meta.get("tags", {})
    comment = tags.get("comment", "").strip()
    lat, lon = _changeset_centroid(cs_meta)
    generic = {"mapping", "edit", "test", "update", "changes", "fix", "map", "editing"}

    if not comment:
        detail = "changeset comment is empty"
    elif len(comment) < 10:
        detail = f"comment too short to be informative: '{comment}'"
    elif comment.strip().lower() in generic:
        detail = f"generic, uninformative comment: '{comment}'"
    else:
        return []

    return [Issue("unclear changeset comment", "changeset", cs_meta["id"], lat, lon, detail=detail)]


def _changeset_centroid(cs_meta):
    try:
        return (cs_meta["min_lat"] + cs_meta["max_lat"]) / 2, (cs_meta["min_lon"] + cs_meta["max_lon"]) / 2
    except (KeyError, TypeError):
        return None, None


# ---------------------------------------------------------------------------
# Tagging checks
# ---------------------------------------------------------------------------

def check_untagged_and_missing_primary(elements):
    issues = []
    for el in elements:
        tags = el.get("tags", {})
        lat, lon = _element_point(el)
        if lat is None:
            continue
        if not tags:
            if el["type"] == "way":
                issues.append(Issue("untagged way", "way", el["id"], lat, lon))
            continue
        if not (set(tags) & config.PRIMARY_TAG_KEYS):
            issues.append(Issue("feature mapped without primary tag", el["type"], el["id"], lat, lon,
                                 detail=f"tags present but none are a primary key: {list(tags.keys())}"))
    return issues


def check_wrong_tagging(elements):
    """
    Flags only the high-confidence case: a value that's a real,
    recognised value for a DIFFERENT key showing up under this one
    (e.g. highway=building, area=building) -- not simply "a value we
    haven't catalogued for this key". Our whitelists are illustrative,
    not an exhaustive copy of OSM's full tag vocabulary (e.g. they used
    to be missing railway=level_crossing, a completely standard tag),
    so flagging every uncatalogued value guarantees false positives on
    real tags we just hadn't listed yet. This narrower check trades
    catching a rarer made-up value for eliminating that whole class of
    false positive.
    """
    issues = []
    all_known_values = set()
    for values in config.ENUMERATED_KEY_VALUES.values():
        all_known_values |= values

    for el in elements:
        tags = el.get("tags", {})
        lat, lon = _element_point(el)
        if lat is None:
            continue
        for key, allowed in config.ENUMERATED_KEY_VALUES.items():
            if key in tags and tags[key] not in allowed:
                value = tags[key]
                belongs_elsewhere = any(value in vals for k, vals in config.ENUMERATED_KEY_VALUES.items() if k != key)
                if belongs_elsewhere:
                    issues.append(Issue("wrong tagging", el["type"], el["id"], lat, lon,
                                         detail=f"'{key}={value}' looks like a value meant for a different key"))
        name = tags.get("name")
        if name and name.strip().lower() in all_known_values:
            issues.append(Issue("wrong tagging", el["type"], el["id"], lat, lon,
                                 detail=f"name='{name}' looks like a misplaced tag value, not a real name"))
    return issues


def _element_point(el):
    if el["type"] == "node" and "lat" in el:
        return el["lat"], el["lon"]
    geom = el.get("_geom")
    if geom is not None:
        return geo_utils.centroid_of(geom)
    return None, None


# ---------------------------------------------------------------------------
# Duplicate checks (don't need Overpass -- only compare within the diff)
# ---------------------------------------------------------------------------

def check_duplicate_nodes(created_nodes):
    """Grid-bucketed so this stays fast even for large (mass-upload) changesets."""
    issues = []
    tol = config.DUPLICATE_NODE_TOLERANCE_M
    cell = max(tol, 0.01) / 111000
    buckets = {}
    for n in created_nodes:
        key = (round(n["lat"] / cell), round(n["lon"] / cell))
        buckets.setdefault(key, []).append(n)

    seen_pairs = set()
    for (kx, ky), nodes_here in buckets.items():
        neighbours = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbours.extend(buckets.get((kx + dx, ky + dy), []))
        for a in nodes_here:
            for b in neighbours:
                if a["id"] >= b["id"]:
                    continue
                pair = (a["id"], b["id"])
                if pair in seen_pairs:
                    continue
                d = geo_utils.haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
                if d <= tol:
                    seen_pairs.add(pair)
                    issues.append(Issue("duplicated node", "node", a["id"], a["lat"], a["lon"],
                                         detail=f"within {d:.2f}m of node {b['id']}"))
    return issues


def check_duplicate_ways(new_ways):
    issues = []
    seen = {}
    for w in new_ways:
        if len(w["nodes"]) < 2:
            continue
        key = tuple(sorted(w["nodes"]))
        if key in seen:
            lat, lon = _way_point(w)
            if lat is not None:
                issues.append(Issue("duplicated way", "way", w["id"], lat, lon,
                                     detail=f"shares all the same nodes as way {seen[key]}"))
        else:
            seen[key] = w["id"]
    return issues


def _way_point(w):
    geom = w.get("_geom")
    return geo_utils.centroid_of(geom) if geom is not None else (None, None)


# ---------------------------------------------------------------------------
# Geometry checks that NEED Overpass context: buildings, ways, highways
# compared against pre-existing, nearby map data.
# ---------------------------------------------------------------------------

def check_building_geometry(new_buildings, context_buildings, fetch_module=None):
    """overlapping buildings / building inside building / crossing buildings.

    fetch_module, if given, is used to re-verify a candidate overlap
    against the LIVE OSM API before flagging it, whenever the "other"
    building came from Overpass context rather than from this changeset.
    This guards against Overpass replication lag: if a mapper fixed an
    overlap by editing one building in an earlier changeset and the
    other in this one, Overpass may not have caught up to the earlier
    fix yet, which would otherwise show up as a false positive here.
    """
    issues = []
    context_ids = {b["id"] for b in context_buildings}
    all_polys = new_buildings + context_buildings
    geoms = [b["_geom"] for b in all_polys]
    valid_idx = [i for i, g in enumerate(geoms) if g is not None and g.is_valid and g.area > 0]
    if not valid_idx:
        return issues
    tree = STRtree([geoms[i] for i in valid_idx])

    checked_pairs = set()
    for b in new_buildings:
        geom = b.get("_geom")
        if geom is None or not geom.is_valid or geom.area == 0:
            continue
        for j in tree.query(geom):
            other = all_polys[valid_idx[j]]
            if other["id"] == b["id"]:
                continue
            pair = tuple(sorted((b["id"], other["id"])))
            if pair in checked_pairs:
                continue
            other_geom = other["_geom"]
            if not geom.intersects(other_geom):
                continue

            if fetch_module is not None and other["id"] in context_ids:
                fresh_geom = fetch_module.fetch_live_way_geometry(other["id"])
                if fresh_geom is not None:
                    other_geom = fresh_geom
                    if not geom.is_valid or not other_geom.is_valid or not geom.intersects(other_geom):
                        continue  # the live API shows this overlap is already resolved

            checked_pairs.add(pair)
            lat, lon = geo_utils.centroid_of(geom)

            if geom.within(other_geom) or other_geom.within(geom):
                issues.append(Issue("building inside building", "way", b["id"], lat, lon,
                                     detail=f"fully contained with/containing way {other['id']}"))
                continue

            inter_area = geom.intersection(other_geom).area
            ratio = inter_area / min(geom.area, other_geom.area)
            if ratio < config.BUILDING_OVERLAP_MIN_RATIO:
                continue

            if geom.boundary.crosses(other_geom.boundary):
                issues.append(Issue("crossing buildings", "way", b["id"], lat, lon,
                                     detail=f"outline crosses way {other['id']} ({ratio:.0%} area overlap)"))
            else:
                issues.append(Issue("overlapping buildings", "way", b["id"], lat, lon,
                                     detail=f"{ratio:.0%} area overlap with way {other['id']}"))
    return issues


def check_way_crossings(new_lines, context_lines, issue_name):
    """Generic crossing-line check, reused for both ordinary ways and highways."""
    issues = []
    all_lines = new_lines + context_lines
    geoms = [w["_geom"] for w in all_lines]
    valid_idx = [i for i, g in enumerate(geoms) if g is not None]
    if not valid_idx:
        return issues
    tree = STRtree([geoms[i] for i in valid_idx])
    checked = set()

    for w in new_lines:
        geom = w.get("_geom")
        if geom is None:
            continue
        shared_nodes = set(w["nodes"])
        for j in tree.query(geom):
            other = all_lines[valid_idx[j]]
            if other["id"] == w["id"]:
                continue
            pair = tuple(sorted((w["id"], other["id"])))
            if pair in checked:
                continue
            other_geom = other["_geom"]
            # sharing a node is a normal junction -- only flag a genuine
            # mid-line crossing where the ways aren't actually connected.
            if shared_nodes & set(other.get("nodes", [])):
                continue
            if geom.crosses(other_geom):
                checked.add(pair)
                inter = geom.intersection(other_geom)
                lat, lon = geo_utils.centroid_of(inter if not inter.is_empty else geom)
                issues.append(Issue(issue_name, "way", w["id"], lat, lon,
                                     detail=f"crosses way {other['id']} without a shared junction node"))
    return issues


def check_overlapping_highways(new_highways, context_highways):
    """Two highway ways running along (nearly) the same alignment, not just crossing."""
    issues = []
    for w in new_highways:
        geom = w.get("_geom")
        if geom is None or geom.length == 0:
            continue
        buffered = geom.buffer(0.00003)  # ~3m, in degrees (rough at low latitudes)
        for other in context_highways + new_highways:
            if other["id"] == w["id"]:
                continue
            other_geom = other.get("_geom")
            if other_geom is None or other_geom.length == 0:
                continue
            overlap_len = other_geom.intersection(buffered).length
            ratio = overlap_len / other_geom.length
            if ratio > 0.6:
                lat, lon = geo_utils.centroid_of(geom)
                issues.append(Issue("overlapping highway", "way", w["id"], lat, lon,
                                     detail=f"runs alongside existing way {other['id']} for {ratio:.0%} of its length"))
                break
    return issues


def check_node_connects_highway_and_building(building_ways, highway_ways):
    issues = []
    building_nodes = {}
    for b in building_ways:
        for n in b["nodes"]:
            building_nodes.setdefault(n, []).append(b["id"])

    flagged = set()
    for h in highway_ways:
        for n in h["nodes"]:
            if n in building_nodes and n not in flagged:
                flagged.add(n)
                geom = h.get("_geom")
                if geom is None:
                    continue
                lat, lon = geo_utils.centroid_of(geom)
                issues.append(Issue("node connected highway and building", "node", n, lat, lon,
                                     detail=f"shared by highway way {h['id']} and building way {building_nodes[n][0]}"))
    return issues


def check_endpoint_near_other_way(new_ways, context_ways):
    """Undershoot/overshoot: a way's endpoint sits suspiciously close to
    another way's LINE without actually sharing a node with it.

    Only compares against other lineal geometry (LineString) -- a
    building polygon has no .project() concept of "nearest point along
    it", so polygons must be excluded from the comparison set, not just
    from the "w" being checked.
    """
    issues = []
    all_ways = new_ways + context_ways
    geoms = [w.get("_geom") for w in all_ways]
    valid = [(g, w) for g, w in zip(geoms, all_ways) if isinstance(g, LineString)]
    if not valid:
        return issues
    tree = STRtree([g for g, _ in valid])

    for w in new_ways:
        geom = w.get("_geom")
        if geom is None or not isinstance(geom, LineString) or len(w["nodes"]) < 2:
            continue
        endpoints = [(geom.coords[0], w["nodes"][0]), (geom.coords[-1], w["nodes"][-1])]
        for coord, node_id in endpoints:
            pt = Point(coord)
            for j in tree.query(pt.buffer(0.0001)):
                other_geom, other = valid[j]
                if other["id"] == w["id"] or node_id in other.get("nodes", []):
                    continue
                nearest_pt = other_geom.interpolate(other_geom.project(pt))
                dist_m = geo_utils.haversine_m(pt.y, pt.x, nearest_pt.y, nearest_pt.x)
                if dist_m <= config.ENDPOINT_NEAR_WAY_THRESHOLD_M:
                    issues.append(Issue("way end node near other way", "node", node_id, pt.y, pt.x,
                                         detail=f"{dist_m:.2f}m from way {other['id']} but not connected to it"))
                    break
    return issues


def run_overpass_dependent_checks(cs_meta, new_ways, diff, fetch_module):
    """
    Runs every check that needs to compare this changeset's new ways
    against pre-existing, nearby map data (Overpass context): building
    overlap/containment/crossing, crossing ways/highways, overlapping
    highway alignment, node-connects-highway-building, and endpoint-
    near-other-way.

    Split out from run_all_checks specifically so it can be retried on
    its own later if Overpass was unavailable the first time, without
    re-running (and duplicating) the checks that don't need Overpass.

    Returns (issues, overpass_incomplete). overpass_incomplete is True
    if Overpass could not be reached (outage or circuit breaker) --
    when True, `issues` is always [] and the caller MUST treat this
    changeset as still needing a retry, not as "checked, nothing found".
    """
    if not new_ways:
        return [], False

    changeset_node_index = {
        n["id"]: n for n in diff["create"] + diff["modify"]
        if n["type"] == "node" and n.get("lat") is not None
    }
    all_needed_nodes = {n for w in new_ways for n in w["nodes"]}
    node_coords = fetch_module.fetch_node_coords(list(all_needed_nodes), changeset_node_index)
    for w in new_ways:
        w["_geom"] = geo_utils.build_way_geometry(w["nodes"], node_coords, w.get("tags"))

    try:
        context_ways, context_nodes = fetch_module.fetch_overpass_context(
            cs_meta["min_lat"], cs_meta["min_lon"], cs_meta["max_lat"], cs_meta["max_lon"],
            exclude_way_ids={w["id"] for w in new_ways},
            exclude_node_ids=all_needed_nodes,
        )
    except OverpassUnavailable:
        return [], True
    except (KeyError, TypeError):
        context_ways, context_nodes = [], {}

    for w in context_ways:
        w["_geom"] = geo_utils.build_way_geometry(w["nodes"], context_nodes, w.get("tags"))

    def is_building(w):
        return w.get("tags", {}).get("building") and isinstance(w.get("_geom"), Polygon)

    def is_highway(w):
        return "highway" in w.get("tags", {}) and isinstance(w.get("_geom"), LineString)

    def is_plain_way(w):
        return isinstance(w.get("_geom"), LineString) and not is_highway(w)

    issues = []
    new_buildings = [w for w in new_ways if is_building(w)]
    context_buildings = [w for w in context_ways if is_building(w)]
    issues += check_building_geometry(new_buildings, context_buildings, fetch_module)

    new_highways = [w for w in new_ways if is_highway(w)]
    context_highways = [w for w in context_ways if is_highway(w)]
    issues += check_way_crossings(new_highways, context_highways, "crossing highway")
    issues += check_way_crossings([w for w in new_ways if is_plain_way(w)],
                                   [w for w in context_ways if is_plain_way(w)],
                                   "crossing way")
    issues += check_overlapping_highways(new_highways, context_highways)
    issues += check_node_connects_highway_and_building(
        new_buildings + context_buildings, new_highways + context_highways,
    )
    issues += check_endpoint_near_other_way(new_ways, context_ways)

    return issues, False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_all_checks(cs_meta, diff, fetch_module):
    """
    Runs every check on a freshly-downloaded changeset.
    Returns (issues, overpass_incomplete) -- see run_overpass_dependent_checks
    for what overpass_incomplete means and why the caller must act on it
    (queue this changeset for a later retry) rather than ignore it.
    """
    issues = []
    issues += check_mass_edit_and_revert(cs_meta, diff)
    issues += check_comment_quality(cs_meta)

    changed_elements = diff["create"] + diff["modify"]
    non_relations = [e for e in changed_elements if e["type"] != "relation"]
    issues += check_untagged_and_missing_primary(non_relations)
    issues += check_wrong_tagging(changed_elements)

    created_nodes = [e for e in diff["create"] if e["type"] == "node" and e.get("lat") is not None]
    issues += check_duplicate_nodes(created_nodes)

    new_ways = [e for e in changed_elements if e["type"] == "way"]
    issues += check_duplicate_ways(new_ways)

    overpass_issues, incomplete = run_overpass_dependent_checks(cs_meta, new_ways, diff, fetch_module)
    issues += overpass_issues
    return issues, incomplete


def to_row(cs_meta, issue: Issue):
    country = geocode.country_for(issue.lat, issue.lon)
    return {
        "error_type": issue.issue_type,
        "username": cs_meta.get("user", "unknown"),
        "user_id": cs_meta.get("uid", ""),
        "osm_location_link": geo_utils.map_link(issue.lat, issue.lon) if issue.lat is not None else "",
        "changeset_id": cs_meta["id"],
        "changeset_link": geo_utils.changeset_link(cs_meta["id"]),
        "osm_object_type": issue.osm_type,
        "osm_object_id": issue.osm_id,
        "time_utc": cs_meta.get("closed_at") or cs_meta.get("created_at"),
        "country": country,
        "detail": issue.detail,
    }
