"""Small geometry and link-building helpers shared across the pipeline."""
import math
from shapely.geometry import Polygon, LineString

EARTH_RADIUS_M = 6371000.0


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def way_is_closed(node_ids):
    return len(node_ids) > 2 and node_ids[0] == node_ids[-1]


def build_way_geometry(node_ids, node_coords, tags=None):
    """
    Returns a shapely Polygon for a closed way that represents an area
    (building/landuse/etc.), otherwise a LineString. node_coords must map
    node_id -> (lon, lat). Returns None if coordinates can't be resolved.
    """
    try:
        coords = [node_coords[n] for n in node_ids]
    except KeyError:
        return None
    if len(coords) < 2:
        return None
    if way_is_closed(node_ids) and (tags or {}).get("area") != "no":
        if len(coords) < 4:
            return None
        try:
            poly = Polygon(coords)
            return poly if poly.is_valid else poly.buffer(0)
        except Exception:
            return None
    try:
        return LineString(coords)
    except Exception:
        return None


def changeset_link(changeset_id):
    return f"https://www.openstreetmap.org/changeset/{changeset_id}"


def osm_object_link(osm_type, osm_id):
    return f"https://www.openstreetmap.org/{osm_type}/{osm_id}"


def map_link(lat, lon, zoom=18):
    return f"https://www.openstreetmap.org/#map={zoom}/{lat:.6f}/{lon:.6f}"


def centroid_of(geom):
    """Returns (lat, lon) for any shapely geometry's centroid."""
    c = geom.centroid
    return c.y, c.x


def line_length_m(line):
    """Real-world length of a LineString in metres (its own .length is in
    degrees, since it's built directly from lon/lat, not a projected CRS)."""
    coords = list(line.coords)
    total = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        total += haversine_m(lat1, lon1, lat2, lon2)
    return total
