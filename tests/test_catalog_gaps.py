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

    def search(self, term, start=1, limit=50, location_id=None, brand=None):
        self.calls.append((term, start) if brand is None else (term, brand, start))
        return self.pages.get((term, start) if brand is None else (term, brand, start), [])


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

    assert finder.next_term(conn, 30) == ("Brand", "", 1)  # the brand it found is searched next
    assert finder.crawl_step(conn, client, off_check, settings)["term_done"]  # (nothing more for it)
    assert finder.crawl_step(conn, client, off_check, settings) is None  # every search done
    s = finder.status(conn)
    assert s["terms"] == {"total": 2, "done": 2} and s["staged_by_status"] == {"queued": 1}
    assert s["today"]["kroger"] == 2


@pg
def test_crawl_resumes_page_by_page_and_skips_products_already_seen(conn):
    finder.add_terms(conn, ["bars"])
    full = [kroger_item(f"00012345{i:05d}", f"Bar {i}") for i in range(finder.PAGE)]
    client = FakeKroger({("bars", 1): full, ("bars", 51): full[:3] + [NEW]})
    off_check = lambda c, keys: True
    settings = finder.Settings({})

    first = finder.crawl_step(conn, client, off_check, settings)
    assert not first["term_done"] and finder.next_term(conn, 30) == ("bars", "", 51)
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


def test_product_info_from_a_kroger_record():
    from catalog_gaps import kroger_product as kp
    record = {
        "productId": "0001111012345", "upc": "0001111012345", "brand": "Simple Truth",
        "description": "Simple Truth® Almond Butter", "categories": ["Natural & Organic"],
        "items": [{"size": "16 oz"}], "images": [], "organicClaimName": "Organic", "allergensDescription": "Tree nuts",
        "nutritionInformation": [{
            "ingredientStatement": "Dry Roasted Almonds, Sea Salt.",
            "servingSize": {"quantity": 2, "unitOfMeasure": {"code": "G24", "abbreviation": "tbsp"}},
            "servingsPerPackage": {"value": 14},
            "nutrients": [
                {"code": "ENER-", "quantity": 190, "unitOfMeasure": {"code": "E14"}},
                {"code": "PRO-", "quantity": 7, "unitOfMeasure": {"code": "GRM"}},
                {"code": "NA", "quantity": 0.06, "unitOfMeasure": {"code": "GRM"}},
                {"code": "K", "percentDailyIntake": 4},
            ]}]}
    draft = kp.product_info(record)
    assert draft["ingredients"] == "Dry Roasted Almonds, Sea Salt." and draft["serving_text"] == "2 tbsp"
    assert draft["serving_size_g"] == round(16 * 28.3495 / 14, 2)  # tablespoons: package weight / servings
    assert draft["nutrients_serving"] == {"energy_kcal": 190, "protein_g": 7, "sodium_mg": 60}
    assert draft["nutrients_100g"]["energy_kcal"] == round(190 * 100 / (16 * 28.3495 / 14), 3)
    assert draft["labels"] == ["en:organic"] and draft["allergens"] == "Tree nuts"
    assert kp.completeness(draft) == "full"
    assert kp.package_grams("40 ct / 0.8 oz") == 40 * 0.8 * 28.3495  # 40 pouches of 0.8 oz
    assert kp.package_grams("12 fl oz") == 12 * 29.5735 and kp.package_grams("16 oz") == 16 * 28.3495
    assert kp.package_grams("8 pk / 0.11 oz") == 8 * 0.11 * 28.3495
    assert kp.package_grams("295 pieces / 82.36 oz") == 82.36 * 28.3495  # the bag weighs 82 oz in all
    assert kp.completeness(kp.product_info({"description": "Mystery", "nutritionInformation": ""})) == "basic"


@pg
def test_capped_searches_split_by_brand_and_new_brands_are_queued(conn):
    finder.add_terms(conn, ["yogurt"])
    full = [kroger_item(f"00022222{i:05d}", f"Yogurt {i}", brand="Chobani" if i % 2 else "Kroger")
            for i in range(finder.PAGE)]
    pages = {("yogurt", s): full for s in range(1, finder.MAX_START + 1, finder.PAGE)}
    client = FakeKroger(pages)
    settings = finder.Settings({})
    while (step := finder.crawl_step(conn, client, lambda c, keys: True, settings)) and not step["term_done"]:
        pass
    queued = conn.execute("SELECT term, brand, kind FROM catalog.terms ORDER BY kind, term, brand").fetchall()
    # the capped food term splits per brand seen; both brands are also queued as their own searches
    assert queued == [("Chobani", "", "brand"), ("Kroger", "", "brand"),
                      ("yogurt", "Chobani", "split"), ("yogurt", "Kroger", "split"), ("yogurt", "", "term")]
    assert conn.execute("SELECT capped FROM catalog.terms WHERE term = 'yogurt' AND brand = ''").fetchone()[0]
    # next come the brand searches, then the splits (with filter.brand)
    assert finder.next_term(conn, 30) == ("Chobani", "", 1)
    finder.crawl_step(conn, client, lambda c, keys: True, settings)
    finder.crawl_step(conn, client, lambda c, keys: True, settings)
    assert finder.next_term(conn, 30) == ("yogurt", "Chobani", 1)
    finder.crawl_step(conn, client, lambda c, keys: True, settings)
    assert client.calls[-1] == ("yogurt", "Chobani", 1)
    s = finder.status(conn)
    assert s["searches"]["term"]["capped"] == 1 and s["searches"]["brand"]["done"] == 2


@pg
def test_searches_kroger_rejects_are_given_up_and_counted(conn):
    from kroger_sync.client import KrogerError

    class Rejecting(FakeKroger):
        def search(self, term, start=1, limit=50, location_id=None, brand=None):
            self.calls.append(term)
            raise KrogerError("GET /products failed (400): term not accepted")

    finder.add_terms(conn, ["4C", "granola"])  # "4C": too short for Kroger, never sent
    client = Rejecting({})
    settings = finder.Settings({})
    first = finder.crawl_step(conn, client, lambda c, keys: True, settings)
    assert first["term"] == "4C" and first["term_done"] and client.calls == []
    second = finder.crawl_step(conn, client, lambda c, keys: True, settings)
    assert second["term"] == "granola" and "400" in second["error"] and client.calls == ["granola"]
    assert finder.crawl_step(conn, client, lambda c, keys: True, settings) is None  # nothing retried forever
    assert finder.used(conn, "kroger") == 1  # the failed call counts toward the daily budget
    errors = dict(conn.execute("SELECT term, error FROM catalog.terms").fetchall())
    assert "3 to 127 characters" in errors["4C"] and "400" in errors["granola"]


@pg
def test_reclassify_takes_new_non_food_out_of_the_queue(conn):
    finder.add_terms(conn, ["ball"])
    ball = kroger_item("0003600029145", "Soccer Ball", categories=("Snacks",))
    finder.crawl_step(conn, FakeKroger({("ball", 1): [ball]}), lambda c, keys: False, finder.Settings({}))
    assert conn.execute("SELECT count(*) FROM staging.scanned_products").fetchone()[0] == 1
    conn.execute("""UPDATE catalog.seen SET raw = jsonb_set(raw, '{categories}', '["Sporting Goods"]')""")
    assert finder.reclassify_food(conn) == (1, 1)
    assert conn.execute("SELECT food, status FROM catalog.seen").fetchone() == (False, "non_food")
    assert conn.execute("SELECT count(*) FROM staging.scanned_products").fetchone()[0] == 0


@pg
def test_splits_of_non_food_brands_are_skipped_and_not_rechecked(conn):
    finder.add_terms(conn, ["juice"])
    makeup = [kroger_item(f"00033333{i:05d}", f"Lipstick {i}", categories=("Beauty",), brand="NYX") for i in range(3)]
    finder.crawl_step(conn, FakeKroger({("juice", 1): makeup}), lambda c, keys: True, finder.Settings({}))
    assert not finder.food_brand(conn, "NYX") and finder.food_brand(conn, "Unknown Brand")
    conn.execute("INSERT INTO catalog.terms (term, brand, kind) VALUES ('juice', 'NYX', 'split'), ('juice', 'Oatly', 'split')")
    conn.execute("UPDATE catalog.terms SET done_at = now() WHERE kind <> 'split'")  # only the splits are left
    conn.commit()
    client = FakeKroger({})
    step = finder.crawl_step(conn, client, lambda c, keys: True, finder.Settings({}))
    assert step["skipped"] == 1 and step["brand"] == "Oatly"            # NYX skipped, Oatly searched
    assert client.calls == [("juice", "Oatly", 1)]                         # no call for NYX
    assert conn.execute("SELECT error FROM catalog.terms WHERE brand = 'NYX'").fetchone()[0] == finder.NON_FOOD_BRAND
    conn.execute("UPDATE catalog.terms SET done_at = now() - interval '60 days'")  # everything is due again...
    conn.commit()
    assert finder.next_term(conn, 30)[1] != "NYX"                          # ...except the skipped split
    assert finder.split(conn, "NYX", "", "brand") == 0                     # and a capped NYX search isn't split


@pg
def test_crawler_sleeps_with_no_transaction_open(conn):
    """An open transaction while sleeping blocked the gateway's schema setup for minutes."""
    states = []

    def sleep(seconds):
        with psycopg.connect(DSN) as probe:
            states.extend(s for (s,) in probe.execute(
                "SELECT state FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"))

    conn.execute("UPDATE catalog.terms SET done_at = now()")  # nothing to do: it sleeps until tomorrow
    conn.commit()
    finder.run(DSN, lambda: FakeKroger({}), finder.Settings({}), sleep=sleep, log=lambda m: None,
               max_steps=1, off_check=lambda c, keys: True)
    assert "idle in transaction" not in states
