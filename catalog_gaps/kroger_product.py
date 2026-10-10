"""Product information from a Kroger product record: name, brand, size, photo, ingredients,
nutrition, allergens and claims, in the draft shape staging and approval use (see
mobile_api.staging.REVIEW_FIELDS). No prices and no store details: the crawl searches
without a store, so Kroger doesn't send them.

Kroger gives nutrition per serving. Per-100 g values (what search filters use) need the
serving in grams: taken from the serving size when it's a weight or volume, otherwise
estimated as package weight / servings per package (a 16 oz jar with 14 servings: ~32 g).
"""

import re

from kroger_sync.client import parse_product

# Serving and package units in grams (volumes as 1 g/ml, as USDA labels treat drinks).
UNIT_G = {"GRM": 1.0, "KGM": 1000.0, "ONZ": 28.3495, "LBR": 453.592, "MLT": 1.0, "ML": 1.0, "LTR": 1000.0,
          "OZA": 29.5735, "FO": 29.5735}
VOLUME_UNITS = {"MLT", "ML", "LTR", "OZA", "FO"}

# Our nutrient keys -> Kroger nutrient codes, most specific first.
NUTRIENTS = {
    "energy_kcal": ("ENER-",),
    "protein_g": ("PRO-", "PROT", "PRO"),
    "fat_g": ("FAT", "FAT-", "FATNLEA"),
    "saturated_fat_g": ("FASAT",),
    "trans_fat_g": ("FATRN",),
    "carbs_g": ("CHO-", "CHOCDF", "CHOAVL"),
    "sugars_g": ("SUGAR", "SUGAR-"),
    "added_sugars_g": ("SUGAD",),
    "fiber_g": ("FIBTG", "FIB-"),
    "sodium_mg": ("NA",),
    "cholesterol_mg": ("CHOL-",),
}
# Unit factors to grams / milligrams / kilocalories. A missing unit means the label's usual one.
_TO_G = {"GRM": 1.0, "MGM": 0.001, None: 1.0}
_TO_MG = {"MGM": 1.0, "GRM": 1000.0, None: 1.0}
_TO_KCAL = {"E14": 1.0, "D70": 1.0, None: 1.0}  # label "Calories" are kilocalories

PACKAGE = re.compile(r"(\d+(?:\.\d+)?)\s*(fl\.?\s*oz|oz|lbs?|kg|g|ml|l|liters?|litres?)\b", re.I)
PACKAGE_G = {"fl oz": 29.5735, "oz": 28.3495, "lb": 453.592, "lbs": 453.592, "kg": 1000.0, "g": 1.0,
             "ml": 1.0, "l": 1000.0, "liter": 1000.0, "liters": 1000.0, "litre": 1000.0, "litres": 1000.0}


def _num(v):
    try:
        f = float(v)
        return f if f >= 0 else None
    except (TypeError, ValueError):
        return None


def _list(v):
    return v if isinstance(v, list) else []


def panel(product):
    """The nutrition block to use: the as-sold (unprepared) one when there are several."""
    blocks = [b for b in _list(product.get("nutritionInformation")) if isinstance(b, dict)]
    for b in blocks:
        if (b.get("preparationState") or {}).get("code") in ("UNPREPARED", None) and b.get("nutrients"):
            return b
    return next((b for b in blocks if b.get("nutrients")), blocks[0] if blocks else {})


COUNT = re.compile(r"(\d+(?:\.\d+)?)\s*ct\b", re.I)


def package_grams(size):
    """Net contents in grams from Kroger's size text: '16 oz', '12 fl oz', or 'N ct / X oz'
    (N items of X oz each, e.g. '40 ct / 0.8 oz' is 40 pouches of 0.8 oz)."""
    m = PACKAGE.search(size or "")
    if not m:
        return None
    unit = re.sub(r"\s+", " ", m.group(2).lower().replace(".", ""))
    grams = float(m.group(1)) * PACKAGE_G.get(unit, 0)
    count = COUNT.search((size or "")[:m.start()])  # a count before the weight multiplies it
    if count and "/" in (size or "")[count.end():m.start()]:
        grams *= float(count.group(1))
    return grams or None


def serving(product, block=None):
    """(grams per serving or None, 'g' or 'ml', serving text as printed)."""
    block = block if block is not None else panel(product)
    size = block.get("servingSize") or {}
    qty = _num(size.get("quantity"))
    unit = (size.get("unitOfMeasure") or {}).get("code")
    abbr = (size.get("unitOfMeasure") or {}).get("abbreviation") or (size.get("unitOfMeasure") or {}).get("name")
    text = " ".join(str(x) for x in (size.get("quantity"), abbr) if x not in (None, "")) or None
    kind = "ml" if unit in VOLUME_UNITS else "g"
    if qty and unit in UNIT_G:
        return qty * UNIT_G[unit], kind, text
    per_pack = _num((block.get("servingsPerPackage") or {}).get("value"))
    pack = package_grams(parse_product(product).get("size"))
    if per_pack and pack and per_pack >= 1:
        return pack / per_pack, kind, text
    return None, kind, text


def nutrients_per_serving(block):
    by_code = {}
    for n in _list(block.get("nutrients")):
        if isinstance(n, dict) and n.get("code") and _num(n.get("quantity")) is not None:
            by_code.setdefault(n["code"], n)
    out = {}
    for key, codes in NUTRIENTS.items():
        n = next((by_code[c] for c in codes if c in by_code), None)
        if not n:
            continue
        unit = (n.get("unitOfMeasure") or {}).get("code")
        table = _TO_KCAL if key == "energy_kcal" else _TO_MG if key.endswith("_mg") else _TO_G
        if unit not in table:
            continue  # e.g. energy given in grams: not usable
        out[key] = round(_num(n["quantity"]) * table[unit], 3)
    return out


def labels(product):
    out = []
    if product.get("organicClaimName"):
        out.append("en:organic")
    if product.get("nonGmoClaimName") or product.get("nonGmo"):
        out.append("en:no-gmos")
    if (product.get("hypoallergenic") is True):
        out.append("hypoallergenic")
    return out


def product_info(product):
    """The draft for staging: everything Kroger says about the product except price and store."""
    p = parse_product(product)
    block = panel(product)
    grams, unit, text = serving(product, block)
    per_serving = nutrients_per_serving(block)
    per_100 = {k: round(v * 100 / grams, 3) for k, v in per_serving.items()} if grams else {}
    allergens = product.get("allergensDescription") or None
    draft = {
        "name": p["description"], "brand": p["brand"], "category": p["category"], "package_size": p["size"],
        "image_url": p["image_url"], "ingredients": (block.get("ingredientStatement") or "").strip() or None,
        "serving_size_g": round(grams, 2) if grams else None, "serving_unit": unit if grams else None,
        "serving_text": text, "nutrients_100g": per_100 or None, "nutrients_serving": per_serving or None,
        "labels": labels(product) or None, "allergens": allergens,
        "country_of_origin": product.get("countryOrigin") or None,
    }
    return {k: v for k, v in draft.items() if v not in (None, "", [], {})}


def completeness(draft):
    """'full' (ingredients and per-100 g nutrition), 'partial' or 'basic' (name only)."""
    has_ing, has_nut = bool(draft.get("ingredients")), bool(draft.get("nutrients_100g"))
    return "full" if has_ing and has_nut else "partial" if has_ing or has_nut or draft.get("nutrients_serving") else "basic"
