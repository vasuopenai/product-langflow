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


class CountingEmbedder(HashEmbedder):
    def __init__(self):
        super().__init__()
        self.texts = 0

    def embed(self, texts):
        self.texts += len(texts)
        return super().embed(texts)


def test_reload_reuses_embeddings_and_drops_rejected(conn, tmp_path):
    rows = [json.loads(line) for line in open(SAMPLE, encoding="utf-8")]
    src = tmp_path / "reload.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    again = CountingEmbedder()
    load(str(src), DSN, again, progress=lambda *_: None)
    assert again.texts == 0  # nothing changed since the fixture load

    # One product turns out to have impossible numbers; another gets a new name.
    by_code = {r["code"]: r for r in rows}
    bad, renamed = by_code["0000000000013"], by_code["0000000000024"]
    bad["nutriments"] = {**bad["nutriments"], "proteins_100g": 150}
    renamed["product_name"] += " v2"
    src.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    changed = CountingEmbedder()
    load(str(src), DSN, changed, progress=lambda *_: None)
    assert changed.texts == 1
    assert conn.execute("SELECT count(*) FROM products WHERE code = %s", (bad["code"],)).fetchone()[0] == 0

    load(SAMPLE, DSN, HashEmbedder(), progress=lambda *_: None)  # restore for later tests


def test_ask_relaxes_empty_category(conn):
    spec = {"semantic_query": "avocado oil chips", "categories_any": ["en:protein-bars"],
            "include_ingredients_all": ["en:avocado-oil"], "max_ingredients": 3}
    out = ask(conn, "q", HashEmbedder(), FakeOpenAI(spec))
    assert [p["code"] for p in out["products"]] == ["0000000000021"]
    assert any("searched all categories" in n for n in out["notes"])
