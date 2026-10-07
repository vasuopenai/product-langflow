"""Postgres tests. Run with a throwaway database that has pgvector available:

  OFF_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/off_test python -m pytest tests
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.getenv("OFF_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set OFF_TEST_DATABASE_URL to run Postgres tests")

from off_products.ask import ask  # noqa: E402
from off_products.pg import HashEmbedder, load, search  # noqa: E402
from off_products.query import QuerySpec  # noqa: E402

SAMPLE = str(Path(__file__).parent / "fixtures" / "sample_products.jsonl")


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        for t in ("products", "product_tags", "product_ingredients"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
    seen, kept = load(SAMPLE, DSN, HashEmbedder(), progress=lambda *_: None)
    assert (seen, kept) == (11, 10)  # the bar without an ingredient list is skipped
    with psycopg.connect(DSN) as c:
        yield c


def codes(conn, **spec):
    q = QuerySpec.from_dict(spec)
    vec = HashEmbedder().embed([q.semantic_query])[0] if q.semantic_query else None
    return [r["code"] for r in search(conn, q, vec)]


def test_filters_match_sqlite_behaviour(conn):
    assert codes(
        conn, semantic_query="protein bar", categories_any=["en:protein-bars"],
        nutrients=[{"nutrient": "protein_g", "basis": "serving", "op": ">=", "value": 20}],
        exclude_groups=["seed_oils"],
    ) == ["0000000000011"]
    assert codes(
        conn, semantic_query="chips", include_ingredients_all=["en:avocado-oil"],
        max_ingredients=4, sort_by="ingredients_n",
    ) == ["0000000000021"]
    assert codes(
        conn, semantic_query="chocolate",
        ingredient_amounts=[{"ingredient": "en:cocoa-mass", "basis": "percent", "op": ">", "value": 70}],
    ) == ["0000000000031"]


def test_semantic_ranking_breaks_ties(conn):
    assert codes(conn, semantic_query="avocado oil sea salt potato chips", limit=1) == ["0000000000021"]


class FakeOpenAI:
    """Returns a fixed tool call for parsing and echoes product names as the answer."""

    def __init__(self, spec):
        self.spec = spec
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        if "tools" in kw:
            call = SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(self.spec)))
            msg = SimpleNamespace(tool_calls=[call], content=None)
        else:
            products = json.loads(kw["messages"][-1]["content"])["products"]
            msg = SimpleNamespace(content="; ".join(p["name"] for p in products))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_ask_end_to_end_with_relaxation(conn):
    spec = {
        "semantic_query": "protein bar",
        "categories_any": ["en:protein-bars", "en:made-up-category"],
        "nutrients": [{"nutrient": "protein_g", "basis": "serving", "op": ">=", "value": 20}],
        "exclude_groups": ["seed_oils"],
    }
    out = ask(conn, "protein bar >= 20 g protein, no seed oils", HashEmbedder(), FakeOpenAI(spec))
    assert [p["code"] for p in out["products"]] == ["0000000000011"]
    assert out["answer"] == "Chocolate Almond Protein Bar"
    assert any("made-up-category" in n for n in out["notes"])


def test_ask_relaxes_empty_category(conn):
    spec = {"semantic_query": "avocado oil chips", "categories_any": ["en:protein-bars"],
            "include_ingredients_all": ["en:avocado-oil"], "max_ingredients": 3}
    out = ask(conn, "q", HashEmbedder(), FakeOpenAI(spec))
    assert [p["code"] for p in out["products"]] == ["0000000000021"]
    assert any("searched all categories" in n for n in out["notes"])


def test_retailer_tags_and_info_in_postgres(conn):
    from off_products.retail import link_to_store, retailer_info

    parsed = {"price_promo": None, "aisle": "Aisle 3", "location_id": "01400943",
              "fetched_at": "2026-10-07T00:00:00+00:00"}
    rows = [{"store_code": "0000000000021", "product_id": "k1", "upc": "0001111041700",
             "description": "Avocado chips", "size": "5 oz", "price_regular": 4.49, "_parsed": parsed}]
    assert link_to_store(conn, rows, ph="%s") == 1
    assert codes(conn, semantic_query="chips", retailers_any=["kroger"]) == ["0000000000021"]
    info = retailer_info(conn, ["0000000000021"], ph="%s")
    assert info["0000000000021"][0]["aisle"] == "Aisle 3"
    conn.execute("DELETE FROM product_tags WHERE kind = 'retailer'")
    conn.commit()
