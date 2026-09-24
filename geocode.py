"""
Cached reverse geocoding (coordinate -> country name) via Nominatim.

Cached to a ~1km grid and rate-limited to Nominatim's usage policy
(max ~1 request/second) so a run with hundreds of issues in the same
country only costs a handful of real requests.
"""
import time
import requests

import config

_cache = {}
_last_call = 0.0


def _rate_limit():
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < config.NOMINATIM_MIN_INTERVAL_S:
        time.sleep(config.NOMINATIM_MIN_INTERVAL_S - elapsed)
    _last_call = time.time()


def country_for(lat, lon):
    if lat is None or lon is None:
        return "Unknown"
    key = (round(lat, 2), round(lon, 2))  # ~1km grid -- plenty for country level
    if key in _cache:
        return _cache[key]

    _rate_limit()
    try:
        resp = requests.get(
            config.NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 3, "accept-language": "en"},
            headers={"User-Agent": config.NOMINATIM_USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        country = resp.json().get("address", {}).get("country", "Unknown")
    except Exception:
        country = "Unknown"

    _cache[key] = country
    return country
