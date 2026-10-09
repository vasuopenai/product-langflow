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


def test_store_codes_are_set_aside():
    assert finder.is_store_code("40110")            # produce PLU
    assert finder.is_store_code("20123400000")      # in-store random-weight label (UPC number system 2)
    assert not finder.is_store_code("36000291452")


def test_is_food_skips_only_all_non_food_categories():
    assert finder.is_food(["Snacks"]) and finder.is_food([]) and finder.is_food(["Baby", "Natural & Organic"])
    assert not finder.is_food(["Health & Beauty", "Cleaning Products"])


def test_off_api_checks_are_paced():
    waits, now = [], [100.0]
    check = finder.OffCheck(fetch=lambda *a, **k: (404, None), sleep=waits.append, clock=lambda: now[0])
    check._refs = False  # no reference list loaded: use the API
    conn = SimpleNamespace(execute=lambda *a, **k: None)
    check(conn, ["49000028911"])
    now[0] += 0.25
    check(conn, ["12345678905"])
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
    off_check = lambda c, keys: "49000028911" in keys
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
    off_check = lambda c, keys: True
    settings = finder.Settings({})

    first = finder.crawl_step(conn, client, off_check, settings)
    assert not first["term_done"] and finder.next_term(conn, 30) == ("bars", 51)
    second = finder.crawl_step(conn, client, off_check, settings)
    assert second["new"] == 1 and second["term_done"]  # the 3 repeats were skipped
    assert client.calls == [("bars", 1), ("bars", 51)]


@pg
def test_refs_report_export_and_staging_a_group_without_crawling_again(conn, tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from catalog_gaps import gaps, refs

    usda_csv = tmp_path / "branded_food.csv"
    usda_csv.write_text('"fdc_id","gtin_upc"\n"1","00012345678912"\n"2","00049000028911"\n"3","012345678912"\n')
    off_parquet = tmp_path / "food.parquet"
    pq.write_table(pa.table({"code": ["0049000028911", "0036000291452", "3017620422003"],
                             "countries_tags": [["en:united-states"], ["en:france"], ["en:france"]]}), off_parquet)
    assert refs.build_usda(conn, usda_csv, progress=lambda *_: None) == 2       # duplicates collapsed
    assert refs.build_off(conn, off_parquet, progress=lambda *_: None) == 3

    # Crawled with auto-staging off: OFF check says no, so everything not in our app is staged...
    finder.add_terms(conn, ["chips"])
    gone = kroger_item("0007777777777", "Gone Everywhere")  # 077777777779: in no list
    finder.crawl_step(conn, FakeKroger({("chips", 1): [IN_DB, IN_OFF, NEW, SOAP, gone]}),
                      lambda c, keys: False, finder.Settings({}))
    conn.execute("DELETE FROM staging.scanned_products")  # ...start staging from scratch for this test
    conn.commit()

    r = gaps.report(conn)
    assert r["kroger_food_products"] == 4 and r["excluded"] == {"non_food": 1}
    # IN_DB: USDA only. IN_OFF (Cola): USDA + OFF (US). NEW: OFF (France) only. gone: neither.
    assert r["groups"]["not_in_usda"] == 2 and r["groups"]["not_in_off"] == 2
    assert r["groups"]["neither"] == 1 and r["groups"]["not_in_off_us"] == 3
    assert r["groups"]["usda_not_app"] == 1  # Cola is in USDA but wasn't loaded into the app
    assert r["usda_x_off"] == {"usda_yes__off_no": 1, "usda_yes__off_yes": 1, "usda_no__off_yes": 1,
                               "usda_no__off_no": 1}
    assert "not in the USDA database" in gaps.format_report(r)

    assert gaps.export_csv(conn, tmp_path / "gaps.csv", "not_in_usda") == 2
    assert gaps.stage_group(conn, "not_in_usda", dry_run=True) == {"would_stage": 2}
    assert gaps.stage_group(conn, "not_in_usda", limit=1) == {"staged": 1}
    assert gaps.stage_group(conn, "not_in_usda") == {"staged": 1}  # the one already staged is left alone
    staged = dict(conn.execute("SELECT draft->>'name', status FROM staging.scanned_products").fetchall())
    assert staged == {"Brand New Chips": "queued", "Gone Everywhere": "queued"}


@pg
def test_research_is_off_by_default_and_capped(conn):
    finder.add_terms(conn, ["chips"])
    finder.crawl_step(conn, FakeKroger({("chips", 1): [NEW]}), lambda c, keys: False, finder.Settings({}))
    assert finder.research_step(conn, finder.Settings({})) is None  # CATALOG_RESEARCH_PER_DAY defaults to 0

    saved = []
    capped = finder.Settings({"CATALOG_RESEARCH_PER_DAY": "1"})
    barcode = finder.research_step(conn, capped, research=lambda b: {"web": {}}, save=lambda c, b, f: saved.append(b))
    assert barcode == "36000291452" and saved == [barcode]
    assert finder.research_step(conn, capped) is None  # today's one is used
