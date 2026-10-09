"""Which store the user is in (or near), from the phone's location.

Kroger-family stores (Kroger, Harris Teeter, Ralphs, Fred Meyer, ...) come from the
Kroger Locations API. The user is "at" the nearest one when it is within
AT_STORE_METERS. Answers are cached per ~100 m cell for an hour to save calls.
"""

import math
import time

from . import config

_cache = {}
CACHE_SECONDS = 3600


def distance_m(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearby(lat, lng, client_factory=None, limit=5):
    """{"at_store": store or None, "stores": [nearest first, with distance_m]}."""
    from kroger_sync.client import KrogerClient, configured

    cell = (round(lat, 3), round(lng, 3))
    hit = _cache.get(cell)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    if client_factory is None and not configured():
        return {"at_store": None, "stores": [], "note": "Kroger API keys not set"}
    stores = (client_factory or KrogerClient)().locations(lat=lat, lng=lng, limit=limit, radius_miles=10)
    for s in stores:
        s["distance_m"] = (round(distance_m(lat, lng, s["lat"], s["lng"]))
                           if s.get("lat") is not None and s.get("lng") is not None else None)
    stores.sort(key=lambda s: s["distance_m"] if s["distance_m"] is not None else math.inf)
    at = stores[0] if stores and stores[0]["distance_m"] is not None \
        and stores[0]["distance_m"] <= config.AT_STORE_METERS else None
    result = {"at_store": at, "stores": stores}
    _cache[cell] = (time.time(), result)
    return result
