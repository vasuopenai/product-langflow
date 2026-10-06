"""Generate sample_products.jsonl: synthetic products in OFF JSONL shape.

Brands are made up. Field names, tag formats and ingredient trees follow the
real OFF product schema so the pipeline can be tested without the full dump.
Run: python tests/fixtures/make_sample.py
"""

import json
from pathlib import Path

# id -> ancestors, as in OFF's ingredients taxonomy (ingredients_tags includes them)
ANCESTORS = {
    "en:whey-protein-isolate": ["en:milk-proteins", "en:dairy"],
    "en:milk-protein-isolate": ["en:milk-proteins", "en:dairy"],
    "en:sunflower-oil": ["en:oil-and-fat", "en:vegetable-oil-and-fat", "en:vegetable-oil"],
    "en:high-oleic-sunflower-oil": ["en:oil-and-fat", "en:vegetable-oil-and-fat", "en:vegetable-oil", "en:sunflower-oil"],
    "en:avocado-oil": ["en:oil-and-fat", "en:vegetable-oil-and-fat", "en:vegetable-oil"],
    "en:vegetable-oil": ["en:oil-and-fat", "en:vegetable-oil-and-fat"],
    "en:sea-salt": ["en:salt"],
    "en:soya-protein-isolate": ["en:soya", "en:vegetable-protein"],
    "en:cocoa-mass": ["en:cocoa"],
    "en:cocoa-butter": ["en:cocoa", "en:oil-and-fat", "en:vegetable-oil-and-fat", "en:vegetable-fat"],
    "en:cane-sugar": ["en:sugar"],
}


def ing(id_, text, pct=None, children=None, declared=None, **extra):
    d = {"id": id_, "text": text, "is_in_taxonomy": 1, **extra}
    if pct is not None:
        d["percent_estimate"] = pct
    if declared is not None:
        d["percent"] = d["percent_estimate"] = d["percent_min"] = d["percent_max"] = declared
    if children:
        d["ingredients"] = children
    return d


def product(code, name, brand, categories, ingredients, nutr_100g, serving_g, **extra):
    tags, stack = [], list(ingredients)
    while stack:
        i = stack.pop(0)
        for t in [i["id"], *ANCESTORS.get(i["id"], [])]:
            if t not in tags:
                tags.append(t)
        stack.extend(i.get("ingredients", []))
    nutriments = {}
    for k, v in nutr_100g.items():
        nutriments[f"{k}_100g"] = v
        nutriments[f"{k}_serving"] = round(v * serving_g / 100, 2)
    p = {
        "code": code,
        "product_name": name,
        "brands": brand,
        "categories_tags": categories,
        "countries_tags": ["en:united-states"],
        "ingredients": ingredients,
        "ingredients_tags": tags,
        "ingredients_text": ", ".join(i["text"] for i in ingredients),
        "nutriments": nutriments,
        "nutrition_data_per": "serving",
        "serving_size": f"1 bar ({serving_g} g)" if "en:bars" in categories else f"1 oz ({serving_g} g)",
        "serving_quantity": serving_g,
        "completeness": 0.8,
        "unique_scans_n": 10,
    }
    if not ingredients:
        del p["ingredients"], p["ingredients_text"]
    p.update(extra)
    return p


BAR = ["en:snacks", "en:sweet-snacks", "en:bars", "en:protein-bars"]
CHIPS = ["en:plant-based-foods-and-beverages", "en:snacks", "en:salty-snacks", "en:appetizers",
         "en:chips-and-fries", "en:crisps", "en:potato-crisps"]
CHOC = ["en:snacks", "en:sweet-snacks", "en:cocoa-and-its-products", "en:chocolates", "en:dark-chocolates"]
WHEY = ing("en:milk-protein-blend", "protein blend", 35, [
    ing("en:whey-protein-isolate", "whey protein isolate"),
    ing("en:milk-protein-isolate", "milk protein isolate"),
])

PRODUCTS = [
    product("0000000000031", "85% Dark Chocolate", "Ridge Test Chocolate", CHOC,
            [ing("en:cocoa-mass", "cocoa mass", declared=78),
             ing("en:cocoa-butter", "cocoa butter", declared=7),
             ing("en:cane-sugar", "cane sugar", 15)],
            {"energy-kcal": 600, "proteins": 11, "fat": 50, "sugars": 15}, 40),
    product("0000000000032", "Milk Chocolate with Hazelnuts", "Ridge Test Chocolate", CHOC[:-1] + ["en:milk-chocolates"],
            [ing("en:sugar", "sugar", 40), ing("en:cocoa-butter", "cocoa butter", 20),
             ing("en:whole-milk-powder", "whole milk powder", 18),
             ing("en:hazelnut", "hazelnuts", declared=12), ing("en:cocoa-mass", "cocoa mass", 9),
             ing("en:e322", "lecithin", 1), ing("en:e955", "sucralose", 0)],
            {"energy-kcal": 560, "proteins": 7, "fat": 35, "sugars": 50}, 40,
            additives_tags=["en:e322", "en:e955"]),
    product("0000000000011", "Chocolate Almond Protein Bar", "Trailhead Test Co", BAR,
            [WHEY, ing("en:almond", "almonds", 25), ing("en:date", "dates", 20),
             ing("en:cocoa", "cocoa", 10), ing("en:sea-salt", "sea salt", 1)],
            {"energy-kcal": 380, "proteins": 35, "fat": 15, "sugars": 12, "fiber": 8, "sodium": 0.3}, 60,
            labels_tags=["en:gluten-free"], allergens_tags=["en:milk", "en:nuts"],
            ingredients_analysis_tags=["en:palm-oil-free", "en:non-vegan", "en:vegetarian"]),
    product("0000000000012", "Peanut Butter Crunch Protein Bar", "Trailhead Test Co", BAR,
            [WHEY, ing("en:peanut", "peanuts", 20), ing("en:sunflower-oil", "sunflower oil", 8),
             ing("en:sugar", "sugar", 10)],
            {"energy-kcal": 400, "proteins": 33.3, "fat": 18, "sugars": 15, "sodium": 0.4}, 60,
            allergens_tags=["en:milk", "en:peanuts"]),
    product("0000000000013", "Oat Date Bar", "Meadow Test Foods", BAR,
            [ing("en:oat", "oats", 40), ing("en:date", "dates", 35), ing("en:almond", "almonds", 25)],
            {"energy-kcal": 410, "proteins": 12, "fat": 14, "sugars": 25}, 50),
    product("0000000000014", "Vanilla Crisp Protein Bar", "Meadow Test Foods", BAR,
            [ing("en:soya-protein-isolate", "soy protein isolate", 40),
             ing("en:vegetable-oil", "vegetable oil", 10), ing("en:rice", "crisp rice", 20)],
            {"energy-kcal": 360, "proteins": 36.7, "fat": 12, "sugars": 6}, 60),
    product("0000000000015", "Mystery Protein Bar", "Unknown Test Brand", BAR, [],
            {"energy-kcal": 370, "proteins": 40, "fat": 10}, 62.5),
    product("0000000000021", "Avocado Oil Sea Salt Potato Chips", "Coastline Test Snacks", CHIPS,
            [ing("en:potato", "potatoes", 70), ing("en:avocado-oil", "avocado oil", 29),
             ing("en:sea-salt", "sea salt", 1)],
            {"energy-kcal": 535, "proteins": 7, "fat": 32, "sugars": 0.5, "sodium": 0.39}, 28,
            unique_scans_n=50),
    product("0000000000022", "Avocado Oil Ranch Potato Chips", "Coastline Test Snacks", CHIPS,
            [ing("en:potato", "potatoes"), ing("en:avocado-oil", "avocado oil"),
             ing("en:sea-salt", "sea salt"), ing("en:onion-powder", "onion powder"),
             ing("en:garlic-powder", "garlic powder"), ing("en:sugar", "sugar"),
             ing("en:yeast-extract", "yeast extract"), ing("en:natural-flavouring", "natural flavors")],
            {"energy-kcal": 530, "proteins": 7, "fat": 31, "sugars": 2, "sodium": 0.6}, 28),
    product("0000000000023", "Classic Potato Chips", "Crunchy Test Co", CHIPS,
            [ing("en:potato", "potatoes"), ing("en:sunflower-oil", "sunflower oil"), ing("en:salt", "salt")],
            {"energy-kcal": 540, "proteins": 6, "fat": 34, "sodium": 0.55}, 28, unique_scans_n=500),
    product("0000000000024", "Kettle Potato Chips", "Crunchy Test Co", CHIPS,
            [ing("en:potato", "potatoes"), ing("en:high-oleic-sunflower-oil", "high oleic sunflower oil"),
             ing("en:salt", "salt")],
            {"energy-kcal": 520, "proteins": 7, "fat": 30, "sodium": 0.5}, 28),
]

if __name__ == "__main__":
    out = Path(__file__).with_name("sample_products.jsonl")
    out.write_text("".join(json.dumps(p) + "\n" for p in PRODUCTS))
    print(f"wrote {len(PRODUCTS)} products to {out}")
