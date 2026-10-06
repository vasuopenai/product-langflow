"""Ingredient concept groups: answer "without X" / "with X" for any X.

Users ask for things OFF does not tag directly ("no seed oils", "no artificial
sweeteners", "no gums", "no added sugar"). Each group below is a set of Open
Food Facts taxonomy ids. Adding a new group is a data change, not a code change.

Matching rules:
  * ``ids`` are matched against ``ingredients_tags`` (which already contains
    every taxonomy ancestor, so "en:refined-rapeseed-oil" carries
    "en:rapeseed-oil") and ``additives_tags`` (``en:e955`` etc.).
  * ``possible_ids`` are matched only against the *leaf* ingredients actually
    written on the label (an unspecified "vegetable oil" could be anything;
    every specific oil also has "en:vegetable-oil" as an ancestor, so ancestors
    must not trigger this).
  * ``unrecognized_suffix``: a leaf OFF could not map to its taxonomy whose id
    ends with this suffix makes the group "possible" (e.g. an unknown oil).

Per product and group the status is:
  contains - a matching ingredient/additive is listed
  possible - something ambiguous could be a match
  none     - ingredients are known and nothing matched
  unknown  - no ingredient data, so nothing can be claimed

IDs were checked against taxonomies/food/ingredients.txt and
taxonomies/additives.txt in openfoodfacts/openfoodfacts-server.
"""

GENERIC_OILS = {
    "en:vegetable-oil", "en:vegetable-oil-and-fat", "en:vegetable-fat",
    "en:oil", "en:oil-and-fat", "en:fat",
}

GROUPS = {
    "seed_oils": {
        "label": "industrial seed oils",
        "ids": {
            "en:canola-oil", "en:rapeseed-oil", "en:corn-oil", "en:cottonseed-oil",
            "en:soya-oil", "en:sunflower-oil", "en:safflower-oil", "en:grape-seed-oil",
            "en:rice-bran-oil",
        },
        # E471/E472e are usually made from seed oils; listing them as possible
        # keeps "seed-oil free" strict.
        "possible_ids": GENERIC_OILS | {"en:e471", "en:e472e"},
        "unrecognized_suffix": "-oil",
    },
    "palm_oil": {
        "label": "palm oil",
        "ids": {"en:palm-oil", "en:palm-kernel-oil", "en:palm-oil-and-fat"},
        "possible_ids": GENERIC_OILS,
        "unrecognized_suffix": "-oil",
    },
    "hydrogenated_oils": {
        "label": "hydrogenated oils",
        "ids": {"en:hydrogenated-vegetable-oil", "en:partially-hydrogenated-vegetable-oil"},
        "id_substrings": ["hydrogenated"],
    },
    "added_sugars": {
        "label": "added sugars and syrups",
        "ids": {
            "en:sugar", "en:added-sugar", "en:cane-sugar", "en:brown-sugar", "en:dextrose",
            "en:glucose-syrup", "en:corn-syrup", "en:high-fructose-corn-syrup",
            "en:maple-syrup", "en:honey",
        },
        "id_substrings": ["-syrup"],
    },
    "artificial_sweeteners": {
        "label": "artificial sweeteners",
        # acesulfame K, aspartame, saccharin, sucralose, neotame, aspartame-acesulfame salt
        "ids": {"en:e950", "en:e951", "en:e954", "en:e955", "en:e961", "en:e962"},
    },
    "sugar_alcohols": {
        "label": "sugar alcohols",
        # sorbitol, maltitol, lactitol, xylitol, erythritol
        "ids": {"en:e420", "en:e965", "en:e966", "en:e967", "en:e968"},
    },
    "gums_thickeners": {
        "label": "gums and thickeners",
        # carrageenan, locust bean, guar, acacia, xanthan, gellan, cellulose gum
        "ids": {"en:e407", "en:e410", "en:e412", "en:e414", "en:e415", "en:e418", "en:e466"},
    },
    "artificial_colors": {
        "label": "artificial colors",
        # tartrazine, sunset yellow, azorubine, ponceau 4R, allura red, brilliant blue
        "ids": {"en:e102", "en:e110", "en:e122", "en:e124", "en:e129", "en:e133"},
    },
    "artificial_flavors": {
        "label": "artificial flavors",
        "ids": {"en:artificial-flavouring"},
        "possible_ids": {"en:flavouring"},
    },
    "natural_flavors": {
        "label": "natural flavors",
        "ids": {"en:natural-flavouring"},
        "possible_ids": {"en:flavouring"},
    },
    "synthetic_preservatives": {
        "label": "synthetic preservatives",
        # potassium sorbate, sodium benzoate, sodium/potassium nitrite, BHA, BHT
        "ids": {"en:e202", "en:e211", "en:e250", "en:e249", "en:e320", "en:e321"},
    },
}


def _label(tag):
    return tag.split(":", 1)[-1].replace("-", " ")


def classify_group(group, tags, leaves, has_ingredients):
    contains = sorted(
        t for t in tags
        if t in group["ids"] or any(s in t for s in group.get("id_substrings", ()))
    )
    possible = []
    for leaf in leaves:
        lid = leaf.get("id") or ""
        suffix = group.get("unrecognized_suffix")
        if lid in group.get("possible_ids", ()) or (
            suffix and lid.endswith(suffix) and leaf.get("is_in_taxonomy") == 0
        ):
            possible.append(leaf.get("text") or _label(lid))
    possible += [_label(t) for t in tags if t in group.get("possible_ids", ()) and t.startswith("en:e")]

    if contains:
        status = "contains"
    elif possible:
        status = "possible"
    elif has_ingredients:
        status = "none"
    else:
        status = "unknown"
    return {"status": status, "matches": [_label(t) for t in contains], "possible": possible}


def classify_all(ingredient_tags, additives_tags, leaves, has_ingredients, groups=GROUPS):
    tags = set(ingredient_tags or []) | set(additives_tags or [])
    return {
        name: classify_group(group, tags, leaves, has_ingredients)
        for name, group in groups.items()
    }
