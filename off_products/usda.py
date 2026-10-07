"""USDA FoodData Central "Branded Foods" -> the app's canonical record.

USDA is the source of truth for identity (GTIN, brand, name), nutrition and the
ingredient statement. Open Food Facts is joined by barcode only to fill fields
USDA does not have (label claims, front image, NOVA group, popularity); it never
overrides a USDA value.

USDA ships the ingredient statement as one string, so it is parsed here into the
same nested item list the rest of the app uses. Ingredient ids are our own
vocabulary: the cleaned ingredient name as a slug ("EXPELLER PRESSED AVOCADO
OIL" -> "en:avocado-oil"), with a few synonyms folded together.

Files used from the CSV release (unzipped into one directory):
  branded_food.csv, food.csv, food_nutrient.csv
"""

import re
import string

from .concepts import GENERIC_OIL_TEXT, classify_text
from .normalize import checked_nutrition, search_text

# --- nutrients ---------------------------------------------------------------

# USDA nutrient id -> (canonical name, factor to our unit). Amounts are per 100 g
# (or 100 ml); sodium and cholesterol are reported in mg, we store grams.
USDA_NUTRIENTS = {
    "1008": ("energy_kcal", 1),
    "1003": ("protein_g", 1),
    "1004": ("fat_g", 1),
    "1258": ("saturated_fat_g", 1),
    "1257": ("trans_fat_g", 1),
    "1005": ("carbs_g", 1),
    "2000": ("sugars_g", 1),
    "1235": ("added_sugars_g", 1),
    "1079": ("fiber_g", 1),
    "1093": ("sodium_g", 0.001),
    "1253": ("cholesterol_g", 0.001),
}
ENERGY_KJ = "1062"
ALCOHOL = "1018"
NUTRIENT_IDS = set(USDA_NUTRIENTS) | {ENERGY_KJ, ALCOHOL}
_CANONICAL = ["energy_kcal", "protein_g", "fat_g", "saturated_fat_g", "trans_fat_g", "carbs_g",
              "sugars_g", "added_sugars_g", "fiber_g", "salt_g", "sodium_g", "cholesterol_g"]
_GRAM_UNITS = {"g", "grm", "gm"}
_ML_UNITS = {"ml", "mlt"}


def nutrition(raw):
    amounts = raw.get("nutrients") or {}
    per_100g = dict.fromkeys(_CANONICAL)
    for nid, (name, factor) in USDA_NUTRIENTS.items():
        if amounts.get(nid) is not None:
            per_100g[name] = round(amounts[nid] * factor, 4)
    if per_100g["energy_kcal"] is None and amounts.get(ENERGY_KJ) is not None:
        per_100g["energy_kcal"] = round(amounts[ENERGY_KJ] / 4.184, 1)
    if per_100g["sodium_g"] is not None:
        per_100g["salt_g"] = round(per_100g["sodium_g"] * 2.5, 4)

    unit = (raw.get("serving_size_unit") or "").strip().lower()
    serving = _float(raw.get("serving_size"))
    serving_g = serving if serving and unit in _GRAM_UNITS | _ML_UNITS else None
    per_serving = {k: (round(v * serving_g / 100, 2) if v is not None and serving_g else None)
                   for k, v in per_100g.items()}
    # The checks read the label text and whether the product is a liquid.
    label = {"serving_size": raw.get("household_serving_fulltext") or None,
             "serving_quantity_unit": "ml" if unit in _ML_UNITS else "g"}
    block = checked_nutrition(label, per_100g, per_serving, serving_g, amounts.get(ALCOHOL),
                              basis_on_label="serving")
    if serving_g:
        block["serving_size"] = f"{label['serving_size'] or '1 serving'} ({_num(serving_g)} {unit_label(unit)})"
    return block


def unit_label(unit):
    return "ml" if unit in _ML_UNITS else "g"


# --- ingredient statement parser ----------------------------------------------

_ALLERGENS = {
    "milk": "en:milk", "dairy": "en:milk", "egg": "en:eggs", "eggs": "en:eggs",
    "fish": "en:fish", "shellfish": "en:crustaceans", "crustacean": "en:crustaceans",
    "crustacean shellfish": "en:crustaceans", "tree nut": "en:nuts", "tree nuts": "en:nuts",
    "peanut": "en:peanuts", "peanuts": "en:peanuts", "wheat": "en:gluten", "gluten": "en:gluten",
    "soy": "en:soybeans", "soybean": "en:soybeans", "soybeans": "en:soybeans",
    "sesame": "en:sesame-seeds", "almond": "en:nuts", "almonds": "en:nuts", "coconut": "en:nuts",
    "cashew": "en:nuts", "cashews": "en:nuts", "pecan": "en:nuts", "pecans": "en:nuts",
    "walnut": "en:nuts", "walnuts": "en:nuts", "hazelnut": "en:nuts", "hazelnuts": "en:nuts",
    "pistachio": "en:nuts", "pistachios": "en:nuts", "macadamia": "en:nuts",
}
# "CONTAINS: MILK, SOY." / "CONTAINS WHEAT AND MILK INGREDIENTS" / "MAY CONTAIN PEANUTS"
_ALLERGEN_STATEMENT = re.compile(
    r"(?:[.;]|\s)\s*(?:ALLERGENS?:\s*)?(MAY\s+CONTAIN|CONTAINS(?!\s+(?:LESS|\d|ONE|TWO)))"
    r"\s*:?\s+([A-Z ,&/()-]+?)(?:\s+INGREDIENTS?)?\s*\.?\s*$", re.I)
# "CONTAINS 2% OR LESS OF:", "LESS THAN 2% OF", "CONTAINS LESS THAN 0.5% OF EACH OF THE FOLLOWING:"
_MINOR = re.compile(
    r"(?:CONTAINS\s+)?(?:(\d+(?:\.\d+)?)\s*%\s*OR\s+LESS|LESS\s+THAN\s+(\d+(?:\.\d+)?)\s*%)"
    r"(?:\s+OF)?(?:\s+EACH)?(?:\s+OF)?(?:\s+THE\s+FOLLOWING)?\s*:?\s*", re.I)
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# Parenthesised notes about an ingredient's purpose, e.g. "(PRESERVATIVE)", "[AN EMULSIFIER]".
_FUNCTION_NOTE = re.compile(
    r"^(?:an?\s+|as\s+an?\s+|used\s+as\s+an?\s+)?(?:preservatives?|emulsifiers?|colou?r(?:ing)?|"
    r"antioxidants?|stabilizers?|thickeners?|leavening|leavening agents?|anti-?caking agents?|"
    r"dough conditioners?|acidulants?|firming agents?|humectants?|"
    r"(?:to|for)\s+(?:preserve|maintain|retain|protect|promote|prevent|help|freshness|colou?r|flavou?r)\b.*|"
    r"added\s+(?:to|for|as)\b.*|for\s+\w+(?:\s+\w+)?)$", re.I)
_FOOTNOTES = re.compile(r"[*†‡^®™]+")
_LEADING_WORD = re.compile(r"^(?:and|&|contains|including|with)\s+", re.I)
_AMOUNT = re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|µg|ug|g|iu|ppm)(?:\s*/\s*(?:kg|100\s*g|g))?(?!\w)", re.I)
_OPEN, _CLOSE = "([{", ")]}"
_MINOR_MARK = "\x00minor:"


def _split(text):
    """Split on commas/semicolons that are not inside brackets."""
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(depth - 1, 0)
        if ch in ",;" and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _head_and_children(item):
    """'ENRICHED FLOUR (WHEAT FLOUR, NIACIN) BLEND' -> ('ENRICHED FLOUR BLEND', 'WHEAT FLOUR, NIACIN')."""
    depth, start, inner, head = 0, None, [], []
    for i, ch in enumerate(item):
        if ch in _OPEN:
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch in _CLOSE and depth:
            depth -= 1
            if depth == 0 and start is not None:
                inner.append(item[start:i])
                start = None
        elif depth == 0:
            head.append(ch)
    if depth and start is not None:  # unbalanced: keep the rest as children
        inner.append(item[start:])
    return " ".join("".join(head).split()), ", ".join(inner)


_QUALIFIERS = re.compile(
    r"\b(?:organic|expeller[- ]pressed|cold[- ]pressed|high[- ]oleic|mid[- ]oleic|non[- ]gmo|"
    r"refined|unrefined|filtered|purified|pure|fresh|enriched|unbleached|bleached|"
    r"dry[- ]roasted|roasted|raw|blanched|sliced|slivered|chopped|diced|natural(?=\s+\w+\s+oil)|"
    r"virgin|extra virgin|certified|from concentrate|pasteurized|ultra[- ]pasteurized|grade a)\b", re.I)
_PLURAL_KEEP = {"molasses", "citrus", "asparagus", "hummus", "couscous", "swiss", "grits",
                "oats", "series", "species", "lentils", "peas", "chips"}
_SYNONYMS = {
    "cashew-nut": "cashew", "pecan-nut": "pecan", "pistachio-nut": "pistachio",
    "macadamia": "macadamia-nut", "filbert": "hazelnut", "mixed-nut": "mixed-nut",
    "chocolate-liquor": "cocoa", "cocoa-mass": "cocoa", "cacao": "cocoa", "cacao-bean": "cocoa",
    "cocoa-bean": "cocoa", "unsweetened-chocolate": "cocoa", "cocoa-paste": "cocoa",
    "cacao-mass": "cocoa", "cacao-paste": "cocoa", "chocolate-liquor-processed-with-alkali": "cocoa",
    "cacao-liquor": "cocoa", "cocoa-liquor": "cocoa",
    "soybean-oil": "soybean-oil", "soy-oil": "soybean-oil", "soya-oil": "soybean-oil",
    "rapeseed-oil": "canola-oil", "oat": "oat", "oats": "oat", "rolled-oats": "oat",
    "whole-grain-oats": "oat", "whole-grain-rolled-oats": "oat",
}
NUT_IDS = {"en:almond", "en:peanut", "en:cashew", "en:pecan", "en:walnut", "en:pistachio",
           "en:macadamia-nut", "en:hazelnut", "en:brazil-nut", "en:mixed-nut", "en:nut"}
_PARENTS = {"en:cocoa-powder": "en:cocoa", "en:dutch-cocoa": "en:cocoa",
            "en:cocoa-processed-with-alkali": "en:cocoa"}


def _singular(word):
    if word in _PLURAL_KEEP or len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("oes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


_TRAILING_NOTE = re.compile(
    r"\s+(?:\(?\s*)?(?:added\s+)?(?:as\s+an?|to|for|used\s+as|used\s+to)\s+(?:an?\s+)?"
    r"(?:dough conditioner|preservative|emulsifier|antioxidant|stabilizer|thickener|colou?r|"
    r"protect|preserve|maintain|retain|promote|prevent|help|freshness|leavening|acidulant)\b.*$", re.I)


def ingredient_id(name):
    text = _TRAILING_NOTE.sub("", name.lower())
    text = _QUALIFIERS.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).split()
    if not text:
        return None
    text[-1] = _singular(text[-1])
    slug = "-".join(text)
    return "en:" + _SYNONYMS.get(slug, slug)


def _clean(text):
    text = _FOOTNOTES.sub("", text or "").strip()
    text = re.sub(r"^\s*INGREDIENTS?\s*:\s*", "", text, flags=re.I)
    return text.strip().rstrip(".").strip()


def allergens_from_statement(text):
    """Split 'CONTAINS: MILK, SOY.' off the end; return (rest, allergens, traces)."""
    allergens, traces = set(), set()
    while True:
        m = _ALLERGEN_STATEMENT.search(text)
        if not m:
            return text, sorted(allergens), sorted(traces)
        words = [w.strip().lower() for w in re.split(r",|\band\b|&|/", m.group(2)) if w.strip()]
        found = {_ALLERGENS[w] for w in words if w in _ALLERGENS}
        if not found:
            return text, sorted(allergens), sorted(traces)
        (traces if m.group(1).upper().startswith("MAY") else allergens).update(found)
        text = text[:m.start()].rstrip(" .;,")


def _and_or(name):
    """'CANOLA AND/OR SOYBEAN OIL' -> ['CANOLA OIL', 'SOYBEAN OIL']."""
    parts = [p.strip() for p in re.split(r"\s+AND/OR\s+|\s+AND\s+OR\s+|\s*/\s*OR\s+", name, flags=re.I)
             if p.strip()]
    if len(parts) < 2:
        return [name]
    tail = parts[-1].split()[-1]
    return [p if len(p.split()) > 1 or p.lower().endswith(tail.lower()) else f"{p} {tail}"
            for p in parts[:-1]] + [parts[-1]]


def _parse_level(text, depth, items):
    minor_max = None
    text = _MINOR.sub(lambda m: f",{_MINOR_MARK}{m.group(1) or m.group(2)},", text)
    for part in _split(text):
        if part.startswith(_MINOR_MARK):
            minor_max = float(part[len(_MINOR_MARK):])
            continue
        head, inner = _head_and_children(part)
        if ":" in head:  # "VITAMINS AND MINERALS: NIACIN" -> NIACIN
            head = head.split(":", 1)[1].strip()
        declared = None
        m = _PERCENT.search(head)
        if m:
            declared = float(m.group(1))
            head = " ".join(_PERCENT.sub(" ", head).split())
        if inner and _PERCENT.fullmatch(inner.strip()):
            declared, inner = float(_PERCENT.match(inner.strip()).group(1)), ""
        if inner and _FUNCTION_NOTE.match(inner.strip()):  # "(PRESERVATIVE)" is not an ingredient
            inner = ""
        head = head.strip(" .:-")
        # "..., AND SEA SALT" / "CONTAINS NIACIN 75 MG/KG": keep only the ingredient name.
        head = _LEADING_WORD.sub("", head)
        head = " ".join(_AMOUNT.sub(" ", head).split()).strip(" .:-")
        if not head:
            if inner:
                _parse_level(inner, depth, items)
            continue
        for name in _and_or(head):
            items.append({
                "id": ingredient_id(name), "text": name.lower(), "depth": depth,
                "rank": None, "percent": declared, "percent_min": None,
                "percent_max": minor_max, "percent_estimate": None,
                "is_in_taxonomy": 1, "minor": minor_max is not None,
            })
            if inner:
                _parse_level(inner, depth + 1, items)


def _estimate(items):
    """Percent estimates per level: label order is descending by weight, minor items
    (after "2% or less") get half their cap, declared percents are kept."""
    def level(indices, total):
        if not indices or total is None:
            return
        main = [i for i in indices if items[i]["percent"] is None and not items[i]["minor"]]
        minor = [i for i in indices if items[i]["percent"] is None and items[i]["minor"]]
        fixed = sum(items[i]["percent"] for i in indices if items[i]["percent"] is not None)
        for i in minor:
            items[i]["percent_estimate"] = items[i]["percent_max"] / 2
        remaining = total - fixed - sum(items[i]["percent_estimate"] for i in minor)
        if main and remaining > 0:
            n = len(main)
            mids = [((total / k) + (total / n if k == 1 else 0)) / 2 for k in range(1, n + 1)]
            scale = remaining / sum(mids)
            for i, mid in zip(main, mids):
                items[i]["percent_estimate"] = round(mid * scale, 2)
        for i in indices:
            children = _children(items, i)
            share = items[i]["percent"] if items[i]["percent"] is not None else items[i]["percent_estimate"]
            level(children, share)

    level([i for i, it in enumerate(items) if it["depth"] == 0], 100.0)


def _children(items, i):
    out, d = [], items[i]["depth"]
    for j in range(i + 1, len(items)):
        if items[j]["depth"] <= d:
            break
        if items[j]["depth"] == d + 1:
            out.append(j)
    return out


def ingredients(text, name=""):
    """Parse a USDA ingredient statement into the canonical ingredients block."""
    raw = _clean(text)
    body, allergens, traces = allergens_from_statement(raw)
    items = []
    if body:
        _parse_level(body, 0, items)
    rank = 0
    for it in items:
        if it["depth"] == 0:
            rank += 1
            it["rank"] = rank
    _estimate(items)
    # "85% CACAO" in the product name is the declared cocoa content.
    m = re.search(r"(\d{2,3}(?:\.\d+)?)\s*%\s*(?:CACAO|COCOA|DARK)", name or "", re.I)
    cocoa = next((it for it in items if it["id"] == "en:cocoa" and it["depth"] == 0), None)
    if m and cocoa and float(m.group(1)) <= 100:
        cocoa["percent"] = float(m.group(1))
    leaves = [it for i, it in enumerate(items) if not _children(items, i)]
    tags = set()
    for it in items:
        if it["id"]:
            tags.add(it["id"])
            if it["id"] in NUT_IDS - {"en:nut"} or it["id"].endswith("-nut"):
                tags.add("en:nut")
            if it["id"] in _PARENTS:
                tags.add(_PARENTS[it["id"]])
    top = [it for it in items if it["depth"] == 0]
    return {
        "text": raw or None,
        "count_top_level": len(top) if items else None,
        "count_total": len(leaves) if items else None,
        "items": items,
        "leaves": leaves,
        "first": top[0]["id"] if top else None,
        "tags": sorted(tags),
        "allergens": allergens,
        "traces": traces,
        "vegan": None,
        "vegetarian": None,
        "palm_oil_free": None,
    }


# --- categories and labels ------------------------------------------------------

# Our category ids over USDA's mixed category strings; first matching rule wins.
CATEGORY_RULES = [
    ("cat:nut-seed-butters", r"nut & seed butter"),
    ("cat:snack-bars", r"\bbars?\b"),
    ("cat:ice-cream-frozen-desserts", r"ice cream|frozen dessert|frozen yogurt"),
    ("cat:sauces-condiments", r"ketchup|mustard|sauce|dressing|mayonnaise|condiment|gravy|marinade"),
    ("cat:dips-salsa", r"\bdips?\b|salsa"),
    ("cat:chips-pretzels", r"chips|crisps|pretzel|puffs|doodles"),
    ("cat:popcorn-nuts-seeds", r"popcorn|peanuts|seeds & related"),
    ("cat:crackers", r"cracker|savou?ry bakery"),
    ("cat:cookies", r"cookie|biscuits?/cookies"),
    ("cat:chocolate", r"chocolate"),
    ("cat:candy", r"candy|confection|chewing gum|mints"),
    ("cat:cakes-pastries", r"cake|pastr|croissant|muffin|sweet bakery|dessert"),
    ("cat:cereal", r"cereal|muesli|granola|oatmeal"),
    ("cat:other-snacks", r"snack"),
    ("cat:breads", r"bread|buns|rolls|dough|tortilla|bagel"),
    ("cat:yogurt", r"yogurt|yoghurt"),
    ("cat:cheese", r"cheese"),
    ("cat:cream-creamers", r"\bcream\b|milk additive|creamer"),
    ("cat:milk", r"\bmilk\b"),
    ("cat:butter-spreads", r"butter|spread"),
    ("cat:eggs", r"\beggs?\b"),
    ("cat:pizza", r"pizza"),
    ("cat:meals", r"dinner|entree|meal|sandwich|wrap|burrit|sushi|deli salad|sides|appetizer|"
                  r"prepared subs|combination"),
    ("cat:soups", r"soup|chili|stew"),
    ("cat:fish-seafood", r"fish|seafood|tuna|shellfish|salmon"),
    ("cat:meat-poultry", r"meat|poultry|chicken|turkey|bacon|sausage|hotdog|pepperoni|salami|"
                         r"cold cuts|patties|burgers|beef|pork"),
    ("cat:pasta-rice-grains", r"pasta|noodle|rice|grain|flour|corn meal|stuffing"),
    ("cat:pickles-olives", r"pickle|olive|relish"),
    ("cat:jams-honey-syrups", r"\bjam\b|jelly|honey|syrup|molasses|fruit spread"),
    ("cat:sugar-sweeteners", r"sugar|sweetener"),
    ("cat:baking", r"baking|mixes|decoration|topping|pudding|custard|gelatin|pie|crust|extract"),
    ("cat:spices-seasonings", r"spice|herb|seasoning|salt"),
    ("cat:oils", r"\boils?\b"),
    ("cat:juice", r"juice|nectar"),
    ("cat:soda", r"soda|soft drink"),
    ("cat:coffee-tea", r"coffee|\btea\b"),
    ("cat:water", r"\bwater\b"),
    ("cat:sports-protein-drinks", r"energy|protein|sport|recovery|meal replacement|supplement|"
                                  r"formula|weight control"),
    ("cat:alcohol", r"(?<!non )(?<!non-)alcohol(?!ic bev)|\bbeer\b|\bwine\b|spirits"),
    ("cat:other-drinks", r"drink|beverage"),
    ("cat:fruit-vegetables", r"fruit|vegetable|tomato|beans|lentil|potato|produce"),
]
_CATEGORY_RULES = [(cid, re.compile(rx, re.I)) for cid, rx in CATEGORY_RULES]
SNACK_CATEGORIES = {"cat:snack-bars", "cat:chips-pretzels", "cat:popcorn-nuts-seeds", "cat:crackers",
                    "cat:cookies", "cat:chocolate", "cat:candy", "cat:other-snacks"}
CATEGORY_IDS = [cid for cid, _ in CATEGORY_RULES] + ["cat:powders-mixes", "cat:snacks", "cat:other"]

# The product itself is a powder or a drink mix ("Whey Protein Powder", "Protein & Greens
# Drink Mix"), not a food that merely contains one ("Lollipop With Chili Pepper Powder").
_POWDER_OR_MIX = re.compile(r"\b(?:drink|shake|smoothie|beverage)\s+mix\b|\bpowder$", re.I)
_CONTAINS_POWDER = re.compile(r"\b(?:with|dusted|covered|filled|coated|dipped|in)\b", re.I)


def is_powder_or_mix(name):
    head = (name or "").split(",")[0].strip()
    return bool(_POWDER_OR_MIX.search(head)) and not (
        head.lower().endswith("powder") and _CONTAINS_POWDER.search(head))


def categories(usda_category, name=None):
    if is_powder_or_mix(name):  # USDA files some of these under Chocolate, Candy, Snacks
        return ["cat:powders-mixes"]
    text = (usda_category or "").replace("�", " ")
    cid = next((cid for cid, rx in _CATEGORY_RULES if rx.search(text)), "cat:other")
    return [cid, "cat:snacks"] if cid in SNACK_CATEGORIES else [cid]


_NAME_LABELS = [
    ("en:no-gluten", r"gluten[- ]free"),
    ("en:organic", r"\borganic\b"),
    ("en:vegan", r"\bvegan\b|plant[- ]based"),
    ("en:no-gmos", r"non[- ]gmo"),
    ("en:keto", r"\bketo\b"),
    ("en:no-added-sugar", r"no (?:added )?sugar(?:s)? added|no added sugar|unsweetened"),
]


def labels_from_text(name, items):
    found = {lid for lid, rx in _NAME_LABELS if re.search(rx, name or "", re.I)}
    top = [it for it in items if it["depth"] == 0]
    if top and sum("organic" in it["text"] for it in top) * 2 >= len(top):
        found.add("en:organic")
    return found


# --- Open Food Facts enrichment ------------------------------------------------

def gtin_key(code):
    digits = re.sub(r"\D", "", code or "")
    return digits.lstrip("0") if 8 <= len(digits) <= 14 else None


def _image_url(code, images):
    fronts = [i for i in images or [] if (i.get("key") or "").startswith("front_") and i.get("rev")]
    if not fronts:
        return None
    best = next((i for i in fronts if i["key"] == "front_en"), fronts[0])
    path = code if len(code) <= 8 else "/".join([code[:3], code[3:6], code[6:9], code[9:]])
    return f"https://images.openfoodfacts.org/images/products/{path}/{best['key']}.{best['rev']}.400.jpg"


def off_extras(parquet_path, keys):
    """Barcode key -> fields USDA lacks, for the products in ``keys``."""
    import pyarrow.parquet as pq

    out = {}
    pf = pq.ParquetFile(parquet_path)
    cols = ["code", "labels_tags", "images", "nova_group", "unique_scans_n", "allergens_tags"]
    for i in range(pf.num_row_groups):
        for row in pf.read_row_group(i, columns=cols).to_pylist():
            key = gtin_key(row["code"])
            if key in keys:
                out[key] = {
                    "labels": row["labels_tags"] or [],
                    "image_url": _image_url(row["code"], row["images"]),
                    "nova_group": row["nova_group"],
                    "unique_scans_n": row["unique_scans_n"] or 0,
                    "allergens": row["allergens_tags"] or [],
                }
    return out


# --- reading the CSV release ------------------------------------------------------

_BRANDED_COLS = ["fdc_id", "brand_owner", "brand_name", "subbrand_name", "gtin_upc", "ingredients",
                 "serving_size", "serving_size_unit", "household_serving_fulltext",
                 "branded_food_category", "package_weight", "market_country", "discontinued_date",
                 "available_date", "modified_date"]


def _read_csv(path, cols):
    import pyarrow as pa
    import pyarrow.csv as pacsv

    return pacsv.read_csv(path, convert_options=pacsv.ConvertOptions(
        include_columns=cols, column_types={c: pa.string() for c in cols}))


def iter_usda(directory, off_parquet=None, progress=print):
    """Yield one raw dict per current product: the latest record for each GTIN,
    discontinued products dropped, joined with food.csv names and nutrient amounts."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.csv as pacsv

    latest = {}
    for r in _read_csv(f"{directory}/branded_food.csv", _BRANDED_COLS).to_pylist():
        key = gtin_key(r["gtin_upc"])
        if not key:
            continue
        rank = (r["available_date"] or "", r["modified_date"] or "", int(r["fdc_id"]))
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, r)
    rows = {r["fdc_id"]: r for _, r in latest.values() if not r["discontinued_date"]}
    del latest
    progress(f"USDA: {len(rows)} current products")

    names = _read_csv(f"{directory}/food.csv", ["fdc_id", "description"])
    for fdc, desc in zip(names["fdc_id"].to_pylist(), names["description"].to_pylist()):
        if fdc in rows:
            rows[fdc]["description"] = desc

    wanted_ids = pa.array(sorted(NUTRIENT_IDS))
    reader = pacsv.open_csv(f"{directory}/food_nutrient.csv", convert_options=pacsv.ConvertOptions(
        include_columns=["fdc_id", "nutrient_id", "amount"],
        column_types={"fdc_id": pa.string(), "nutrient_id": pa.string(), "amount": pa.float64()}))
    for batch in reader:
        b = pa.Table.from_batches([batch])
        b = b.filter(pc.is_in(b["nutrient_id"], value_set=wanted_ids))
        for fdc, nid, amount in zip(b["fdc_id"].to_pylist(), b["nutrient_id"].to_pylist(),
                                    b["amount"].to_pylist()):
            row = rows.get(fdc)
            if row is not None and amount is not None:
                row.setdefault("nutrients", {})[nid] = amount
    progress("USDA: nutrients joined")

    extras = {}
    if off_parquet:
        extras = off_extras(off_parquet, {gtin_key(r["gtin_upc"]) for r in rows.values()})
        progress(f"Open Food Facts: extra fields for {len(extras)} products")
    for row in rows.values():
        row["off"] = extras.get(gtin_key(row["gtin_upc"]))
        yield row


# --- the canonical record -------------------------------------------------------

def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num(x):
    return f"{x:g}"


_SYMBOL_LEFTOVERS = re.compile(r"\[(?:tilde|caret)\]", re.I)  # ™ / ® lost in USDA's export


def _display(text):
    """USDA text is mostly ALL CAPS; show it in title case."""
    text = " ".join(_SYMBOL_LEFTOVERS.sub("", text or "").split())
    return string.capwords(text.lower()) if text.isupper() else text or None


def normalize(raw):
    off = raw.get("off") or {}
    digits = re.sub(r"\D", "", raw["gtin_upc"])
    code = digits.zfill(12) if len(digits) < 12 else digits
    name = _display(raw.get("description"))
    ing = ingredients(raw.get("ingredients"), raw.get("description") or "")
    nut = nutrition(raw)
    has_ingredients = bool(ing["items"])
    groups = classify_text([it["text"] for it in ing["items"]], has_ingredients)
    oils = sorted({it["id"] for it in ing["leaves"]
                   if it["id"] and it["id"].endswith("-oil") and it["text"] not in GENERIC_OIL_TEXT})
    kcal, protein = nut["per_100g"]["energy_kcal"], nut["per_100g"]["protein_g"]
    labels = sorted(labels_from_text(raw.get("description"), ing["items"]) | set(off.get("labels", [])))
    us = (raw.get("market_country") or "").strip().lower() in ("united states", "us", "usa")
    record = {
        "code": code,
        "name": name,
        "generic_name": None,
        "brand": _display(raw.get("brand_name") or raw.get("brand_owner")),
        "brand_owner": _display(raw.get("brand_owner")),
        "quantity": raw.get("package_weight") or None,
        "categories": categories(raw.get("branded_food_category"), raw.get("description")),
        "main_category": (raw.get("branded_food_category") or "").replace("�", "-").strip() or None,
        "labels": labels,
        "allergens": ing["allergens"] or off.get("allergens", []),
        "traces": ing["traces"],
        "countries": ["en:united-states"] if us else [],
        "stores": [],
        "nutrition": nut,
        "ingredients": ing,
        "additives": [],
        "scores": {"nova_group": off.get("nova_group"), "nutriscore_grade": None,
                   "nutriscore_score": None, "environmental_score_grade": None},
        "derived": {
            "groups": groups,
            "oils": oils,
            "protein_kcal_pct": round(protein * 4 / kcal * 100, 1) if kcal and protein is not None else None,
            "has_sweeteners": None,
            "has_non_nutritive_sweeteners": groups["artificial_sweeteners"]["status"] == "contains",
        },
        "quality": {
            "completeness": None,
            "unique_scans_n": off.get("unique_scans_n", 0),
            "popularity_key": 0,
            "data_quality_errors": [],
            "last_modified_t": raw.get("modified_date"),
            "obsolete": False,
        },
        "source": {"usda_fdc_id": raw["fdc_id"], "off": bool(off)},
        "image_url": off.get("image_url"),
        "url": f"https://fdc.nal.usda.gov/food-details/{raw['fdc_id']}/nutrients",
    }
    record["search_text"] = search_text(record)
    return record
