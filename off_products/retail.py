"""Join a Kroger crawl to the USDA index and to the loaded product store.

Outputs:
  * coverage CSV: one row per Kroger product with where its label data is found
  * summary: counts per Kroger category
  * in the product store (SQLite or Postgres): product_tags rows
    (code, 'retailer', 'kroger') and a retailer_items table with price, aisle
    and fetch time, so searches can filter on "available at Kroger"
"""

import csv
import sqlite3
from collections import Counter, defaultdict

from .gtin import key
from .kroger import iter_crawled
from .usda_index import lookup

RETAILER_ITEMS_DDL = (
    "CREATE TABLE IF NOT EXISTS retailer_items (code TEXT, retailer TEXT, retailer_product_id TEXT, "
    "upc TEXT, description TEXT, size TEXT, price_regular DOUBLE PRECISION, price_promo DOUBLE PRECISION, "
    "aisle TEXT, location_id TEXT, fetched_at TEXT)"
)

REPORT_FIELDS = [
    "product_id", "upc", "brand", "description", "category", "size", "price_regular",
    "usda_fdc_id", "usda_has_ingredients", "store_code", "kroger_has_nutrition",
    "kroger_has_ingredients", "label_source",
]


def store_keys(conn):
    """canonical barcode key -> product code, for every product in the store."""
    rows = conn.execute("SELECT code FROM products").fetchall()
    return {key(code): code for (code,) in rows if key(code)}


def match(kroger_jsonl, usda_db_path=None, keys_to_code=None):
    usda_db = sqlite3.connect(usda_db_path) if usda_db_path else None
    keys_to_code = keys_to_code or {}
    rows = []
    for p in iter_crawled(kroger_jsonl):
        usda = lookup(usda_db, p["keys"]) if usda_db else None
        code = next((keys_to_code[k] for k in p["keys"] if k in keys_to_code), None)
        nut = p["nutrition"] or {}
        has_kroger_ing = bool(nut.get("ingredients"))
        usda_ing = bool(usda and (usda.get("ingredients") or "").strip())
        source = ("store" if code else "usda" if usda_ing else "kroger" if has_kroger_ing else "none")
        rows.append({
            "product_id": p["product_id"], "upc": p["upc"], "brand": p["brand"],
            "description": p["description"], "category": (p["categories"] or [""])[0],
            "size": p["size"], "price_regular": p["price_regular"],
            "usda_fdc_id": usda and usda["fdc_id"], "usda_has_ingredients": usda_ing,
            "store_code": code, "kroger_has_nutrition": bool(nut.get("nutrients")),
            "kroger_has_ingredients": has_kroger_ing, "label_source": source,
            "_parsed": p,
        })
    if usda_db:
        usda_db.close()
    return rows


def write_report(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def summarize(rows):
    """Per category: total, in store, in USDA (with ingredients), any label, none."""
    by_cat = defaultdict(Counter)
    for r in rows:
        for cat in (r["category"] or "(none)", "ALL"):
            c = by_cat[cat]
            c["total"] += 1
            c["in_store"] += bool(r["store_code"])
            c["usda_ingredients"] += r["usda_has_ingredients"]
            c["kroger_ingredients"] += r["kroger_has_ingredients"]
            c["any_label"] += r["label_source"] != "none"
    return dict(sorted(by_cat.items(), key=lambda kv: (kv[0] != "ALL", -kv[1]["total"])))


def link_to_store(conn, rows, ph="?", retailer="kroger"):
    """Tag matched products as sold by the retailer and record price/aisle."""
    conn.execute(RETAILER_ITEMS_DDL)
    conn.execute(f"DELETE FROM product_tags WHERE kind = 'retailer' AND tag = {ph}", (retailer,))
    conn.execute(f"DELETE FROM retailer_items WHERE retailer = {ph}", (retailer,))
    matched = [r for r in rows if r["store_code"]]
    cur = conn.cursor()
    cur.executemany(
        f"INSERT INTO product_tags (code, kind, tag) VALUES ({ph}, 'retailer', {ph})",
        sorted({(r["store_code"], retailer) for r in matched}),
    )
    cur.executemany(
        f"INSERT INTO retailer_items VALUES ({', '.join([ph] * 11)})",
        [
            (r["store_code"], retailer, r["product_id"], r["upc"], r["description"], r["size"],
             r["price_regular"], r["_parsed"]["price_promo"], r["_parsed"]["aisle"],
             r["_parsed"]["location_id"], r["_parsed"]["fetched_at"])
            for r in matched
        ],
    )
    conn.commit()
    return len(matched)


def retailer_info(conn, codes, ph="?"):
    """code -> [{retailer, price, aisle, size, fetched_at}] for answer display."""
    if not codes:
        return {}
    exists = conn.execute(
        "SELECT to_regclass('retailer_items') IS NOT NULL" if ph == "%s"
        else "SELECT count(*) FROM sqlite_master WHERE name = 'retailer_items'"
    ).fetchone()[0]
    if not exists:
        return {}
    out = defaultdict(list)
    for code, retailer, price, promo, aisle, size, fetched in conn.execute(
        f"SELECT code, retailer, price_regular, price_promo, aisle, size, fetched_at FROM retailer_items "
        f"WHERE code IN ({', '.join([ph] * len(codes))})", list(codes)
    ).fetchall():
        out[code].append({"retailer": retailer, "price": promo or price, "aisle": aisle,
                          "size": size, "as_of": fetched})
    return dict(out)
