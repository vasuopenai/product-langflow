import json
import sqlite3
from pathlib import Path

import pytest

from off_products import QuerySpec, normalize, to_pinecone_filter, to_pinecone_metadata, to_sql
from off_products.query import matches_ingredient_amounts
from off_products.store import build, iter_raw

SAMPLE = str(Path(__file__).parent / "fixtures" / "sample_products.jsonl")


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "products.db"
    build(SAMPLE, str(path))
    return sqlite3.connect(path)


@pytest.fixture(scope="module")
def records():
    return {r["code"]: r for r in (normalize(raw) for raw in iter_raw(SAMPLE))}


def run(db, **spec):
    sql, params = to_sql(QuerySpec.from_dict(spec))
    return [row[0] for row in db.execute(sql, params)]


def test_protein_bar_min_protein_without_seed_oils(db):
    codes = run(
        db,
        semantic_query="protein bar",
        categories_any=["en:protein-bars"],
        nutrients=[{"nutrient": "protein_g", "basis": "serving", "op": ">=", "value": 20}],
        exclude_groups=["seed_oils"],
    )
    # 12 has sunflower oil, 13 has 6 g protein, 14 has unspecified vegetable oil,
    # 15 has no ingredient list so it cannot be called seed-oil free.
    assert codes == ["0000000000011"]


def test_minimal_chips_with_avocado_oil(db):
    codes = run(
        db,
        semantic_query="potato chips",
        categories_any=["en:potato-crisps"],
        include_ingredients_all=["en:avocado-oil"],
        max_ingredients=4,
        sort_by="ingredients_n",
    )
    assert codes == ["0000000000021"]


def test_macro_threshold_more_than_10g_protein(db):
    codes = run(
        db,
        semantic_query="snack",
        nutrients=[{"nutrient": "protein_g", "basis": "serving", "op": ">", "value": 10}],
    )
    assert set(codes) == {"0000000000011", "0000000000012", "0000000000014", "0000000000015"}


def test_ingredient_amount_percent(db):
    codes = run(
        db,
        semantic_query="chocolate",
        ingredient_amounts=[{"ingredient": "en:cocoa-mass", "basis": "percent", "op": ">=", "value": 70}],
    )
    assert codes == ["0000000000031"]


def test_ingredient_amount_grams_per_serving_declared_only(db):
    spec = dict(
        semantic_query="chocolate",
        ingredient_amounts=[{"ingredient": "en:hazelnut", "basis": "grams_per_serving",
                             "op": ">=", "value": 4, "declared_only": True}],
    )
    # 12% of a 40 g serving = 4.8 g
    assert run(db, **spec) == ["0000000000032"]


def test_hidden_seed_oil_is_possible_not_none(records):
    bar = records["0000000000014"]["derived"]["groups"]["seed_oils"]
    assert bar["status"] == "possible" and bar["possible"] == ["vegetable oil"]
    kettle = records["0000000000024"]["derived"]["groups"]["seed_oils"]
    assert kettle["status"] == "contains"  # high oleic sunflower via ancestor tag


def test_other_groups(records):
    milk = records["0000000000032"]["derived"]["groups"]
    assert milk["artificial_sweeteners"]["status"] == "contains"
    assert milk["added_sugars"]["status"] == "contains"
    assert records["0000000000021"]["derived"]["groups"]["added_sugars"]["status"] == "none"
    assert records["0000000000015"]["derived"]["groups"]["added_sugars"]["status"] == "unknown"


def test_per_serving_is_derived_from_100g_when_missing():
    raw = {
        "code": "1",
        "product_name": "x",
        "serving_quantity": "50",
        "nutriments": {"proteins_100g": 30, "energy-kcal_100g": 400},
    }
    n = normalize(raw)["nutrition"]
    assert n["per_serving"]["protein_g"] == 15
    assert normalize(raw)["derived"]["protein_kcal_pct"] == 30.0


def test_impossible_per_100g_values_become_unknown():
    # Real OFF pattern: per-100 g numbers typed into the per-serving fields of jerky.
    raw = {
        "code": "1", "product_name": "jerky", "serving_quantity": "28", "serving_size": "28g",
        "nutriments": {"proteins_100g": 153, "proteins_serving": 42.86, "energy-kcal_100g": 982},
    }
    n = normalize(raw)["nutrition"]
    assert n["per_100g"]["protein_g"] is None and n["per_serving"]["protein_g"] is None
    assert any("protein_g" in reason for reason in n["implausible"])


def test_serving_weight_that_disagrees_with_label_drops_per_serving_only():
    # The serving quantity is the prepared drink, the label says a 5 g packet.
    raw = {
        "code": "1", "product_name": "drink mix", "serving_quantity": "507",
        "serving_size": "1 PACKET MIX, MAKES 16.9 fl oz. (5 g)",
        "nutriments": {"proteins_100g": 20, "energy-kcal_100g": 200},
    }
    n = normalize(raw)["nutrition"]
    assert n["per_100g"]["protein_g"] == 20
    assert n["per_serving"]["protein_g"] is None and n["serving_g"] is None
    assert n["implausible"]


def test_plausible_nutrition_is_kept():
    raw = {
        "code": "1", "product_name": "bar", "serving_quantity": "60", "serving_size": "1 bar (60 g)",
        "nutriments": {"proteins_100g": 35, "fat_100g": 15, "carbohydrates_100g": 40,
                       "energy-kcal_100g": 435},
    }
    n = normalize(raw)["nutrition"]
    assert n["implausible"] == [] and n["per_serving"]["protein_g"] == 21


def _nutrition(serving_size=None, serving_quantity=None, **per_100g):
    raw = {"code": "1", "product_name": "x", "serving_size": serving_size,
           "serving_quantity": serving_quantity, "nutriments": {f"{k}_100g": v for k, v in per_100g.items()}}
    return normalize(raw)["nutrition"]


def test_energy_inconsistent_with_macros_becomes_unknown():
    # 519 kcal from 8 g protein, 10 g fat, 7 g carbs (about 150 kcal).
    n = _nutrition(**{"energy-kcal": 519, "proteins": 8, "fat": 10, "carbohydrates": 7})
    assert n["per_100g"]["energy_kcal"] is None
    # Sugar alcohols make low energy from carbs legitimate.
    n = _nutrition(**{"energy-kcal": 30, "proteins": 0, "fat": 0, "carbohydrates": 95})
    assert n["implausible"] == []
    # Spirits: energy comes from alcohol (% vol).
    n = _nutrition(**{"energy-kcal": 231, "proteins": 0, "fat": 0, "carbohydrates": 0, "alcohol": 40})
    assert n["implausible"] == []


def test_liquid_with_protein_density_of_a_powder_becomes_unknown():
    for size in ("16.9 OZA (507 ml)", "340ml"):
        n = _nutrition(size, "507", **{"energy-kcal": 414, "proteins": 88.76, "fat": 0,
                                       "carbohydrates": 5.92})
        assert n["per_100g"]["protein_g"] is None, size
    n = _nutrition("1 portion (443 ml)", "443", **{"energy-kcal": 41, "proteins": 9.5, "fat": 0,
                                                   "carbohydrates": 0.5})
    assert n["per_serving"]["protein_g"] == 42.09


def test_package_sized_or_mislabelled_servings_drop_per_serving_only():
    for size, qty in (("20 wings (1339 g)", "1339"), ("30 g (2 lbs)", "907.18")):
        n = _nutrition(size, qty, **{"energy-kcal": 161, "proteins": 18.4, "fat": 9.56,
                                     "carbohydrates": 0.67})
        assert n["per_100g"]["protein_g"] == 18.4
        assert n["per_serving"]["protein_g"] is None and n["serving_g"] is None


def test_nested_restatement_of_an_ingredient_is_not_double_counted():
    from off_products.normalize import ingredient_amounts
    raw = {
        "code": "1", "product_name": "dark chocolate",
        "ingredients": [
            {"id": "en:cocoa", "text": "Cocoa & cocoa butter", "percent_estimate": 68.39,
             "ingredients": [{"id": "en:cocoa", "text": "cocoa", "percent": 70}]},
            {"id": "en:cane-sugar", "text": "cane sugar", "percent": 30},
            # Same id in two separate sub-recipes is still summed.
            {"id": "en:filling", "text": "filling", "percent_estimate": 1,
             "ingredients": [{"id": "en:cane-sugar", "text": "sugar", "percent_estimate": 0.5}]},
        ],
    }
    rows = {r["ingredient"]: r for r in ingredient_amounts(normalize(raw))}
    assert rows["en:cocoa"]["percent"] == 68.39 and not rows["en:cocoa"]["declared"]
    assert rows["en:cane-sugar"]["percent"] == 30.5


def test_answer_is_told_which_filters_ran():
    from types import SimpleNamespace

    from off_products.ask import answer, applied_filters
    sent = {}

    def create(**kw):
        sent.update(json.loads(kw["messages"][-1]["content"]))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    spec = QuerySpec(semantic_query="granola", first_ingredient_any=["en:almond"], limit=5)
    answer("gluten-free granola, nuts first", [], [], client, filters=applied_filters(spec))
    assert sent["filters"] == {"first_ingredient_any": ["en:almond"]}
    spec.unchecked_terms = ["gluten-free"]
    assert "unchecked_terms" not in applied_filters(spec)  # a wish, not a filter


def test_healthy_rule_keeps_only_protein_rich_low_carb_no_added_sugar_no_trans_fat(tmp_path):
    path = tmp_path / "healthy.db"
    build(SAMPLE, str(path))
    db = sqlite3.connect(path)
    # The sample has no carbs or trans fat; give some products values. Protein share of
    # calories: 11 is 37%, 14 is 41%, 12 is 33%, 13 is 12%.
    for code, carbs, trans in [("0000000000011", 30, 0),  # healthy
                               ("0000000000014", 60, 0),  # too many carbs (67% of calories)
                               ("0000000000013", 30, 0),  # too little protein
                               ("0000000000012", 30, 0)]:  # added sugars, amount unknown
        db.execute("UPDATE products SET carbs_g_100g = ?, trans_fat_g_serving = ? WHERE code = ?", (carbs, trans, code))
    assert run(db, healthy=True) == ["0000000000011"]
    db.execute("UPDATE products SET added_sugars_g_serving = 1 WHERE code = '0000000000012'")  # a little is fine
    assert sorted(run(db, healthy=True)) == ["0000000000011", "0000000000012"]
    db.execute("UPDATE products SET trans_fat_g_serving = 0.5 WHERE code = '0000000000011'")
    assert run(db, healthy=True) == ["0000000000012"]


def test_max_nutriscore_grade_keeps_that_grade_and_better(tmp_path):
    path = tmp_path / "ns.db"
    build(SAMPLE, str(path))
    db = sqlite3.connect(path)
    db.execute("UPDATE products SET nutriscore_grade = NULL")
    for code, grade in [("0000000000011", "a"), ("0000000000012", "b"), ("0000000000013", "c")]:
        db.execute("UPDATE products SET nutriscore_grade = ? WHERE code = ?", (grade, code))
    assert sorted(run(db, max_nutriscore_grade="b")) == ["0000000000011", "0000000000012"]
    assert run(db, max_nutriscore_grade="A") == ["0000000000011"]  # ungraded products never match
    assert to_pinecone_filter(QuerySpec(max_nutriscore_grade="b")) == {
        "$and": [{"nutriscore_grade": {"$in": ["a", "b"]}}]}
    with pytest.raises(ValueError):
        to_sql(QuerySpec(max_nutriscore_grade="f"))


def test_closing_line_names_unchecked_wishes_in_plain_words():
    from off_products.ask import closing_line
    assert closing_line([]) == closing_line(["", " "]) == ""
    assert closing_line(["high fiber"]) == 'These are the closest matches for "high fiber"; check the label to be sure.'
    assert closing_line(["crunchy", "healthy", "crunchy", "kid-friendly"]) == (
        'These are the closest matches for "crunchy", "healthy" and "kid-friendly"; check the label to be sure.')


def test_answer_sees_a_fixed_ranked_slice_deterministically(records):
    from types import SimpleNamespace

    from off_products.ask import ANSWER_TOP_N, answer
    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    ranked = sorted(records.values(), key=lambda r: r["code"])
    answer("q", ranked, [], client)
    sent = json.loads(calls[0]["messages"][-1]["content"])["products"]
    assert [p["code"] for p in sent] == [r["code"] for r in ranked[:ANSWER_TOP_N]]
    assert calls[0]["temperature"] == 0 and "seed" in calls[0]
    assert [p["rank"] for p in sent] == list(range(1, len(sent) + 1))


def test_answer_numbers_are_rounded_like_a_label():
    from off_products.ask import _facts
    r = {"name": "x", "brand": None, "code": "1", "url": None, "labels": [],
         "nutrition": {"serving_size": "28 g", "per_100g": {},
                       "per_serving": {"energy_kcal": 129.92, "protein_g": 2.0016, "sodium_mg": 120.4}},
         "ingredients": {"text": "", "count_total": 0, "items": []}, "derived": {"groups": {}}}
    assert _facts(r)["per_serving"] == {"energy_kcal": 130, "protein_g": 2.0, "sodium_mg": 120}


def test_parquet_row_shape():
    row = {
        "code": "42",
        "product_name": [{"lang": "main", "text": "Barre"}, {"lang": "en", "text": "Bar"}],
        "ingredients_text": [{"lang": "en", "text": "dates, almonds"}],
        "ingredients": json.dumps([
            {"id": "en:date", "text": "dates", "percent_estimate": 60},
            {"id": "en:almond", "text": "almonds", "percent_estimate": 40},
        ]),
        "ingredients_tags": ["en:date", "en:almond", "en:nut"],
        "nutriments": [{"name": "proteins", "100g": 8.0, "serving": None}],
        "serving_quantity": "40",
    }
    r = normalize(row)
    assert r["name"] == "Bar"
    assert r["ingredients"]["count_total"] == 2
    assert r["nutrition"]["per_serving"]["protein_g"] == 3.2
    assert r["derived"]["groups"]["seed_oils"]["status"] == "none"


def test_pinecone_filter_and_metadata(records):
    spec = QuerySpec(
        categories_any=["en:protein-bars"],
        nutrients=[{"nutrient": "protein_g", "basis": "serving", "op": ">=", "value": 20}],
        exclude_groups=["seed_oils"],
    )
    assert to_pinecone_filter(spec) == {"$and": [
        {"category_tags": {"$in": ["en:protein-bars"]}},
        {"free_of": {"$in": ["seed_oils"]}},
        {"protein_g_serving": {"$gte": 20}},
    ]}
    meta = to_pinecone_metadata(records["0000000000011"])
    assert None not in meta.values()
    assert "seed_oils" in meta["free_of"]


def test_post_filter_amounts(records):
    spec = QuerySpec(ingredient_amounts=[
        {"ingredient": "en:cocoa-mass", "basis": "percent", "op": ">", "value": 70}])
    assert matches_ingredient_amounts(records["0000000000031"], spec)
    assert not matches_ingredient_amounts(records["0000000000032"], spec)


def test_rejects_unknown_fields():
    with pytest.raises(ValueError):
        to_sql(QuerySpec(exclude_groups=["not_a_group"]))
    with pytest.raises(ValueError):
        to_sql(QuerySpec(nutrients=[{"nutrient": "protein_g; DROP", "basis": "serving", "op": ">", "value": 1}]))


def test_no_grade_note_says_why_in_plain_words():
    from off_products.ask import _no_grade_note
    assert _no_grade_note({"status": "missing_data", "missing": ["salt_g", "saturated_fat_g", "fiber_g"],
                           "reasons": ["missing salt_g, saturated_fat_g"]}) == "Missing salt, saturated fat"
    assert _no_grade_note({"status": "missing_data", "missing": ["energy_kcal"],
                           "reasons": ["nutrition values failed plausibility checks", "missing energy_kcal"]}) \
        == "Nutrition data looks wrong"
    assert _no_grade_note({"status": "not_applicable", "reasons": ["herbs and spices"]}) \
        == "Not rated for this kind of food (herbs and spices)"
    assert _no_grade_note({"status": "needs_prepared_values", "reasons": ["x"]}).startswith("Rated as prepared")
    assert _no_grade_note({"status": "missing_data", "missing": ["fiber_g"], "reasons": []}) == "Not enough nutrition data"
