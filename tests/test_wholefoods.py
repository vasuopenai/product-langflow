"""wholefoods: which products count as Whole Foods brand, listing and filters."""

import os
import re
from pathlib import Path

import pytest

from wholefoods.store import OWNER_PATTERN

USDA_SAMPLE = str(Path(__file__).parent / "fixtures" / "usda")


@pytest.mark.parametrize("owner, is_wfm", [
    ("Whole Foods Market, Inc.", True), ("WHOLE FOODS MARKET", True), ("WHOLE FOODS MARKETS", True),
    ("365 by Whole Foods Market Services", True), ("WHOLE FOODS MARKET - PGC", True), ("WHOLE FOODS", True),
    ("Franco Whole Foods, LLC", False), ("Whole Foods Co-op", False), ("Kroger", False),
])
def test_owner_pattern(owner, is_wfm):
    assert bool(re.search(OWNER_PATTERN, owner, re.I)) is is_wfm


psycopg = pytest.importorskip("psycopg")
DSN = os.getenv("OFF_TEST_DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="set OFF_TEST_DATABASE_URL to run Postgres tests")
def test_refresh_list_and_filters():
    from off_products.pg import HashEmbedder, load
    from wholefoods import store

    with psycopg.connect(DSN, autocommit=True) as c:
        for t in ("products", "product_tags", "product_ingredients"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
        c.execute("DROP SCHEMA IF EXISTS wholefoods CASCADE")
    load(USDA_SAMPLE, DSN, HashEmbedder(), source="usda", progress=lambda *_: None)
    with psycopg.connect(DSN) as c:
        set_owner = ("UPDATE products SET record = jsonb_set(record, '{source,raw,brand_owner}', to_jsonb(%s::text)) "
                     "WHERE code = %s")
        c.execute(set_owner, ("Whole Foods Market, Inc.", "033333333333"))  # the dark chocolate
        c.execute(set_owner, ("Franco Whole Foods, LLC", "012345678905"))   # not Whole Foods Market
        c.commit()
        assert store.refresh(c) == 1
        page = store.list_products(c)
        assert page["total"] == 1 and page["products"][0]["name"] == "85% Dark Chocolate"
        assert store.list_products(c, q="chocolate liquor")["total"] == 1  # matches ingredients too
        assert store.list_products(c, category="cat:chocolate")["total"] == 1
        assert store.list_products(c, category="cat:chips-pretzels")["total"] == 0
        assert store.facets(c)["categories"][0]["count"] == 1
