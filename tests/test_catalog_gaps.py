"""catalog_gaps: sort Kroger catalog products into in-db / in-OFF / staged, resumably. No network."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from catalog_gaps import finder

USDA_SAMPLE = str(Path(__file__).parent / "fixtures" / "usda")


def kroger_item(upc, name, categories=("Snacks",), brand="Brand"):
    """A Kroger search result; Kroger's upc is the 13-digit code without its check digit."""
    return {"productId": upc, "upc": upc, "brand": brand, "description": name, "categories": list(categories),
            "items": [{"size": "1 oz"}], "images": []}


IN_DB = kroger_item("0001234567891", "Classic Potato Chips")           # 012345678912 is in the USDA fixture
IN_OFF = kroger_item("0004900002891", "Cola")                           # 049000028911
NEW = kroger_item("0003600029145", "Brand New Chips")                   # 036000291452, not in the fixture
SOAP = kroger_item("0001111111111", "Hand Soap", categories=("Health & Beauty",))


def test_is_food_skips_only_all_non_food_categories():
    assert finder.is_food(["Snacks"]) and finder.is_food([]) and finder.is_food(["Baby", "Natural & Organic"])
    assert not finder.is_food(["Health & Beauty", "Cleaning Products"])


def test_off_api_checks_are_paced():
    waits, now = [], [100.0]
    check = finder.OffCheck(fetch=lambda *a, **k: (404, None), sleep=waits.append, clock=lambda: now[0])
    conn = SimpleNamespace(execute=lambda *a, **k: None)
    check(conn, "049000028911")
    now[0] += 0.25
    check(conn, "012345678905")
    assert waits == [pytest.approx(0.75)]


psycopg = pytest.importorskip("psycopg")
DSN = os.getenv("OFF_TEST_DATABASE_URL")
pg = pytest.mark.skipif(not DSN, reason="set OFF_TEST_DATABASE_URL to run Postgres tests")


class FakeKroger:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def search(self, term, start=1, limit=50, location_id=None):
        self.calls.append((term, start))
        return self.pages.get((term, start), [])


@pytest.fixture
def conn():
    from off_products.pg import HashEmbedder, load
    with psycopg.connect(DSN, autocommit=True) as c:
        for t in ("products", "product_tags", "product_ingredients"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
        for s in ("staging", "catalog", "kroger"):
            c.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
    load(USDA_SAMPLE, DSN, HashEmbedder(), source="usda", progress=lambda *_: None)
    with psycopg.connect(DSN) as c:
        finder.init_schema(c)
        yield c


@pg
def test_crawl_sorts_products_and_stages_only_real_gaps(conn):
    finder.add_terms(conn, ["chips"])
    client = FakeKroger({("chips", 1): [IN_DB, IN_OFF, NEW, SOAP]})
    off_check = lambda c, code: code == "49000028911"
    settings = finder.Settings({})

    stats = finder.crawl_step(conn, client, off_check, settings)
    assert stats["new"] == 4 and stats["staged"] == 1 and stats["term_done"]  # short page: term finished
    seen = dict(conn.execute("SELECT name, status FROM catalog.seen").fetchall())
    assert seen == {"Classic Potato Chips": "in_db", "Cola": "in_off",
                    "Brand New Chips": "staged", "Hand Soap": "non_food"}
    row = conn.execute("SELECT barcode, status, found_via, draft->>'name', scan_count FROM staging.scanned_products"
                       ).fetchone()
    assert row == ("36000291452", "queued", "kroger_catalog", "Brand New Chips", 0)

    assert finder.crawl_step(conn, client, off_check, settings) is None  # every term done
    s = finder.status(conn)
    assert s["terms"] == {"total": 1, "done": 1} and s["staged_by_status"] == {"queued": 1}
    assert s["today"]["kroger"] == 1


@pg
def test_crawl_resumes_page_by_page_and_skips_products_already_seen(conn):
    finder.add_terms(conn, ["bars"])
    full = [kroger_item(f"00012345{i:05d}", f"Bar {i}") for i in range(finder.PAGE)]
    client = FakeKroger({("bars", 1): full, ("bars", 51): full[:3] + [NEW]})
    off_check = lambda c, code: True
    settings = finder.Settings({})

    first = finder.crawl_step(conn, client, off_check, settings)
    assert not first["term_done"] and finder.next_term(conn, 30) == ("bars", 51)
    second = finder.crawl_step(conn, client, off_check, settings)
    assert second["new"] == 1 and second["term_done"]  # the 3 repeats were skipped
    assert client.calls == [("bars", 1), ("bars", 51)]


@pg
def test_research_is_off_by_default_and_capped(conn):
    finder.add_terms(conn, ["chips"])
    finder.crawl_step(conn, FakeKroger({("chips", 1): [NEW]}), lambda c, code: False, finder.Settings({}))
    assert finder.research_step(conn, finder.Settings({})) is None  # CATALOG_RESEARCH_PER_DAY defaults to 0

    saved = []
    capped = finder.Settings({"CATALOG_RESEARCH_PER_DAY": "1"})
    barcode = finder.research_step(conn, capped, research=lambda b: {"web": {}}, save=lambda c, b, f: saved.append(b))
    assert barcode == "36000291452" and saved == [barcode]
    assert finder.research_step(conn, capped) is None  # today's one is used
