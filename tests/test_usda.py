"""USDA Branded Foods source: parser, mapping, loader, and Postgres search."""

import os
from pathlib import Path

import pytest

from off_products.concepts import classify_text
from off_products.pg import iter_records
from off_products.usda import categories, ingredient_id, ingredients, labels_from_text, nutrition

SAMPLE = str(Path(__file__).parent / "fixtures" / "usda")


def ids(text, name=""):
    return [(i["id"], i["depth"]) for i in ingredients(text, name)["items"]]


def test_nested_ingredients_minor_clause_and_allergen_statement():
    ing = ingredients(
        "ENRICHED WHEAT FLOUR (WHEAT FLOUR, NIACIN), WATER, SUGAR, CONTAINS 2% OR LESS OF: SALT, "
        "CALCIUM PROPIONATE (PRESERVATIVE). CONTAINS: WHEAT, MILK.")
    assert [(i["id"], i["depth"]) for i in ing["items"]] == [
        ("en:wheat-flour", 0), ("en:wheat-flour", 1), ("en:niacin", 1), ("en:water", 0),
        ("en:sugar", 0), ("en:salt", 0), ("en:calcium-propionate", 0)]
    salt = ing["items"][5]
    assert salt["minor"] and salt["percent_max"] == 2 and salt["percent_estimate"] == 1
    assert ing["allergens"] == ["en:gluten", "en:milk"]
    assert ing["first"] == "en:wheat-flour" and ing["count_top_level"] == 5


def test_leading_conjunctions_and_nutrient_amounts_are_not_ingredients():
    assert ids("POTATOES, AVOCADO OIL, AND HIMALAYAN SEA SALT")[-1] == ("en:himalayan-sea-salt", 0)
    assert ids("WHEAT FLOUR (CONTAINS NIACIN 75 MG/KG), RED 40") == [
        ("en:wheat-flour", 0), ("en:niacin", 1), ("en:red-40", 0)]


def test_qualifiers_plurals_synonyms_and_and_or():
    assert ingredient_id("EXPELLER PRESSED AVOCADO OIL") == "en:avocado-oil"
    assert ingredient_id("DRY ROASTED ALMONDS") == "en:almond"
    assert ingredient_id("CASHEW NUTS") == "en:cashew"
    assert ingredient_id("POTATOES") == "en:potato"
    assert ingredient_id("CHOCOLATE LIQUOR") == "en:cocoa"
    assert ids("CANOLA AND/OR SUNFLOWER OIL") == [("en:canola-oil", 0), ("en:sunflower-oil", 0)]
    assert ids("MOLASSES, SOY LECITHIN [AN EMULSIFIER]") == [("en:molasses", 0), ("en:soy-lecithin", 0)]


def test_declared_percentages_and_cocoa_from_the_name():
    ing = ingredients("CHOCOLATE LIQUOR, SUGAR, COCOA BUTTER, SALT (1%)", "85% DARK CHOCOLATE")
    cocoa, *_, salt = ing["items"]
    assert cocoa["id"] == "en:cocoa" and cocoa["percent"] == 85
    assert salt["percent"] == 1


def test_groups_from_label_text():
    names = [i["text"] for i in ingredients(
        "ALMONDS, HONEY, VEGETABLE OIL, NATURAL AND ARTIFICIAL FLAVORS, SUCRALOSE, RED 40, "
        "XANTHAN GUM, POTASSIUM SORBATE")["items"]]
    g = {k: v["status"] for k, v in classify_text(names, True).items()}
    assert g["added_sugars"] == g["artificial_sweeteners"] == g["artificial_colors"] == "contains"
    assert g["natural_flavors"] == g["artificial_flavors"] == "contains"
    assert g["gums_thickeners"] == g["synthetic_preservatives"] == "contains"
    assert g["seed_oils"] == g["palm_oil"] == "possible"  # unspecified vegetable oil
    assert g["sugar_alcohols"] == "none"
    assert classify_text([], False)["seed_oils"]["status"] == "unknown"


def test_product_key_merges_spelling_variants_but_not_flavors():
    from off_products.pg import _product_key as key
    rec = lambda name, brand="Ghirardelli": {"code": "1", "name": name, "brand": brand}
    assert key(rec("Intense Dark 72% Cacao Dark Chocolate, Intense Dark")) == \
        key(rec("Intense Dark 72% Cacao Dark Chocolate, Intense Dark 72% Cacao"))
    assert key(rec("Big100 Colossal Bar, Maple Bacon", "Met-rx")) == \
        key(rec("Big 100 Colossal Bar, Maple Bacon", "Met-rx"))
    assert key(rec("Super Cookie Crunch Meal Replacement Bar", "Met-rx")) == \
        key(rec("Meal Replacement Bar, Super Cookie Crunch", "Met-rx"))
    assert key(rec("Potato Chips, Sea Salt", "Kettle")) != key(rec("Potato Chips, Himalayan Salt", "Kettle"))
    assert key(rec("Bar", "Kettle")) != key(rec("Bar", "Clif"))
    assert key(rec("Vanilla Almond Protein Bars, Vanilla Almond", "Clif")) == \
        key(rec("Vanilla Almond Protein Bar, Vanilla; Almond", "Clif"))


def test_recipe_key_catches_same_product_under_two_names():
    from off_products.pg import _recipe_key as key
    rec = lambda name, protein=28: {
        "code": "1", "name": name, "brand": "Met-rx",
        "ingredients": {"text": "PROTEIN BLEND (SOY PROTEIN ISOLATE), HONEY, DATES."},
        "nutrition": {"per_100g": {"energy_kcal": 375, "protein_g": protein, "fat_g": 10, "carbs_g": 45}}}
    assert key(rec("Brownie Bars, Chocolate Chip Blondie")) == \
        key(rec("High Protein Brownie Bars, Chocolate Chip Blondie"))
    assert key(rec("Brownie Bars", protein=35)) != key(rec("Brownie Bars"))  # reformulated
    assert key({"code": "2", "name": "x", "brand": None, "ingredients": {"text": "A"}}) is None


def test_symbol_leftovers_are_removed_from_names():
    from off_products.usda import _display
    assert _display("HIGH PROTEIN BROWNIE[TILDE] BARS") == "High Protein Brownie Bars"


def test_category_mapping():
    assert categories("Chips, Pretzels & Snacks") == ["cat:chips-pretzels", "cat:snacks"]
    assert categories("Snack, Energy & Granola Bars") == ["cat:snack-bars", "cat:snacks"]
    assert categories("Ketchup, Mustard, BBQ & Cheese Sauce") == ["cat:sauces-condiments"]
    assert categories("Non Alcoholic Beverages � Ready to Drink") == ["cat:other-drinks"]
    assert categories(None) == ["cat:other"]


def test_labels_from_name_and_organic_ingredients():
    items = ingredients("ORGANIC OATS, ORGANIC HONEY, SALT")["items"]
    assert labels_from_text("GLUTEN FREE GRANOLA", items) == {"en:no-gluten", "en:organic"}


def test_nutrition_per_serving_and_units():
    n = nutrition({"nutrients": {"1008": 400, "1003": 35, "1004": 15, "1005": 40, "1093": 250},
                   "serving_size": "60", "serving_size_unit": "GRM",
                   "household_serving_fulltext": "1 BAR"})
    assert n["per_serving"]["protein_g"] == 21 and n["per_100g"]["sodium_mg"] == 250
    assert n["per_100g"]["salt_g"] == 0.625 and n["serving_size"] == "1 BAR (60 g)"


def test_loader_keeps_latest_current_record_per_gtin():
    records = {r["code"]: r for r in iter_records(SAMPLE, source="usda", progress=lambda *_: None)}
    assert len(records) == 6  # old bar record replaced, discontinued crackers dropped
    bar = records["0098765432109"]
    assert bar["name"] == "Chocolate Almond Protein Bar" and bar["brand"] == "Trailhead"
    assert bar["allergens"] == ["en:milk", "en:nuts"]
    assert records["022222222222"]["nutrition"]["implausible"]  # 150 g protein per 100 g


psycopg = pytest.importorskip("psycopg")
DSN = os.getenv("OFF_TEST_DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="set OFF_TEST_DATABASE_URL to run Postgres tests")
def test_usda_load_and_search():
    from off_products.pg import HashEmbedder, load, search
    from off_products.query import QuerySpec

    with psycopg.connect(DSN, autocommit=True) as c:
        for t in ("products", "product_tags", "product_ingredients"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
    seen, kept = load(SAMPLE, DSN, HashEmbedder(), source="usda", progress=lambda *_: None)
    assert (seen, kept) == (6, 5)  # the implausible jerky is not loaded

    def codes(**spec):
        with psycopg.connect(DSN) as conn:
            return [r["code"] for r in search(conn, QuerySpec.from_dict(spec))]

    # Two barcodes (bag sizes) of the same chips come back once.
    assert codes(categories_any=["cat:chips-pretzels"], include_ingredients_all=["en:avocado-oil"]) \
        == ["012345678905"]
    assert "0098765432109" not in codes(categories_any=["cat:snacks"], max_serving_g=30)  # 60 g bar
    assert "0098765432109" in codes(categories_any=["cat:snacks"], max_serving_g=100)
    assert codes(categories_any=["cat:chips-pretzels"], exclude_groups=["seed_oils"]) == ["012345678905"]
    assert codes(categories_any=["cat:snack-bars"], exclude_allergens=["en:peanuts"],
                 nutrients=[{"nutrient": "protein_g", "basis": "serving", "op": ">=", "value": 20}]) \
        == ["0098765432109"]
    assert codes(ingredient_amounts=[{"ingredient": "en:cocoa", "basis": "percent", "op": ">", "value": 70,
                                      "declared_only": True}]) == ["033333333333"]
    assert codes(categories_any=["cat:snacks"], first_ingredient_any=["en:cocoa"]) == ["033333333333"]
