"""kroger_sync: barcode conversion, parsing, lookups with a fake Kroger API, runs."""

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from kroger_sync.client import KrogerClient, parse_product
from kroger_sync.gtin import kroger_keys, to_kroger_id

USDA_SAMPLE = str(Path(__file__).parent / "fixtures" / "usda")
CHIPS, BAR, CHOCOLATE = "012345678905", "0098765432109", "033333333333"


def test_barcode_to_kroger_id_drops_the_check_digit():
    assert to_kroger_id("036000291452") == "0003600029145"  # valid check digit dropped
    assert to_kroger_id("0098765432109") == "0098765432109"  # no valid check digit: as-is
    assert kroger_keys(to_kroger_id("036000291452")) == ["36000291452"]
    assert to_kroger_id("") is None


def kroger_product(kid, desc, price=3.99, promo=0):
    return {"productId": kid, "upc": kid, "brand": "Test", "description": desc, "categories": ["Snacks"],
            "images": [{"perspective": "front", "sizes": [{"size": "large", "url": f"https://img/{kid}.jpg"}]}],
            "items": [{"size": "5 oz", "price": {"regular": price, "promo": promo},
                       "fulfillment": {"inStore": True}, "inventory": {"stockLevel": "HIGH"}}],
            "aisleLocations": [{"description": "Aisle 12"}]}


CATALOG = {to_kroger_id(CHIPS): kroger_product(to_kroger_id(CHIPS), "Avocado Oil Kettle Chips", 4.49, 3.99),
           to_kroger_id(CHOCOLATE): kroger_product(to_kroger_id(CHOCOLATE), "85% Dark Chocolate", 2.99)}


class FakeKroger:
    """Token endpoint, /products?filter.productId=a,b and /products/{id}."""

    def __init__(self, reject_lists=False):
        self.reject_lists, self.urls = reject_lists, []

    def __call__(self, method, url, headers, data=None):
        if url.endswith("/connect/oauth2/token"):
            return 200, json.dumps({"access_token": "t", "expires_in": 1800}).encode()
        self.urls.append(url)
        parsed = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if parsed.path.endswith("/products"):
            if self.reject_lists:
                return 400, b"filter.productId must be a single id"
            ids = q["filter.productId"].split(",")
            return 200, json.dumps({"data": [CATALOG[i] for i in ids if i in CATALOG]}).encode()
        pid = parsed.path.rsplit("/", 1)[-1]
        if pid in CATALOG:
            return 200, json.dumps({"data": CATALOG[pid]}).encode()
        return 404, b"not found"


def test_parse_product_reads_price_aisle_image():
    p = parse_product(CATALOG[to_kroger_id(CHIPS)])
    assert (p["price_regular"], p["price_promo"], p["aisle"]) == (4.49, 3.99, "Aisle 12")
    assert p["image_url"].endswith(".jpg") and p["in_store"] is True


psycopg = pytest.importorskip("psycopg")
DSN = os.getenv("OFF_TEST_DATABASE_URL")
pg = pytest.mark.skipif(not DSN, reason="set OFF_TEST_DATABASE_URL to run Postgres tests")


@pytest.fixture
def conn():
    from off_products.pg import HashEmbedder, load
    from kroger_sync import sync

    with psycopg.connect(DSN, autocommit=True) as c:
        for t in ("products", "product_tags", "product_ingredients"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
        c.execute("DROP SCHEMA IF EXISTS kroger CASCADE")
    load(USDA_SAMPLE, DSN, HashEmbedder(), source="usda", progress=lambda *_: None)
    with psycopg.connect(DSN) as c:
        sync.init_schema(c)
        sync.set_setting(c, "location_id", "01400943")
        yield c


@pg
def test_lookup_caches_found_and_not_found(conn):
    from kroger_sync import sync

    fake = FakeKroger()
    out = sync.items_for(conn, [CHIPS, BAR], KrogerClient("id", "secret", fetch=fake))
    assert out["items"][CHIPS]["sold"] and out["items"][CHIPS]["price"] == 3.99
    assert out["items"][CHIPS]["on_sale"] and out["items"][CHIPS]["aisle"] == "Aisle 12"
    assert out["items"][BAR]["sold"] is False
    assert out["lookup"]["calls"] == 1  # both barcodes in one call
    # Cached: asking again makes no calls.
    again = sync.items_for(conn, [CHIPS, BAR], KrogerClient("id", "secret", fetch=fake))
    assert again["lookup"].get("calls", 0) == 0 and len(fake.urls) == 1


@pg
def test_falls_back_to_single_lookups_when_lists_are_rejected(conn):
    from kroger_sync import sync

    fake = FakeKroger(reject_lists=True)
    out = sync.items_for(conn, [CHIPS, BAR, CHOCOLATE], KrogerClient("id", "secret", fetch=fake))
    assert {c for c, v in out["items"].items() if v["sold"]} == {CHIPS, CHOCOLATE}


@pg
def test_daily_budget_stops_lookups(conn, monkeypatch):
    from kroger_sync import sync

    monkeypatch.setenv("KROGER_MAX_CALLS_PER_DAY", "0")
    out = sync.items_for(conn, [CHIPS], KrogerClient("id", "secret", fetch=FakeKroger()))
    assert out["lookup"]["budget_reached"] == 1 and out["items"] == {}


@pg
def test_load_run_over_a_category_and_coverage(conn):
    from kroger_sync import sync

    run_id = sync.start_run(conn, "load", {"categories": ["cat:snacks"]})
    assert sync.start_run(conn, "load", {}) is None  # one run at a time
    sync.run_sync(DSN, run_id, "load", ["cat:snacks"],
                  make_client=lambda: KrogerClient("id", "secret", fetch=FakeKroger()))
    run = sync.latest_run(conn)
    assert run["status"] == "done", run["message"]
    assert run["stats"]["in_scope"] == 5 and run["stats"]["found"] == 2  # 3 chips, bar, chocolate
    cov = sync.coverage(conn)
    assert cov["sold"] == 2 and any(r["category"] == "cat:snacks" and r["sold"] == 2 for r in cov["by_category"])


@pg
def test_product_reload_keeps_kroger_data(conn):
    from kroger_sync import sync
    from off_products.pg import HashEmbedder, load

    sync.items_for(conn, [CHIPS], KrogerClient("id", "secret", fetch=FakeKroger()))
    load(USDA_SAMPLE, DSN, HashEmbedder(), source="usda", progress=lambda *_: None)
    assert sync.items_for(conn, [CHIPS])["items"][CHIPS]["sold"]
