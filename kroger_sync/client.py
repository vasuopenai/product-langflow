"""Kroger Developer API client and product parsing.

Register an app at https://developer.kroger.com (Products scope) and set
KROGER_CLIENT_ID and KROGER_CLIENT_SECRET (e.g. in .env).

Public API facts this relies on (from Kroger's docs and third-party clients; not
yet verified against the live API from this project):
  * OAuth2 client credentials: POST /v1/connect/oauth2/token, scope product.compact
  * GET /v1/products?filter.term=..&filter.locationId=..&filter.limit<=50&filter.start=..
  * GET /v1/locations?filter.zipCode.near=..
  * about 10,000 product calls per day on the public tier

The raw response of every product is stored, so a field read wrongly here can be
fixed in ``parse_product`` and re-parsed without calling the API again.
"""

import base64
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .gtin import kroger_keys

# Production. Apps registered in Kroger's Certification environment only work
# against https://api-ce.kroger.com/v1 until they are promoted to production.
API = os.getenv("KROGER_API_BASE", "https://api.kroger.com/v1").rstrip("/")


def _body(raw):
    """Kroger answers gzip-compressed even unasked; urllib doesn't decompress."""
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


def _urllib_fetch(method, url, headers, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"Accept-Encoding": "gzip", **headers},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _body(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _body(e.read())


class KrogerError(RuntimeError):
    pass


def configured():
    return bool(os.getenv("KROGER_CLIENT_ID") and os.getenv("KROGER_CLIENT_SECRET"))


class KrogerClient:
    def __init__(self, client_id=None, client_secret=None, fetch=_urllib_fetch, sleep=time.sleep):
        self.client_id = client_id or os.getenv("KROGER_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("KROGER_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise KrogerError("set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET (e.g. in .env)")
        self.fetch, self.sleep = fetch, sleep
        self._token, self._token_expiry = None, 0.0
        self.calls = 0  # API calls made (token requests excluded)

    def _get_token(self):
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        status, body = self.fetch(
            "POST", f"{API}/connect/oauth2/token",
            {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "product.compact"}).encode(),
        )
        if status != 200:
            raise KrogerError(f"token request failed ({status}): {body[:300]!r}")
        payload = json.loads(body)
        self._token = payload["access_token"]
        self._token_expiry = time.time() + float(payload.get("expires_in", 1800))
        return self._token

    def get(self, path, params, retries=4):
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(retries + 1):
            status, body = self.fetch(
                "GET", url, {"Authorization": f"Bearer {self._get_token()}", "Accept": "application/json"})
            self.calls += 1
            if status == 200:
                return json.loads(body)
            if status == 401 and attempt == 0:
                self._token = None  # expired early; refresh once
                continue
            if status in (429, 500, 502, 503, 504) and attempt < retries:
                self.sleep(2 ** (attempt + 1))
                continue
            raise KrogerError(f"GET {path} failed ({status}): {body[:300]!r}")
        raise KrogerError(f"GET {path} failed after retries")

    def locations(self, zip_code=None, limit=10, lat=None, lng=None, radius_miles=10):
        """Stores near a ZIP code, or near a point (lat/lng), nearest first."""
        params = {"filter.limit": limit, "filter.radiusInMiles": radius_miles}
        if lat is not None and lng is not None:
            params["filter.latLong.near"] = f"{lat},{lng}"
        else:
            params["filter.zipCode.near"] = zip_code
        data = self.get("/locations", params)
        out = []
        for loc in data.get("data", []):
            geo = loc.get("geolocation") or {}
            out.append({
                "location_id": loc.get("locationId"),
                "name": loc.get("name"),
                "chain": loc.get("chain"),
                "address": ", ".join(
                    v for v in (loc.get("address", {}).get(k)
                                for k in ("addressLine1", "city", "state", "zipCode")) if v),
                "lat": geo.get("latitude"), "lng": geo.get("longitude"),
                "phone": loc.get("phone"),
            })
        return out

    def products(self, product_ids, location_id=None):
        """Look up products by Kroger product id (the 13-digit UPC without check digit).

        Uses filter.productId with up to BATCH ids per call; if the API rejects a
        list, falls back to one GET /products/{id} per id (a 404 means not sold)."""
        ids = list(product_ids)
        params = {"filter.productId": ",".join(ids)}
        if location_id:
            params["filter.locationId"] = location_id
        if len(ids) > 1 and not self.single_only:
            try:
                return self.get("/products", params).get("data", [])
            except KrogerError:
                self.single_only = True  # the list form is not supported; use single lookups
        found = []
        for pid in ids:
            p = {"filter.locationId": location_id} if location_id else {}
            try:
                data = self.get(f"/products/{pid}", p).get("data")
            except KrogerError as e:
                if "(404)" in str(e):
                    continue
                raise
            if data:
                found.append(data)
        return found

    single_only = False
    BATCH = 50


def _first(value):
    return value[0] if isinstance(value, list) and value else (value if isinstance(value, dict) else {})


def parse_product(product):
    """The fields the app uses, from one raw /products item."""
    item = _first(product.get("items"))
    price = item.get("price") or {}
    fulfillment = item.get("fulfillment") or {}
    image = None
    for img in product.get("images") or []:
        if img.get("perspective") == "front" or image is None:
            sizes = {s.get("size"): s.get("url") for s in img.get("sizes") or []}
            image = sizes.get("large") or sizes.get("medium") or next(iter(sizes.values()), None)
            if img.get("perspective") == "front":
                break
    aisle = _first(product.get("aisleLocations"))
    return {
        "product_id": product.get("productId"),
        "upc": product.get("upc"),
        "keys": kroger_keys(product.get("upc")),
        "brand": product.get("brand"),
        "description": product.get("description"),
        "category": (product.get("categories") or [None])[0],
        "size": item.get("size"),
        "price_regular": price.get("regular"),
        "price_promo": price.get("promo") or None,
        "in_store": fulfillment.get("inStore"),
        "stock_level": (item.get("inventory") or {}).get("stockLevel"),
        "aisle": aisle.get("description"),
        "image_url": image,
    }
