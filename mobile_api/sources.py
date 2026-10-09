"""Where to find a product the store doesn't have yet, best source first.

Each source returns None (not found) or {"draft": {...}, "raw": <response>, "url": str},
where draft uses the review fields: name, brand, brand_owner, ingredients, category,
serving_size_g, serving_unit, serving_text, package_size, image_url, labels,
nutrients_100g {energy_kcal, protein_g, fat_g, saturated_fat_g, trans_fat_g, carbs_g,
sugars_g, added_sugars_g, fiber_g, sodium_mg, cholesterol_mg}.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .barcodes import key

NUTRIENT_FIELDS = ["energy_kcal", "protein_g", "fat_g", "saturated_fat_g", "trans_fat_g", "carbs_g",
                   "sugars_g", "added_sugars_g", "fiber_g", "sodium_mg", "cholesterol_mg"]
# Review field -> USDA nutrient id (USDA reports sodium and cholesterol in mg, per 100 g).
USDA_IDS = {"energy_kcal": "1008", "protein_g": "1003", "fat_g": "1004", "saturated_fat_g": "1258",
            "trans_fat_g": "1257", "carbs_g": "1005", "sugars_g": "2000", "added_sugars_g": "1235",
            "fiber_g": "1079", "sodium_mg": "1093", "cholesterol_mg": "1253"}
USER_AGENT = "product-langflow-mobile/0.1 (food product lookup)"


def http_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clean(d):
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# --- USDA FoodData Central (live) ---------------------------------------------------

def usda_live(barcode, fetch=http_json):
    """Search FDC Branded Foods by barcode; newer than the bulk download we loaded."""
    k = key(barcode)
    q = urllib.parse.urlencode({"query": k, "dataType": "Branded", "pageSize": 10,
                                "api_key": config.USDA_API_KEY})
    status, data = fetch(f"https://api.nal.usda.gov/fdc/v1/foods/search?{q}")
    if status != 200 or not data:
        return None
    food = next((f for f in data.get("foods") or [] if key(f.get("gtinUpc")) == k), None)
    if not food:
        return None
    ids = {v: name for name, v in USDA_IDS.items()}
    nutrients = {}
    for n in food.get("foodNutrients") or []:
        name = ids.get(str(n.get("nutrientId")))
        if name and n.get("value") is not None:
            nutrients[name] = n["value"]
    unit = (food.get("servingSizeUnit") or "").lower()
    draft = {
        "name": food.get("description"), "brand": food.get("brandName") or food.get("brandOwner"),
        "brand_owner": food.get("brandOwner"), "ingredients": food.get("ingredients"),
        "category": food.get("foodCategory") or food.get("brandedFoodCategory"),
        "serving_size_g": _num(food.get("servingSize")) if unit in ("g", "grm", "ml", "mlt") else None,
        "serving_unit": "ml" if unit in ("ml", "mlt") else "g",
        "serving_text": food.get("householdServingFullText"), "package_size": food.get("packageWeight"),
        "nutrients_100g": nutrients,
    }
    return {"draft": _clean(draft), "raw": food, "fdc_id": str(food.get("fdcId")),
            "url": f"https://fdc.nal.usda.gov/food-details/{food.get('fdcId')}/nutrients"}


# --- Open Food Facts (live) -----------------------------------------------------------

_OFF_NUTRIENTS = {"energy-kcal_100g": ("energy_kcal", 1), "proteins_100g": ("protein_g", 1),
                  "fat_100g": ("fat_g", 1), "saturated-fat_100g": ("saturated_fat_g", 1),
                  "trans-fat_100g": ("trans_fat_g", 1), "carbohydrates_100g": ("carbs_g", 1),
                  "sugars_100g": ("sugars_g", 1), "added-sugars_100g": ("added_sugars_g", 1),
                  "fiber_100g": ("fiber_g", 1), "sodium_100g": ("sodium_mg", 1000),
                  "cholesterol_100g": ("cholesterol_mg", 1000)}


def open_food_facts(barcode, fetch=http_json):
    k = key(barcode)
    fields = ("product_name,brands,brand_owner,ingredients_text,nutriments,serving_size,serving_quantity,"
              "image_front_url,categories_tags,labels_tags,quantity")
    status, data = fetch(f"https://world.openfoodfacts.org/api/v2/product/{k.zfill(13)}.json?fields={fields}")
    if status != 200 or not data or data.get("status") != 1:
        return None
    p = data.get("product") or {}
    nut = p.get("nutriments") or {}
    nutrients = {}
    for off_key, (name, factor) in _OFF_NUTRIENTS.items():
        v = _num(nut.get(off_key))
        if v is not None:
            nutrients[name] = round(v * factor, 4)
    # Only English category tags ("en:dairy-drinks"); others ("es:lacteos") are left for
    # the next source to fill.
    cats = [c for c in p.get("categories_tags") or [] if c.startswith("en:")]
    draft = {
        "name": p.get("product_name"), "brand": (p.get("brands") or "").split(",")[0].strip() or None,
        "brand_owner": p.get("brand_owner"), "ingredients": p.get("ingredients_text"),
        "category": cats[-1].split(":", 1)[-1].replace("-", " ") if cats else None,
        "serving_size_g": _num(p.get("serving_quantity")), "serving_unit": "g",
        "serving_text": p.get("serving_size"), "package_size": p.get("quantity"),
        "image_url": p.get("image_front_url"), "labels": p.get("labels_tags") or [],
        "nutrients_100g": nutrients,
    }
    return {"draft": _clean(draft), "raw": p, "url": f"https://world.openfoodfacts.org/product/{k.zfill(13)}"}


# --- Kroger (name, brand, size, image; no nutrition) ----------------------------------------

def kroger(barcode, location_id=None, client_factory=None):
    from kroger_sync.client import KrogerClient, KrogerError, configured, parse_product
    from kroger_sync.gtin import to_kroger_id

    if client_factory is None and not configured():
        return None
    kid = to_kroger_id(barcode)
    if not kid:
        return None
    try:
        found = (client_factory or KrogerClient)().products([kid], location_id)
    except KrogerError:
        return None
    if not found:
        return None
    p = parse_product(found[0])
    draft = {"name": p["description"], "brand": p["brand"], "package_size": p["size"],
             "image_url": p["image_url"]}
    return {"draft": _clean(draft), "raw": found[0], "url": f"https://www.kroger.com/p/item/{kid}"}


# --- Web search (OpenAI) -------------------------------------------------------------------

WEB_PROMPT = """Find the packaged food or drink product with barcode UPC {upc} (EAN {ean}).
{hint}Search the web (manufacturer site, retailer product pages, product databases) using the
barcode and, if given, the likely product name. Only describe a product if a source ties
it to this barcode or clearly is that exact product (same brand, name, flavor and size);
never fill in data from similar products.

Reply with JSON only (no prose, no code fences):
{{"found": true|false, "name": str, "brand": str, "package_size": str,
  "ingredients": str (exactly as printed, or null), "category": str,
  "serving_text": str (e.g. "1 bar (60 g)"), "serving_size_g": number,
  "nutrition_per_serving": {{"energy_kcal": n, "protein_g": n, "fat_g": n, "saturated_fat_g": n,
     "trans_fat_g": n, "carbs_g": n, "sugars_g": n, "added_sugars_g": n, "fiber_g": n,
     "sodium_mg": n, "cholesterol_mg": n}},
  "image_url": str, "confidence": "high"|"medium"|"low",
  "evidence": str (one sentence: which source matched the barcode),
  "source_urls": [str] (the pages you used)}}
Use null for anything not stated by a source."""


def responses_web_search(client, model, prompt):
    """Run one web-search-enabled response; returns (text, [{"url", "title"}])."""
    last = None
    for tool in ("web_search", "web_search_preview"):
        try:
            resp = client.responses.create(model=model, tools=[{"type": tool}], input=prompt)
            break
        except Exception as e:  # older accounts/models only know the preview tool name
            last = e
    else:
        raise last
    citations = []
    for item in getattr(resp, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            for a in getattr(part, "annotations", None) or []:
                if getattr(a, "type", "") == "url_citation" and a.url not in [c["url"] for c in citations]:
                    citations.append({"url": a.url, "title": getattr(a, "title", None)})
    return resp.output_text or "", citations


def parse_json(text):
    """The first JSON object or array in a model reply (tolerates code fences and prose)."""
    m = re.search(r"[\[{].*[\]}]", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def web(barcode, client=None, model=None, hint=None):
    """OpenAI web search for the barcode; nutrition converted to per 100 g. ``hint`` is
    what other sources already think the product is ("Fairlife High Quality Protein")."""
    if client is None:
        from openai import OpenAI
        client = OpenAI()
    k = key(barcode)
    prompt = WEB_PROMPT.format(
        upc=k.zfill(12), ean=k.zfill(13),
        hint=(f"Other databases say it is probably: {hint}. Confirm it and fill in the label data.\n"
              if hint else ""))
    text, citations = responses_web_search(client, model or config.WEB_SEARCH_MODEL, prompt)
    data = parse_json(text)
    if not isinstance(data, dict) or not data.get("found") or not data.get("name"):
        return None
    serving = _num(data.get("serving_size_g"))
    per_serving = data.get("nutrition_per_serving") or {}
    nutrients = {}
    if serving:
        for f in NUTRIENT_FIELDS:
            v = _num(per_serving.get(f))
            if v is not None:
                nutrients[f] = round(v * 100 / serving, 2)
    draft = {k: data.get(k) for k in ("name", "brand", "package_size", "ingredients", "category",
                                       "serving_text", "image_url")}
    draft.update(serving_size_g=serving, serving_unit="g", nutrients_100g=nutrients)
    # JSON-only replies usually carry no inline citations; the model lists its pages instead.
    if not citations:
        citations = [{"url": u, "title": None} for u in data.get("source_urls") or []
                     if isinstance(u, str) and u.startswith(("http://", "https://"))]
    return {"draft": _clean(draft), "raw": {**data, "citations": citations},
            "url": citations[0]["url"] if citations else None,
            "confidence": data.get("confidence") or "low"}
