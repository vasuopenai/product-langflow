"""Turn one raw Open Food Facts product into the app's canonical record.

Accepts both source shapes:
  * JSONL / MongoDB / API products: ``nutriments`` is a flat dict
    (``proteins_100g``, ``proteins_serving``...), ``ingredients`` is a list,
    text fields are ``product_name`` / ``product_name_en``.
  * Hugging Face Parquet rows: ``nutriments`` is a list of
    ``{name, 100g, serving, ...}``, ``ingredients`` is a JSON string, and text
    fields are lists of ``{lang, text}`` (``lang == "main"`` for the main one).
"""

import json
import re

from .concepts import classify_all, GENERIC_OILS

# canonical name -> OFF nutrient id. Values are in grams except energy (kcal).
NUTRIENTS = {
    "energy_kcal": "energy-kcal",
    "protein_g": "proteins",
    "fat_g": "fat",
    "saturated_fat_g": "saturated-fat",
    "trans_fat_g": "trans-fat",
    "carbs_g": "carbohydrates",
    "sugars_g": "sugars",
    "added_sugars_g": "added-sugars",
    "fiber_g": "fiber",
    "salt_g": "salt",
    "sodium_g": "sodium",
    "cholesterol_g": "cholesterol",
}


def _float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(raw, field, lang="en"):
    """Read a possibly-translated text field in either source shape."""
    value = raw.get(field)
    if isinstance(value, list):  # Parquet: [{lang, text}, ...]
        by_lang = {v.get("lang"): v.get("text") for v in value if v}
        return by_lang.get(lang) or by_lang.get("main") or next(
            (t for t in by_lang.values() if t), None
        )
    return raw.get(f"{field}_{lang}") or value or None


def _nutriments(raw):
    """Return {off_nutrient_id: {"100g": x, "serving": y}}."""
    value = raw.get("nutriments") or {}
    out = {}
    if isinstance(value, list):  # Parquet
        for n in value:
            out[n["name"]] = {"100g": _float(n.get("100g")), "serving": _float(n.get("serving"))}
        return out
    for key, v in value.items():
        for basis in ("100g", "serving"):
            suffix = f"_{basis}"
            if key.endswith(suffix) and "_prepared" not in key:
                out.setdefault(key[: -len(suffix)], {})[basis] = _float(v)
    return out


def _ingredients_tree(raw):
    value = raw.get("ingredients")
    if isinstance(value, str):  # Parquet stores the tree as JSON
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    return value or []


def _flatten(tree, depth=0):
    for item in tree:
        children = item.get("ingredients")
        yield item, depth, not children
        if children:
            yield from _flatten(children, depth + 1)


def _serving_grams(raw):
    grams = _float(raw.get("serving_quantity"))
    unit = (raw.get("serving_quantity_unit") or "g").lower()
    if grams is None or unit not in ("g", "ml"):
        return None
    return grams


_MASS_NUTRIENTS = ("protein_g", "fat_g", "carbs_g", "sugars_g", "fiber_g", "salt_g")
_LABEL_AMOUNTS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:g|ml)\b", re.I)
# No \b before the unit: labels write "340ml" as often as "340 ml".
_LIQUID = re.compile(r"(?<![a-z])(?:ml|cl|l|fl\.?\s*oz|oza)\b", re.I)
MAX_SERVING_G = 1000
MAX_LIQUID_PROTEIN_100G = 25  # milk ~3.4, liquid egg white ~11, protein shakes ~6-10


def _is_liquid(raw):
    unit = (raw.get("serving_quantity_unit") or "").lower()
    return unit == "ml" or bool(_LIQUID.search(raw.get("serving_size") or ""))


def implausible_100g(raw, per_100g, alcohol_100g=None):
    """Reasons the per-100 g values can't be real. A common cause in Open Food
    Facts is per-100 g numbers typed into the per-serving fields, which then
    scale up to e.g. 150 g protein per 100 g."""
    reasons = [f"{k} {per_100g[k]} > 100 g" for k in _MASS_NUTRIENTS
               if per_100g.get(k) is not None and per_100g[k] > 100]
    protein, fat, carbs = (per_100g.get(k) for k in ("protein_g", "fat_g", "carbs_g"))
    if sum(m or 0 for m in (protein, fat, carbs)) > 105:
        reasons.append("protein + fat + carbs > 105 g")
    kcal = per_100g.get("energy_kcal")
    if (kcal or 0) > 950:
        reasons.append(f"energy {kcal} kcal > 950")
    if kcal is not None and None not in (protein, fat, carbs):
        # Atwater factors. Only protein and fat bound energy from below: sugar
        # alcohols, allulose and fibre make low-calorie carbs legitimate.
        # OFF records alcohol in % vol: 0.789 g/ml ethanol at 7 kcal/g.
        expected = 4 * protein + 4 * carbs + 9 * fat + 5.5 * (alcohol_100g or 0)
        if kcal > 1.5 * expected + 50:
            reasons.append(f"energy {round(kcal)} kcal far above macros ({round(expected)} kcal)")
        elif kcal < 0.7 * (4 * protein + 9 * fat) - 30:
            reasons.append(f"energy {round(kcal)} kcal far below protein + fat")
    if protein is not None and protein > MAX_LIQUID_PROTEIN_100G and _is_liquid(raw):
        reasons.append(f"liquid with {protein} g protein per 100 ml")
    return reasons


def implausible_serving(raw, per_serving, serving_g):
    """Reasons the per-serving values can't be trusted (the per-100 g ones may still be fine)."""
    if not serving_g:
        return []
    reasons = []
    if serving_g >= MAX_SERVING_G:
        reasons.append(f"serving {serving_g} g is a package, not a serving")
    on_label = [float(x) for x in _LABEL_AMOUNTS.findall(raw.get("serving_size") or "") if float(x) > 0]
    if on_label and not any(0.5 <= serving_g / x <= 2 for x in on_label):
        reasons.append(f"serving_quantity {serving_g} g disagrees with label {raw.get('serving_size')!r}")
    mass = sum(per_serving.get(k) or 0 for k in ("protein_g", "fat_g", "carbs_g"))
    if mass > serving_g * 1.05:
        reasons.append(f"protein + fat + carbs {round(mass, 1)} g > serving {serving_g} g")
    return reasons


def nutrition(raw):
    nutr = _nutriments(raw)
    serving_g = _serving_grams(raw)
    per_100g, per_serving = {}, {}
    for name, off_id in NUTRIENTS.items():
        values = nutr.get(off_id, {})
        v100, vserv = values.get("100g"), values.get("serving")
        if off_id == "energy-kcal" and v100 is None:
            kj = nutr.get("energy", {}).get("100g")
            v100 = round(kj / 4.184, 1) if kj is not None else None
        if vserv is None and v100 is not None and serving_g:
            vserv = round(v100 * serving_g / 100, 2)
        per_100g[name] = v100
        per_serving[name] = vserv
    # Bad numbers become unknown, so numeric filters exclude the product instead
    # of ranking it first.
    implausible = implausible_100g(raw, per_100g, nutr.get("alcohol", {}).get("100g"))
    if implausible:
        per_100g = dict.fromkeys(per_100g)
        per_serving = dict.fromkeys(per_serving)
    else:
        implausible = implausible_serving(raw, per_serving, serving_g)
        if implausible:
            per_serving = dict.fromkeys(per_serving)
            serving_g = None
    # Sodium in mg reads more naturally in US-style questions.
    for d in (per_100g, per_serving):
        d["sodium_mg"] = round(d["sodium_g"] * 1000, 1) if d["sodium_g"] is not None else None
    return {
        "basis_on_label": raw.get("nutrition_data_per"),
        "serving_size": raw.get("serving_size"),
        "serving_g": serving_g,
        "per_100g": per_100g,
        "per_serving": per_serving,
        "no_nutrition_data": raw.get("no_nutrition_data") in ("on", True),
        "implausible": implausible,
    }


def ingredients(raw):
    tree = _ingredients_tree(raw)
    text = _text(raw, "ingredients_text")
    items, leaves = [], []
    for item, depth, is_leaf in _flatten(tree):
        entry = {
            "id": item.get("id"),
            "text": item.get("text"),
            "depth": depth,
            "rank": len([i for i in items if i["depth"] == depth]) + 1 if depth == 0 else None,
            # percent: declared on the label; the others are OFF's estimates
            "percent": _float(item.get("percent")),
            "percent_min": _float(item.get("percent_min")),
            "percent_max": _float(item.get("percent_max")),
            "percent_estimate": _float(item.get("percent_estimate")),
            "is_in_taxonomy": item.get("is_in_taxonomy"),
        }
        items.append(entry)
        if is_leaf:
            leaves.append(entry)
    analysis = set(raw.get("ingredients_analysis_tags") or [])

    def tri(yes, no):
        return True if yes in analysis else False if no in analysis else None

    return {
        "text": text,
        "count_top_level": len(tree) if tree else None,
        "count_total": len(leaves) if tree else None,
        "items": items,
        "leaves": leaves,
        "first": tree[0].get("id") if tree else None,
        "tags": raw.get("ingredients_tags") or [],
        "vegan": tri("en:vegan", "en:non-vegan"),
        "vegetarian": tri("en:vegetarian", "en:non-vegetarian"),
        "palm_oil_free": tri("en:palm-oil-free", "en:palm-oil"),
    }


def _first_image(raw):
    return raw.get("image_front_url") or raw.get("image_url")


def normalize(raw):
    """Map one OFF product (JSONL or Parquet shape) to the canonical record."""
    code = str(raw.get("code") or raw.get("_id") or "")
    ing = ingredients(raw)
    nut = nutrition(raw)
    additives = raw.get("additives_tags") or []
    has_ingredients = bool(ing["items"] or ing["text"])
    groups = classify_all(ing["tags"], additives, ing["leaves"], has_ingredients)
    oils = sorted({
        leaf["id"] for leaf in ing["leaves"]
        if leaf["id"] and leaf["id"].endswith("-oil") and leaf["id"] not in GENERIC_OILS
    })

    kcal = nut["per_100g"]["energy_kcal"]
    protein = nut["per_100g"]["protein_g"]
    protein_kcal_pct = round(protein * 4 / kcal * 100, 1) if kcal and protein is not None else None

    record = {
        "code": code,
        "name": _text(raw, "product_name"),
        "generic_name": _text(raw, "generic_name"),
        "brand": (raw.get("brands") or "").split(",")[0].strip() or None,
        "quantity": raw.get("quantity"),
        "categories": raw.get("categories_tags") or [],
        "main_category": raw.get("compared_to_category")
        or ((raw.get("categories_tags") or [None])[-1]),
        "labels": raw.get("labels_tags") or [],
        "allergens": raw.get("allergens_tags") or [],
        "traces": raw.get("traces_tags") or [],
        "countries": raw.get("countries_tags") or [],
        "stores": raw.get("stores_tags") or [],
        "nutrition": nut,
        "ingredients": ing,
        "additives": additives,
        "scores": {
            "nova_group": raw.get("nova_group"),
            "nutriscore_grade": raw.get("nutriscore_grade"),
            "nutriscore_score": raw.get("nutriscore_score"),
            "environmental_score_grade": raw.get("environmental_score_grade")
            or raw.get("ecoscore_grade"),
        },
        "derived": {
            "groups": groups,
            "oils": oils,
            "protein_kcal_pct": protein_kcal_pct,
            "has_sweeteners": bool(raw.get("with_sweeteners")),
            "has_non_nutritive_sweeteners": bool(raw.get("with_non_nutritive_sweeteners")),
        },
        "quality": {
            "completeness": _float(raw.get("completeness")),
            "unique_scans_n": raw.get("unique_scans_n") or 0,
            "popularity_key": raw.get("popularity_key") or 0,
            "data_quality_errors": raw.get("data_quality_errors_tags") or [],
            "last_modified_t": raw.get("last_modified_t"),
            "obsolete": bool(raw.get("obsolete")),
        },
        "image_url": _first_image(raw),
        "url": f"https://world.openfoodfacts.org/product/{code}",
    }
    record["search_text"] = search_text(record)
    return record


def _tag_label(tag):
    return tag.split(":", 1)[-1].replace("-", " ")


def search_text(p):
    """Compact natural-language document to embed for semantic matching.

    Hard constraints (grams, ingredient groups, counts) are answered by filters, not
    by the embedding, so this stays short and descriptive.
    """
    parts = [p["name"] or ""]
    if p["brand"]:
        parts.append(f"by {p['brand']}")
    if p["generic_name"]:
        parts.append(f"({p['generic_name']})")
    lines = [" ".join(parts)]
    if p["main_category"]:
        lines.append(f"Category: {_tag_label(p['main_category'])}.")
    if p["labels"]:
        lines.append("Labels: " + ", ".join(_tag_label(t) for t in p["labels"][:8]) + ".")
    if p["ingredients"]["text"]:
        lines.append("Ingredients: " + p["ingredients"]["text"][:600])
    if p["derived"]["oils"]:
        lines.append("Oils: " + ", ".join(_tag_label(t) for t in p["derived"]["oils"]) + ".")
    return " ".join(lines)


def to_flat(p):
    """Flat, filterable view: one SQL row or one Pinecone metadata dict."""
    flat = {
        "code": p["code"],
        "name": p["name"],
        "brand": p["brand"],
        "main_category": p["main_category"],
        "serving_g": p["nutrition"]["serving_g"],
        "ingredients_n": p["ingredients"]["count_total"],
        "ingredients_top_level_n": p["ingredients"]["count_top_level"],
        "protein_kcal_pct": p["derived"]["protein_kcal_pct"],
        "nova_group": p["scores"]["nova_group"],
        "nutriscore_grade": p["scores"]["nutriscore_grade"],
        "vegan": p["ingredients"]["vegan"],
        "unique_scans_n": p["quality"]["unique_scans_n"],
        "completeness": p["quality"]["completeness"],
        "image_url": p["image_url"],
    }
    for basis, values in (("100g", p["nutrition"]["per_100g"]), ("serving", p["nutrition"]["per_serving"])):
        for name, value in values.items():
            flat[f"{name}_{basis}"] = value
    flat["category_tags"] = p["categories"]
    flat["label_tags"] = p["labels"]
    flat["allergen_tags"] = p["allergens"] + p["traces"]
    flat["country_tags"] = p["countries"]
    flat["ingredient_tags"] = p["ingredients"]["tags"]
    flat["oil_tags"] = p["derived"]["oils"]
    flat["additive_tags"] = p["additives"]
    groups = p["derived"]["groups"]
    # "free of X" needs status none; "contains X" needs status contains.
    flat["free_of"] = [g for g, v in groups.items() if v["status"] == "none"]
    flat["contains_groups"] = [g for g, v in groups.items() if v["status"] == "contains"]
    flat["first_ingredient"] = p["ingredients"]["first"]
    return flat


def ingredient_amounts(p):
    """Per-ingredient amounts for "at least 30% almonds" style questions.

    One row per distinct ingredient id at any depth of the label. If an id
    appears more than once (e.g. sugar in two sub-recipes) amounts are summed,
    except when it is nested inside itself ("cocoa & cocoa butter (70% cocoa)"),
    which restates the parent rather than adding to it. A total above 100%
    is a parsing error and becomes unknown.
    grams are derived from percent and the serving / 100 g basis.
    """
    serving_g = p["nutrition"]["serving_g"]
    rows = {}
    ancestors = []  # ids of the current item's parents, by depth
    for item in p["ingredients"]["items"]:
        del ancestors[item["depth"]:]
        restated = item["id"] in ancestors
        ancestors.append(item["id"])
        if not item["id"] or restated:
            continue
        row = rows.setdefault(item["id"], {
            "ingredient": item["id"], "rank": item["rank"], "declared": False,
            "percent": None, "percent_min": None, "percent_max": None,
        })
        best = item["percent"] if item["percent"] is not None else item["percent_estimate"]
        row["declared"] |= item["percent"] is not None
        for key, value in (("percent", best), ("percent_min", item["percent_min"]),
                           ("percent_max", item["percent_max"])):
            if value is not None:
                row[key] = round((row[key] or 0) + value, 2)
    for row in rows.values():
        if row["percent"] is not None and row["percent"] > 100:
            row["percent"], row["declared"] = None, False
        pct = row["percent"]
        row["grams_per_100g"] = pct
        row["grams_per_serving"] = round(pct * serving_g / 100, 2) if pct is not None and serving_g else None
    return list(rows.values())


def to_pinecone_metadata(p):
    """Pinecone rejects null metadata values, so drop missing fields."""
    meta = {k: v for k, v in to_flat(p).items() if v is not None and v != []}
    meta["text"] = p["search_text"]
    return meta
