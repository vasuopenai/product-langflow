import csv
import json
import sqlite3
from pathlib import Path

import pytest

from off_products.gtin import check_digit, has_valid_check, key, kroger_keys
from off_products.kroger import KrogerClient, crawl, iter_crawled, parse_product
from off_products.query import QuerySpec, to_sql
from off_products.retail import link_to_store, match, retailer_info, store_keys, summarize, write_report
from off_products.store import build
from off_products.usda import build_index

SAMPLE = Path(__file__).parent / "fixtures" / "sample_products.jsonl"

# Real-format barcodes: UPC-A 036000291452 and 011110417008 (valid check digits).
BAR_UPC, CHIPS_UPC = "036000291452", "011110417008"


def test_gtin_normalization():
    assert check_digit("03600029145") == "2"
    assert has_valid_check(BAR_UPC)
    assert key("00036000291452") == key("036000291452") == "36000291452"
    # Kroger drops the check digit and pads to 13
    assert kroger_keys("0003600029145")[0] == "36000291452"
    assert kroger_keys("") == []


def kroger_product(pid, upc, desc, category, price=3.99, nutrition=None):
    p = {
        "productId": pid, "upc": upc, "brand": "Test", "description": desc,
        "categories": [category],
        "images": [{"perspective": "front", "sizes": [{"size": "large", "url": f"https://img/{pid}.jpg"}]}],
        "items": [{"itemId": pid, "size": "2 oz", "price": {"regular": price, "promo": 0},
                   "fulfillment": {"inStore": True}, "inventory": {"stockLevel": "HIGH"}}],
        "aisleLocations": [{"description": "Aisle 12"}],
    }
    if nutrition:
        p["nutritionInformation"] = [nutrition]
    return p


CATALOG = {
    "protein bar": [
        kroger_product("1", "0003600029145", "Chocolate Almond Protein Bar", "Snacks"),
        kroger_product("2", "0004900000004", "Store Brand Bar", "Snacks", nutrition={
            "ingredientStatement": "Dates, almonds, whey protein",
            "servingSize": {"quantity": 50, "unitOfMeasure": {"abbreviation": "g"}},
            "nutrients": [{"displayName": "Protein", "quantity": 12,
                           "unitOfMeasure": {"abbreviation": "g"}, "percentDailyIntake": 24}],
        }),
    ],
    "potato chips": [
        kroger_product("3", "0001111041700", "Avocado Oil Sea Salt Potato Chips", "Snacks", 4.49),
        kroger_product("1", "0003600029145", "Chocolate Almond Protein Bar", "Snacks"),  # duplicate
        kroger_product("4", "0007777700000", "Mystery Chips", "Snacks"),
    ],
}


class FakeKroger:
    """Token endpoint + /products with paging; can inject one 429."""

    def __init__(self, fail_once_with_429=False):
        self.requests, self.fail = [], fail_once_with_429

    def __call__(self, method, url, headers, data=None):
        self.requests.append((method, url))
        if url.endswith("/connect/oauth2/token"):
            assert headers["Authorization"].startswith("Basic ")
            return 200, json.dumps({"access_token": "t", "expires_in": 1800}).encode()
        if self.fail:
            self.fail = False
            return 429, b"slow down"
        from urllib.parse import parse_qs, urlparse

        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        items = CATALOG.get(q["filter.term"], [])
        start, limit = int(q["filter.start"]), int(q["filter.limit"])
        return 200, json.dumps({"data": items[start - 1:start - 1 + limit]}).encode()


def client(fake):
    return KrogerClient("id", "secret", fetch=fake, sleep=lambda s: None)


def test_crawl_pages_dedupes_retries_and_resumes(tmp_path):
    fake = FakeKroger(fail_once_with_429=True)
    n = crawl(client(fake), ["protein bar", "potato chips"], tmp_path, "01400943",
              page_size=2, progress=lambda *_: None)
    assert n == 4  # product 1 appears under both terms but is written once
    rows = list(iter_crawled(tmp_path / "products.jsonl"))
    assert {r["product_id"] for r in rows} == {"1", "2", "3", "4"}
    assert rows[0]["location_id"] == "01400943"

    # second run: everything done, no product calls
    fake2 = FakeKroger()
    assert crawl(client(fake2), ["protein bar", "potato chips"], tmp_path, progress=lambda *_: None) == 0
    assert not [u for m, u in fake2.requests if "/products" in u]


def test_crawl_stops_at_daily_budget(tmp_path):
    msgs = []
    crawl(client(FakeKroger()), ["protein bar", "potato chips"], tmp_path, page_size=1,
          max_calls_per_day=2, progress=msgs.append)
    state = json.loads((tmp_path / "state.json").read_text())
    assert sum(state["calls_by_day"].values()) == 2
    assert any("budget" in m for m in msgs)


def test_parse_product_extracts_nutrition_price_aisle():
    p = parse_product(CATALOG["protein bar"][1])
    assert p["keys"][0] == "49000000047"  # Kroger 0004900000004 + computed check digit
    assert p["price_regular"] == 3.99 and p["price_promo"] is None
    assert p["aisle"] == "Aisle 12" and p["image_url"].endswith("2.jpg")
    assert p["nutrition"]["ingredients"].startswith("Dates")
    assert p["nutrition"]["nutrients"]["Protein"] == {"quantity": 12, "unit": "g", "percent_daily": 24}
    assert parse_product(CATALOG["protein bar"][0])["nutrition"] is None


def write_usda_csv(folder):
    folder.mkdir()
    cols = ["fdc_id", "brand_owner", "gtin_upc", "ingredients", "branded_food_category", "available_date"]
    with open(folder / "branded_food.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerow(["100", "Coastline", "00011110417008", "potatoes, avocado oil, sea salt", "Chips", "2024-01-01"])
        w.writerow(["101", "Coastline", "011110417008", "potatoes, avocado oil, salt", "Chips", "2025-06-01"])
        w.writerow(["200", "Other", "00049000000047", "", "Bars", "2025-01-01"])
    with open(folder / "food.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fdc_id", "description"])
        w.writerows([["100", "OLD CHIPS"], ["101", "AVOCADO OIL CHIPS"], ["200", "BAR"]])


@pytest.fixture
def setup(tmp_path):
    # product store with real-format barcodes on two sample products
    recoded = tmp_path / "products.jsonl"
    with open(SAMPLE) as src, open(recoded, "w") as out:
        for line in src:
            p = json.loads(line)
            p["code"] = {"0000000000011": BAR_UPC, "0000000000021": CHIPS_UPC}.get(p["code"], p["code"])
            out.write(json.dumps(p) + "\n")
    store = tmp_path / "store.db"
    build(str(recoded), str(store))

    write_usda_csv(tmp_path / "usda")
    usda_db = tmp_path / "usda.db"
    read, unique = build_index(str(tmp_path / "usda"), str(usda_db), progress=lambda *_: None)
    assert (read, unique) == (3, 2)  # two records share a barcode; newest kept

    crawl(client(FakeKroger()), ["protein bar", "potato chips"], tmp_path / "kroger", "01400943",
          progress=lambda *_: None)
    return tmp_path, store, usda_db


def test_match_report_and_link(setup):
    tmp_path, store_path, usda_db = setup
    conn = sqlite3.connect(store_path)
    rows = match(tmp_path / "kroger" / "products.jsonl", str(usda_db), store_keys(conn))
    by_id = {r["product_id"]: r for r in rows}

    assert by_id["1"]["store_code"] == BAR_UPC and by_id["1"]["label_source"] == "store"
    assert by_id["3"]["store_code"] == CHIPS_UPC
    assert by_id["3"]["usda_fdc_id"] == "101"  # newest USDA record for the barcode
    assert by_id["2"]["store_code"] is None and by_id["2"]["usda_fdc_id"] == "200"
    assert by_id["2"]["label_source"] == "kroger"  # USDA has no ingredients, Kroger does
    assert by_id["4"]["label_source"] == "none"

    summary = summarize(rows)["ALL"]
    assert summary["total"] == 4 and summary["in_store"] == 2 and summary["any_label"] == 3

    write_report(rows, tmp_path / "coverage.csv")
    assert (tmp_path / "coverage.csv").read_text().splitlines()[0].startswith("product_id,upc")

    assert link_to_store(conn, rows) == 2
    assert link_to_store(conn, rows) == 2  # idempotent re-run

    sql, params = to_sql(QuerySpec(semantic_query="snack", retailers_any=["kroger"]))
    assert sorted(r[0] for r in conn.execute(sql, params)) == sorted([BAR_UPC, CHIPS_UPC])

    info = retailer_info(conn, [CHIPS_UPC, "nope"])
    assert info[CHIPS_UPC][0]["price"] == 4.49 and info[CHIPS_UPC][0]["aisle"] == "Aisle 12"
