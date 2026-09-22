"""
Central configuration for the OSM #tt_event quality-check pipeline.

Everything here can be overridden with an environment variable, so the
same code runs unchanged locally (via a .env file you load yourself,
e.g. with `python-dotenv` or just `export`ing before running) and in
GitHub Actions (via repository secrets passed in as env vars in the
workflow file).
"""
import os

# --- What we're watching for ------------------------------------------------
HASHTAG = os.getenv("QC_HASHTAG", "tt_event")  # without the leading '#'

# --- OSM API -----------------------------------------------------------------
OSM_API_BASE = os.getenv("OSM_API_BASE", "https://api.openstreetmap.org/api/0.6")

# --- Overpass ------------------------------------------------------------
OVERPASS_ENDPOINTS = [
    os.getenv("OVERPASS_PRIMARY", "https://overpass-api.de/api/interpreter"),
    os.getenv("OVERPASS_FALLBACK", "https://overpass.kumi.systems/api/interpreter"),
]
# how far (metres) around a changeset's bbox to pull existing context geometry
OVERPASS_CONTEXT_BUFFER_M = float(os.getenv("QC_OVERPASS_BUFFER_M", 50))
# how long (seconds) to wait for a single HTTP response from a mirror
OVERPASS_HTTP_TIMEOUT_S = int(os.getenv("QC_OVERPASS_HTTP_TIMEOUT_S", 60))
# told to the Overpass server itself as its own internal execution budget
OVERPASS_QUERY_TIMEOUT_S = int(os.getenv("QC_OVERPASS_QUERY_TIMEOUT_S", 200))
# how many times to retry a single mirror before moving to the next one
OVERPASS_RETRIES = int(os.getenv("QC_OVERPASS_RETRIES", 1))

# --- osmcha (optional enrichment only -- never a hard dependency) -----------
OSMCHA_API_BASE = os.getenv("OSMCHA_API_BASE", "https://osmcha.org/api/v1")
OSMCHA_TOKEN = os.getenv("OSMCHA_TOKEN")  # set as a GitHub Actions secret

# --- Nominatim (reverse geocoding for "country") -----------------------------
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/reverse")
NOMINATIM_USER_AGENT = os.getenv(
    "NOMINATIM_USER_AGENT",
    "osm-tt-event-quality-check/1.0 (set a contact email in this User-Agent)",
)
NOMINATIM_MIN_INTERVAL_S = 1.1  # Nominatim usage policy: max ~1 request/second

# --- Thresholds: mass upload / delete ----------------------------------------
MASS_CREATE_THRESHOLD = int(os.getenv("QC_MASS_CREATE_THRESHOLD", 10000))
MASS_MODIFY_THRESHOLD = int(os.getenv("QC_MASS_MODIFY_THRESHOLD", 10000))
MASS_DELETE_THRESHOLD = int(os.getenv("QC_MASS_DELETE_THRESHOLD", 10000))

# Signatures checked (case-insensitive) in the changeset's created_by/comment
# tags to decide whether a mass delete is a legitimate revert.
REVERT_SIGNATURES = ("reverter_plugin", "revert")

# --- Geometry check tolerances ------------------------------------------------
DUPLICATE_NODE_TOLERANCE_M = float(os.getenv("QC_DUP_NODE_TOLERANCE_M", 0.05))
BUILDING_OVERLAP_MIN_RATIO = float(os.getenv("QC_BUILDING_OVERLAP_MIN_RATIO", 0.02))
ENDPOINT_NEAR_WAY_THRESHOLD_M = float(os.getenv("QC_ENDPOINT_NEAR_WAY_M", 0.5))

# --- Storage -------------------------------------------------------------
DATA_DIR = os.getenv("QC_DATA_DIR", "data")
CSV_BASENAME = os.getenv("QC_CSV_BASENAME", "quality_check")
CSV_MAX_BYTES = 40 * 1024 * 1024  # 40MB per file, per requirement
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# --- Slack (wired up in a later step; kept here so main.py never changes) ---
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")
# Off by default. Flip QC_SLACK_ENABLED=1 (env var or GitHub Actions env)
# once the Slack step of this project is wired up.
SLACK_ENABLED = os.getenv("QC_SLACK_ENABLED", "") in ("1", "true", "True")

# --- Tag-key whitelists used by the "wrong tagging" check --------------------
# Not exhaustive -- covers the common, high-confidence "value went into the
# wrong key" mistakes (e.g. name=building, highway=building, area=building).
ENUMERATED_KEY_VALUES = {
    "area": {"yes", "no"},
    "building": {
        "yes", "house", "residential", "apartments", "detached", "terrace",
        "semidetached_house", "garage", "garages", "commercial", "industrial",
        "retail", "warehouse", "school", "church", "hospital", "hotel",
        "office", "roof", "hut", "shed", "cabin", "farm", "farm_auxiliary",
        "barn", "greenhouse", "service", "civic", "public", "stadium",
        "train_station", "transportation", "kiosk", "construction", "ruins",
        "collapsed", "no",
    },
    "highway": {
        "motorway", "trunk", "primary", "secondary", "tertiary",
        "unclassified", "residential", "service", "track", "path",
        "footway", "cycleway", "bridleway", "steps", "pedestrian",
        "living_street", "road", "motorway_link", "trunk_link",
        "primary_link", "secondary_link", "tertiary_link", "construction",
        "proposed", "bus_stop", "crossing", "traffic_signals", "give_way",
        "stop", "mini_roundabout", "turning_circle", "milestone", "elevator",
    },
    "landuse": {
        "residential", "commercial", "industrial", "retail", "farmland",
        "farmyard", "forest", "meadow", "grass", "orchard", "vineyard",
        "quarry", "cemetery", "construction", "military", "railway",
        "recreation_ground", "allotments", "landfill", "brownfield",
        "greenfield",
    },
    "natural": {
        "wood", "water", "wetland", "tree", "tree_row", "scrub",
        "grassland", "heath", "bare_rock", "sand", "beach", "cliff",
        "coastline", "peak", "valley", "ridge", "glacier", "volcano", "bay",
    },
    "waterway": {
        "river", "stream", "canal", "drain", "ditch", "dam", "weir",
        "waterfall", "riverbank", "boatyard",
    },
    "railway": {
        "rail", "subway", "light_rail", "tram", "narrow_gauge", "monorail",
        "funicular", "station", "halt", "platform", "construction",
        "abandoned", "disused",
    },
    "amenity": {
        "restaurant", "cafe", "school", "hospital", "bank", "pharmacy",
        "fuel", "parking", "place_of_worship", "toilets", "bar", "pub",
        "fast_food", "police", "fire_station", "post_office", "library",
        "clinic", "marketplace", "waste_basket", "bench", "drinking_water",
        "atm", "kindergarten", "university", "college", "townhall",
        "community_centre", "social_facility", "veterinary",
    },
}

# Keys that count as "this feature has a real primary tag" for the
# missing-primary-tag check..
PRIMARY_TAG_KEYS = {
    "building", "highway", "natural", "landuse", "amenity", "waterway",
    "railway", "leisure", "shop", "tourism", "man_made", "barrier",
    "boundary", "landcover", "power", "aeroway", "military", "office",
    "craft", "emergency", "healthcare", "public_transport", "place",
    "historic", "geological", "route",
}
