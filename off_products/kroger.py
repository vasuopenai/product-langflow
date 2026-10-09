"""Kroger Developer API: store lookup and a resumable product crawl.

Register an app at https://developer.kroger.com (Products scope), then put
KROGER_CLIENT_ID and KROGER_CLIENT_SECRET in .env.

Public API facts this relies on (confirm in Kroger's docs; they could not be
checked from the environment this was written in):
  * OAuth2 client credentials: POST /v1/connect/oauth2/token, scope product.compact
  * GET /v1/products?filter.term=..&filter.locationId=..&filter.limit<=50&filter.start=..
  * GET /v1/locations?filter.zipCode.near=..
  * about 10,000 product calls per day on the public tier

Every raw product is kept in the output JSONL, so changes in field names never
lose data; ``parse_product`` extracts what it recognises.
"""

import base64
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .gtin import kroger_keys

API = "https://api.kroger.com/v1"


def _urllib_fetch(method, url, headers, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class KrogerError(RuntimeError):
    pass


class KrogerClient:
    def __init__(self, client_id=None, client_secret=None, fetch=_urllib_fetch, sleep=time.sleep):
        self.client_id = client_id or os.getenv("KROGER_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("KROGER_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise KrogerError("set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET (e.g. in .env)")
        self.fetch, self.sleep = fetch, sleep
        self._token, self._token_expiry = None, 0.0
        self.calls = 0  # API calls made by this client (token requests excluded)

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
                "GET", url, {"Authorization": f"Bearer {self._get_token()}", "Accept": "application/json"}
            )
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

    def locations(self, zip_code, limit=10):
        data = self.get("/locations", {"filter.zipCode.near": zip_code, "filter.limit": limit})
        return [
            {
                "location_id": loc.get("locationId"),
                "name": loc.get("name"),
                "chain": loc.get("chain"),
                "address": ", ".join(
                    v for v in (loc.get("address", {}).get(k) for k in ("addressLine1", "city", "state", "zipCode")) if v
                ),
            }
            for loc in data.get("data", [])
        ]

    def search(self, term, location_id=None, start=1, limit=50):
        params = {"filter.term": term, "filter.limit": limit, "filter.start": start}
        if location_id:
            params["filter.locationId"] = location_id
        return self.get("/products", params).get("data", [])


# --- crawl ----------------------------------------------------------------------

def _load_state(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return {"done_terms": [], "calls_by_day": {}}


def crawl(client, terms, out_dir, location_id=None, max_calls_per_day=9000, page_size=50,
          max_start=250, progress=print):
    """Search each term page by page, appending new products to products.jsonl.

    Resumable: finished terms and the day's call count live in state.json, so
    re-running continues where it stopped and stays under the daily quota.
    Returns the number of new products written.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    products_path, state_path = out_dir / "products.jsonl", out_dir / "state.json"
    state = _load_state(state_path)
    seen = set()
    if products_path.exists():
        with products_path.open(encoding="utf-8") as f:
            seen = {json.loads(line)["product"].get("productId") for line in f if line.strip()}

    today = dt.date.today().isoformat()
    written = 0
    with products_path.open("a", encoding="utf-8") as out:
        for term in terms:
            if term in state["done_terms"]:
                continue
            start = 1
            while start <= max_start:
                if state["calls_by_day"].get(today, 0) >= max_calls_per_day:
                    progress(f"daily budget of {max_calls_per_day} calls reached; re-run tomorrow to continue")
                    _save_state(state_path, state)
                    return written
                try:
                    page = client.search(term, location_id, start=start, limit=page_size)
                except KrogerError as e:
                    progress(f"'{term}' start={start}: {e}; moving to next term")
                    page = []
                state["calls_by_day"][today] = state["calls_by_day"].get(today, 0) + 1
                for product in page:
                    pid = product.get("productId")
                    if pid in seen:
                        continue
                    seen.add(pid)
                    out.write(json.dumps({
                        "term": term, "location_id": location_id,
                        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                        "product": product,
                    }) + "\n")
                    written += 1
                if len(page) < page_size:
                    break
                start += page_size
            state["done_terms"].append(term)
            _save_state(state_path, state)
            out.flush()
            progress(f"'{term}': done, {len(seen)} unique products so far")
    return written


def _save_state(path, state):
    Path(path).write_text(json.dumps(state, indent=1))


# --- parse ----------------------------------------------------------------------

def _first(value):
    return value[0] if isinstance(value, list) and value else (value if isinstance(value, dict) else {})


def _nutrition(product):
    """Best-effort read of Kroger nutrition. Shape varies by API version, so
    accept a list or a dict under 'nutritionInformation' or 'nutrition'."""
    info = _first(product.get("nutritionInformation") or product.get("nutrition"))
    if not info:
        return None
    serving = info.get("servingSize") or {}
    unit = serving.get("unitOfMeasure") or {}
    nutrients = {}
    for n in info.get("nutrients") or []:
        name = n.get("displayName") or n.get("description") or n.get("code")
        if name:
            u = n.get("unitOfMeasure") or {}
            nutrients[name] = {
                "quantity": n.get("quantity"),
                "unit": u.get("abbreviation") or u.get("code") if isinstance(u, dict) else u,
                "percent_daily": n.get("percentDailyIntake"),
            }
    return {
        "ingredients": info.get("ingredientStatement") or info.get("ingredients"),
        "serving_quantity": serving.get("quantity") if isinstance(serving, dict) else None,
        "serving_unit": (unit.get("abbreviation") or unit.get("code")) if isinstance(unit, dict) else unit,
        "serving_text": info.get("servingSizeText") or info.get("servingSizeDescription"),
        "nutrients": nutrients,
    }


def parse_product(product):
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
    allergens = product.get("allergens") or []
    return {
        "product_id": product.get("productId"),
        "upc": product.get("upc"),
        "keys": kroger_keys(product.get("upc")),
        "brand": product.get("brand"),
        "description": product.get("description"),
        "categories": product.get("categories") or [],
        "size": item.get("size"),
        "price_regular": price.get("regular"),
        "price_promo": price.get("promo") or None,
        "in_store": fulfillment.get("inStore"),
        "stock_level": (item.get("inventory") or {}).get("stockLevel"),
        "aisle": aisle.get("description"),
        "image_url": image,
        "allergens": [a.get("name") if isinstance(a, dict) else a for a in allergens],
        "nutrition": _nutrition(product),
    }


def iter_crawled(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                yield {**parse_product(row["product"]), "location_id": row.get("location_id"),
                       "fetched_at": row.get("fetched_at"), "term": row.get("term")}
