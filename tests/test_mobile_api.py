"""mobile_api: barcodes, research sources, merging, nearby stores, and the API flow
scan -> research -> review -> approve -> found. No real network calls."""

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_api import sources, staging, stores
from mobile_api.barcodes import key, store_variants, upce_to_upca

USDA_SAMPLE = str(Path(__file__).parent / "fixtures" / "usda")
NEW = "049000028911"  # not in the fixture


def test_barcodes():
    assert upce_to_upca("04252614") == "042100005264"
    assert key("04252614") == "42100005264"  # UPC-E expanded
    assert key("0049000028911") == key("049000028911") == "49000028911"
    assert store_variants("49000028911")[:2] == ["049000028911", "0049000028911"]


def fake_fetch(responses):
    def fetch(url, headers=None, timeout=20):
        for part, (status, body) in responses.items():
            if part in url:
                return status, body
        return 404, None
    return fetch


FDC = {"foods": [
    {"fdcId": 9, "gtinUpc": "00000000000000", "description": "OTHER"},
    {"fdcId": 123, "gtinUpc": "0049000028911", "description": "COLA", "brandOwner": "The Coca-Cola Company",
     "brandName": "COCA-COLA", "ingredients": "CARBONATED WATER, SUGAR", "servingSize": 355,
     "servingSizeUnit": "ml", "householdServingFullText": "1 can", "brandedFoodCategory": "Soda",
     "foodNutrients": [{"nutrientId": 1008, "value": 39}, {"nutrientId": 2000, "value": 10.6},
                       {"nutrientId": 1093, "value": 12}]}]}
OFF = {"status": 1, "product": {"product_name": "Coke", "brands": "Coca-Cola",
                                "ingredients_text": "water, sugar", "image_front_url": "https://img/coke.jpg",
                                "nutriments": {"energy-kcal_100g": 42, "proteins_100g": 0, "sodium_100g": 0.01},
                                "labels_tags": ["en:vegan"]}}


def test_usda_live_matches_the_barcode_and_maps_nutrients():
    r = sources.usda_live(NEW, fake_fetch({"api.nal.usda.gov": (200, FDC)}))
    assert r["fdc_id"] == "123" and r["draft"]["name"] == "COLA"
    assert r["draft"]["nutrients_100g"] == {"energy_kcal": 39, "sugars_g": 10.6, "sodium_mg": 12}
    assert r["draft"]["serving_unit"] == "ml" and r["draft"]["serving_size_g"] == 355
    assert sources.usda_live("000000000017", fake_fetch({"api.nal.usda.gov": (200, FDC)})) is None


def test_open_food_facts_converts_sodium_to_mg():
    r = sources.open_food_facts(NEW, fake_fetch({"openfoodfacts": (200, OFF)}))
    assert r["draft"]["nutrients_100g"]["sodium_mg"] == 10 and r["draft"]["image_url"].endswith("coke.jpg")


class FakeOpenAI:
    def __init__(self, text, cite=True):
        cites = [SimpleNamespace(type="url_citation", url="https://shop.example/coke", title="Coke")] if cite else []
        msg = SimpleNamespace(content=[SimpleNamespace(annotations=cites)])
        self.responses = SimpleNamespace(create=lambda **kw: SimpleNamespace(output_text=text, output=[msg]))

    @classmethod
    def without_citations(cls, text):
        return cls(text, cite=False)


def test_web_search_parses_json_and_converts_to_per_100g():
    text = ('Here you go: {"found": true, "name": "Coca-Cola Classic", "brand": "Coca-Cola", '
            '"serving_size_g": 355, "nutrition_per_serving": {"energy_kcal": 140, "sugars_g": 39}, '
            '"confidence": "medium"}')
    r = sources.web(NEW, FakeOpenAI(text))
    assert r["draft"]["nutrients_100g"] == {"energy_kcal": 39.44, "sugars_g": 10.99}
    assert r["url"] == "https://shop.example/coke" and r["confidence"] == "medium"
    assert sources.web(NEW, FakeOpenAI('{"found": false}')) is None


def test_merge_prefers_usda_then_off_then_web_and_records_sources():
    found = {"usda": {"draft": {"name": "COLA", "nutrients_100g": {"energy_kcal": 39}}},
             "open_food_facts": {"draft": {"name": "Coke", "image_url": "u", "nutrients_100g": {"energy_kcal": 42, "protein_g": 0}}},
             "kroger": {"draft": {"package_size": "12 fl oz"}}}
    draft, origin = staging.merge(found)
    assert draft["name"] == "COLA" and draft["image_url"] == "u" and draft["package_size"] == "12 fl oz"
    assert draft["nutrients_100g"] == {"energy_kcal": 39, "protein_g": 0}
    assert origin["name"] == "usda" and origin["nutrients_100g.protein_g"] == "open_food_facts"
    assert staging.confidence(found) == "high"


def test_nearby_marks_the_store_the_user_is_in():
    here = (34.9705, -80.7598)
    fake = SimpleNamespace(locations=lambda **kw: [
        {"location_id": "far", "name": "Far", "lat": 35.10, "lng": -80.70},
        {"location_id": "here", "name": "Harris Teeter", "lat": 34.9706, "lng": -80.7599}])
    stores._cache.clear()
    r = stores.nearby(*here, client_factory=lambda: fake)
    assert r["at_store"]["location_id"] == "here" and r["stores"][0]["distance_m"] < 50
    stores._cache.clear()
    r = stores.nearby(35.5, -80.0, client_factory=lambda: fake)
    assert r["at_store"] is None


psycopg = pytest.importorskip("psycopg")
DSN = os.getenv("OFF_TEST_DATABASE_URL")
pg = pytest.mark.skipif(not DSN, reason="set OFF_TEST_DATABASE_URL to run Postgres tests")


@pytest.fixture
def api(monkeypatch):
    from fastapi.testclient import TestClient
    from off_products.pg import HashEmbedder, load
    from mobile_api import api as gateway

    monkeypatch.setenv("DATABASE_URL", DSN)
    with psycopg.connect(DSN, autocommit=True) as c:
        for t in ("products", "product_tags", "product_ingredients"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
        for s in ("staging", "mobile", "kroger"):
            c.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
    load(USDA_SAMPLE, DSN, HashEmbedder(), source="usda", progress=lambda *_: None)
    monkeypatch.setattr(gateway, "_embedder", HashEmbedder())
    monkeypatch.setattr(gateway, "kroger_client", lambda: None)  # no live Kroger calls
    monkeypatch.setattr(gateway.config, "APP_KEY", None)
    monkeypatch.setattr(gateway.config, "ADMIN_KEY", None)
    found = {"usda": {"draft": {"name": "COLA", "brand": "COCA-COLA", "ingredients": "CARBONATED WATER, SUGAR",
                                "category": "Soda", "serving_size_g": 355, "serving_unit": "ml",
                                "nutrients_100g": {"energy_kcal": 39, "carbs_g": 10.6, "sugars_g": 10.6,
                                                   "protein_g": 0, "fat_g": 0}},
                      "url": "https://fdc.nal.usda.gov/food-details/123/nutrients", "fdc_id": "123", "raw": {}}}
    monkeypatch.setattr(staging, "research", lambda *a, **kw: dict(found))
    with TestClient(gateway.app) as client:
        yield client


def wait_for(client, barcode, status, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        r = client.get(f"/api/scan/{barcode}").json()
        if r["status"] == status:
            return r
        time.sleep(0.1)
    raise AssertionError(f"{barcode} never reached {status}: {r}")


@pg
def test_scan_known_product(api):
    r = api.post("/api/scan", json={"barcode": "0098765432109"}).json()
    assert r["status"] == "found" and r["product"]["name"] == "Chocolate Almond Protein Bar"
    assert r["product"]["origin"] == "usda"


@pg
def test_scan_unknown_research_review_approve(api):
    r = api.post("/api/scan", json={"barcode": NEW, "lat": 34.97, "lng": -80.76}).json()
    assert r["status"] == "researching"
    staged = wait_for(api, NEW, "pending")["staged"]
    assert staged["draft"]["name"] == "COLA" and staged["confidence"] == "high"
    api.post("/api/scan", json={"barcode": NEW})  # a second scan counts, doesn't re-research
    queue = api.get("/admin/review").json()
    assert queue["counts"] == {"pending": 1} and queue["items"][0]["scan_count"] == 2
    detail = api.get(f"/admin/review/{NEW}").json()
    assert len(detail["events"]) == 2 and detail["field_sources"]["name"] == "usda"

    ok = api.post(f"/admin/review/{NEW}/approve", json={"edits": {"name": "Coca-Cola Classic"}}).json()
    assert ok["status"] == "approved" and ok["product"]["origin"] == "scan"
    r = api.post("/api/scan", json={"barcode": NEW}).json()
    assert r["status"] == "found" and r["product"]["name"] == "Coca-Cola Classic"
    assert r["product"]["url"].startswith("https://fdc.nal.usda.gov")


@pg
def test_approved_scan_products_survive_rederive(api):
    from off_products.pg import HashEmbedder, load
    api.post("/api/scan", json={"barcode": NEW})
    wait_for(api, NEW, "pending")
    api.post(f"/admin/review/{NEW}/approve", json={})
    assert load(DSN, DSN, HashEmbedder(), source="stored", progress=lambda *_: None) == (0, 0)


@pg
def test_approve_rejects_impossible_nutrition(api):
    api.post("/api/scan", json={"barcode": NEW})
    wait_for(api, NEW, "pending")
    r = api.post(f"/admin/review/{NEW}/approve", json={"edits": {"nutrients_100g": {"protein_g": 150}}})
    assert r.status_code == 400 and "impossible" in r.json()["detail"]


@pg
def test_reject_and_app_key(api, monkeypatch):
    from mobile_api import api as gateway
    api.post("/api/scan", json={"barcode": NEW})
    wait_for(api, NEW, "pending")
    assert api.post(f"/admin/review/{NEW}/reject", json={"note": "duplicate"}).json()["status"] == "rejected"
    monkeypatch.setattr(gateway.config, "APP_KEY", "secret")
    assert api.get("/api/products/0098765432109").status_code == 401
    assert api.get("/api/products/0098765432109", headers={"X-App-Key": "secret"}).status_code == 200


@pg
def test_bulk_approve_previews_then_adds_full_drafts(api):
    from psycopg.types.json import Jsonb
    full = {"name": "Almond Butter", "brand": "Simple Truth", "category": "Natural & Organic",
            "ingredients": "Dry Roasted Almonds, Sea Salt.", "serving_size_g": 32, "serving_unit": "g",
            "nutrients_100g": {"energy_kcal": 594, "protein_g": 21.9, "fat_g": 53, "carbs_g": 18.8, "sugars_g": 3.1}}
    partial = {"name": "Mystery Chips", "brand": "B", "ingredients": "Potatoes, Oil, Salt."}
    impossible = {**full, "name": "Bad Label", "nutrients_100g": {"energy_kcal": 5000, "protein_g": 150}}
    kroger = {"kroger": {"url": None, "raw": {"productPageURI": "/p/almond-butter/0001111012345"}}}
    with psycopg.connect(DSN) as c:
        for code, draft in (("11110123450", full), ("36000291452", partial), ("77777777779", impossible)):
            c.execute("INSERT INTO staging.scanned_products (barcode, status, draft, sources, found_via) "
                      "VALUES (%s, 'queued', %s, %s, 'kroger_catalog')", (code, Jsonb(draft), Jsonb(kroger)))

    preview = api.post("/admin/review/bulk", json={"found_via": "kroger_catalog"}).json()  # dry run by default
    assert preview["matching"] == 2 and {s["name"] for s in preview["sample"]} == {"Almond Butter", "Bad Label"}
    assert api.post("/admin/review/bulk", json={"full_only": False}).json()["matching"] == 3
    with_ingredients = api.post("/admin/review/bulk", json={"full_only": False, "ingredients_only": True}).json()
    assert with_ingredients["matching"] == 3  # all three drafts have an ingredient list

    done = api.post("/admin/review/bulk", json={"found_via": "kroger_catalog", "dry_run": False}).json()
    assert done == {"approved": 1, "skipped": {"impossible nutrition": 1}, "still_matching": 1}
    product = api.get("/api/products/011110123450").json()
    assert product["name"] == "Almond Butter" and product["origin"] == "kroger"
    assert product["url"] == "https://www.kroger.com/p/almond-butter/0001111012345"
    statuses = dict(psycopg.connect(DSN).execute("SELECT barcode, status FROM staging.scanned_products").fetchall())
    assert statuses == {"11110123450": "approved", "36000291452": "queued", "77777777779": "queued"}


@pg
def test_kroger_cache_is_per_store(api):
    from kroger_sync import sync as ksync
    from kroger_sync.client import KrogerClient
    from tests.test_kroger_sync import FakeKroger, CHIPS

    with psycopg.connect(DSN) as conn:
        client = KrogerClient("id", "secret", fetch=FakeKroger())
        a = ksync.items_for(conn, [CHIPS], client, "store-a")
        b = ksync.items_for(conn, [CHIPS], client, "store-b")
        assert a["lookup"]["calls"] == 1 and b["lookup"]["calls"] == 1  # each store looked up once
        assert ksync.items_for(conn, [CHIPS], client, "store-a")["lookup"].get("calls", 0) == 0
        assert a["items"][CHIPS]["location_id"] == "store-a"


def test_web_source_urls_must_be_links_and_off_categories_english_only():
    text = ('{"found": true, "name": "Shake", "serving_size_g": 340, "nutrition_per_serving": {}, '
            '"source_urls": ["manufacturer site", "https://fairlife.com/x"]}')
    assert sources.web(NEW, FakeOpenAI.without_citations(text))["url"] == "https://fairlife.com/x"
    off = {"status": 1, "product": {"product_name": "x", "categories_tags": ["es:lacteos"]}}
    assert "category" not in sources.open_food_facts(NEW, fake_fetch({"openfoodfacts": (200, off)}))["draft"]


@pg
def test_catalog_admin_endpoints(api):
    with psycopg.connect(DSN) as c:
        c.execute("TRUNCATE catalog.seen, catalog.usda_codes, catalog.off_codes")
        c.execute("""INSERT INTO catalog.seen (product_id, code, keys, food, term, status, brand, name, category)
                     VALUES ('1', '36000291452', ARRAY['36000291452'], true, 'chips', 'staged', 'B', 'New Chips', 'Snacks')""")
    r = api.get("/admin/catalog/report").json()
    assert r["kroger_food_products"] == 1 and r["groups"]["not_in_usda"] == 1
    assert r["group_labels"]["not_in_usda"] == "Kroger products not in the USDA database"
    page = api.get("/admin/catalog/products", params={"group": "neither", "category": "snack"}).json()
    assert page["total"] == 1 and page["items"][0]["name"] == "New Chips"
    assert r["categories"] == [{"category": "Snacks", "products": 1}]
    assert api.post("/admin/catalog/stage", json={"group": "neither", "dry_run": True}).json() == {"would_stage": 1}
    assert api.post("/admin/catalog/stage", json={"group": "nope"}).status_code == 400


@pg
def test_scan_of_a_catalog_queued_barcode_starts_research(api):
    with psycopg.connect(DSN) as c:
        c.execute("ALTER TABLE staging.scanned_products ADD COLUMN IF NOT EXISTS found_via TEXT")
        c.execute("INSERT INTO staging.scanned_products (barcode, status, found_via) "
                  "VALUES (%s, 'queued', 'kroger_catalog')", (key(NEW),))
    assert api.post("/api/scan", json={"barcode": NEW}).json()["status"] == "researching"
    wait_for(api, NEW, "pending")


def test_privacy_and_support_pages_fill_in_the_contact(monkeypatch):
    from mobile_api import api, config
    monkeypatch.setattr(config, "SUPPORT_EMAIL", "help@example.com")
    for name in ("privacy", "support"):
        body = api.page(name).body.decode()
        assert 'href="mailto:help@example.com"' in body and "{{" not in body
    monkeypatch.setattr(config, "SUPPORT_EMAIL", None)
    assert "App Store page" in api.page("privacy").body.decode()


def test_card_origin_by_source():
    from mobile_api.products import origin
    assert origin({"source": {"name": "usda"}}) == "usda"
    assert origin({"source": {"name": "usda", "origin": "kroger"}}) == "kroger"
    assert origin({"code": "0018627116806"}) == "open_food_facts"  # loaded from an OFF export


@pg
def test_ask_skips_the_written_answer_and_caches_per_question_and_store(api, monkeypatch):
    import off_products.ask
    from mobile_api import api as gateway
    calls = []

    def fake_ask(conn, question, embedder, **kw):
        calls.append((question, kw))
        return {"question": question, "answer": "", "notes": [], "spec": {"semantic_query": "chips"},
                "products": [{"code": "0098765432109"}]}

    monkeypatch.setattr(off_products.ask, "ask", fake_ask)
    monkeypatch.setattr(gateway, "_asked", {})
    first = api.post("/api/ask", json={"question": "Healthy  Snacks"}).json()
    again = api.post("/api/ask", json={"question": "healthy snacks"}).json()  # same question, other spelling
    assert again == first and len(calls) == 1 and calls[0][1]["write_answer"] is False
    api.post("/api/ask", json={"question": "healthy snacks", "location_id": "01400943"})  # another store
    assert len(calls) == 2
    monkeypatch.setattr(gateway, "ASK_TTL", 0)  # expired
    api.post("/api/ask", json={"question": "healthy snacks"})
    assert len(calls) == 3


def test_usage_counts_app_actions_per_install(api):
    me, friend = {"X-Client-Id": "install-aaaaaaaa"}, {"X-Client-Id": "install-bbbbbbbb"}
    api.get("/api/products/0098765432109", headers=me)
    api.get("/api/products/0098765432109", headers=me)
    api.post("/api/scan", json={"barcode": "0098765432109"}, headers=me)
    api.get("/api/scan/0098765432109", headers=me)                 # polling isn't an action
    api.get("/api/products/0098765432109", headers=friend)
    api.get("/api/products/0098765432109")                          # no install ID (e.g. curl): not counted
    r = api.get("/admin/usage").json()
    assert r["active"] == {"1d": 2, "7d": 2, "30d": 2} and r["total_installs"] == 2
    assert r["actions_total"] == {"product_view": 3, "scan": 1}
    assert r["daily"][-1]["active"] == 2 and r["daily"][-1]["new"] == 2 and r["daily"][-1]["product_view"] == 3
    from mobile_api.usage import label
    by_install = {i["install"]: i for i in r["installs"]}
    assert len(by_install) == 2 and label("install-aaaaaaaa") != label("install-bbbbbbbb")
    mine = by_install[label("install-aaaaaaaa")]
    assert (mine["product_view"], mine["scan"], mine["days_active"]) == (2, 1, 1) and r["returning_installs"] == 0


def test_requests_are_timed_per_step_with_header_and_report(api, monkeypatch):
    import off_products.ask
    from mobile_api import api as gateway

    r = api.get("/api/products/0098765432109", headers={"X-Client-Id": "install-timing"})
    header = r.headers["Server-Timing"]
    assert "lookup;dur=" in header and "db_connect;dur=" in header and "usage;dur=" in header
    assert header.rsplit(", ", 1)[-1].startswith("total;dur=")

    monkeypatch.setattr(gateway, "_asked", {})
    monkeypatch.setattr(off_products.ask, "ask", lambda conn, q, emb, **kw: {
        "question": q, "answer": "", "notes": [], "spec": {}, "products": []})
    api.post("/api/ask", json={"question": "chips"})
    api.post("/api/ask", json={"question": "chips"})  # answered from the cache
    deadline = time.time() + 5
    while time.time() < deadline:  # rows are written off the request path
        report = api.get("/admin/timings").json()
        by_route = {x["route"]: x for x in report["routes"]}
        if by_route.get("/api/ask", {}).get("n") == 2 and "/api/products/{barcode}" in by_route:
            break
        time.sleep(0.1)
    assert by_route["/api/products/{barcode}"]["n"] == 1  # the barcode is not part of the route
    assert by_route["/api/ask"]["n"] == 2 and by_route["/api/ask"]["cache_hits"] == 1
    assert {"route": "/api/products/{barcode}", "step": "lookup"} in [
        {"route": s["route"], "step": s["step"]} for s in report["steps"]]
    slow = report["slowest"][0]
    assert slow["total_ms"] >= max(x["total_ms"] for x in report["slowest"]) and "steps" in slow
    assert api.get("/admin/timings").headers.get("Server-Timing") is None  # admin calls aren't timed


def test_timing_steps_add_up_and_cost_nothing_without_a_timer():
    from off_products import timing
    with timing.step("search"):  # no timer: nothing happens
        pass
    marks, token = timing.start()
    try:
        for _ in range(2):
            with timing.step("search"):
                time.sleep(0.01)
        timing.note("cache_hit", False)
    finally:
        timing.stop(token)
    assert marks["steps"]["search"] >= 20 and marks["meta"] == {"cache_hit": False}
    with timing.step("search"):
        pass
    assert len(marks["steps"]) == 1  # stopped: later steps aren't recorded
