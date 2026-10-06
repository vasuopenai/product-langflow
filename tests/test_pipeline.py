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
