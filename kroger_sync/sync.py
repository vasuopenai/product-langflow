"""Kroger lookups by barcode, stored in the ``kroger`` schema.

The product store already has every product's barcode (from USDA), so nothing is
crawled: each barcode is looked up in the Kroger API (product id = UPC without its
check digit) and the answer is cached in kroger.items, including "not sold at
Kroger", so the same barcode isn't asked again until it is stale.

Nothing here writes to the product store's own tables: public.products and
public.product_tags are only read. Product loads, ``pg-rederive`` and search are
never affected, and a product reload never loses Kroger data.

  on demand  items_for(): the UI asks about the products it is showing; unknown or
             stale barcodes are looked up live (one call per 50) and cached
  load       run_sync("load"): look up every barcode in the chosen categories that
             isn't cached yet; stops at the daily call budget, the next run continues
  refresh    run_sync("refresh"): look up the chosen categories again (fresh prices)
"""

import datetime as dt
import json
import os
import traceback
from collections import Counter

import psycopg
from psycopg.types.json import Jsonb

from .client import KrogerClient, KrogerError, configured, parse_product
from .gtin import to_kroger_id

SCHEMA = [
    "CREATE SCHEMA IF NOT EXISTS kroger",
    "CREATE TABLE IF NOT EXISTS kroger.settings (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS kroger.items (
        code TEXT NOT NULL, kroger_id TEXT, location_id TEXT NOT NULL, found BOOLEAN NOT NULL,
        brand TEXT, description TEXT, size TEXT,
        price_regular DOUBLE PRECISION, price_promo DOUBLE PRECISION, in_store BOOLEAN,
        stock_level TEXT, aisle TEXT, image_url TEXT, raw JSONB, fetched_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (code, location_id))""",
    "CREATE INDEX IF NOT EXISTS kroger_items_found ON kroger.items (found)",
    "CREATE TABLE IF NOT EXISTS kroger.calls (day DATE PRIMARY KEY, n INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS kroger.runs (
        id SERIAL PRIMARY KEY, mode TEXT, status TEXT, started_at TIMESTAMPTZ DEFAULT now(),
        finished_at TIMESTAMPTZ, message TEXT, stats JSONB, scope JSONB)""",
]
MODES = ("load", "refresh")
MAX_AGE_DAYS = int(os.getenv("KROGER_MAX_AGE_DAYS", "7"))


def budget():
    return int(os.getenv("KROGER_MAX_CALLS_PER_DAY", "9000"))


def init_schema(conn):
    for sql in SCHEMA:
        conn.execute(sql)
    # Caches made when there was one store for everyone are keyed by barcode only;
    # key them by barcode and store, so each app user gets their own store's prices.
    pk = conn.execute(
        """SELECT array_agg(a.attname::text ORDER BY a.attname) FROM pg_index i
           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
           WHERE i.indrelid = 'kroger.items'::regclass AND i.indisprimary""").fetchone()[0]
    if pk == ["code"]:
        conn.execute("DELETE FROM kroger.items WHERE location_id IS NULL")
        conn.execute("ALTER TABLE kroger.items DROP CONSTRAINT items_pkey")
        conn.execute("ALTER TABLE kroger.items ALTER COLUMN location_id SET NOT NULL")
        conn.execute("ALTER TABLE kroger.items ADD PRIMARY KEY (code, location_id)")
    conn.commit()


def get_setting(conn, name):
    row = conn.execute("SELECT value FROM kroger.settings WHERE key = %s", (name,)).fetchone()
    return row[0] if row else None


def set_setting(conn, name, value):
    conn.execute("INSERT INTO kroger.settings VALUES (%s, %s) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (name, value))
    conn.commit()


def calls_today(conn):
    row = conn.execute("SELECT n FROM kroger.calls WHERE day = CURRENT_DATE").fetchone()
    return row[0] if row else 0


def _add_calls(conn, n):
    if n:
        conn.execute("INSERT INTO kroger.calls VALUES (CURRENT_DATE, %s) "
                     "ON CONFLICT (day) DO UPDATE SET n = kroger.calls.n + EXCLUDED.n", (n,))


def _stale(conn, codes, location_id, max_age_days):
    """The codes with no cached answer for this store, or one older than max_age_days."""
    fresh = {c for (c,) in conn.execute(
        "SELECT code FROM kroger.items WHERE code = ANY(%s) AND location_id = %s "
        "AND fetched_at > now() - make_interval(days => %s)",
        (list(codes), location_id, max_age_days))}
    return [c for c in codes if c not in fresh]


def lookup(conn, client, codes, location_id, max_age_days=MAX_AGE_DAYS, call_budget=None,
           progress=None):
    """Look up stale barcodes in the Kroger API and cache the answers (found or not).
    Returns {"looked_up", "found", "calls", "budget_reached"}."""
    call_budget = budget() if call_budget is None else call_budget
    stats = Counter()
    todo = {}
    for code in _stale(conn, codes, location_id, max_age_days):
        kid = to_kroger_id(code)
        if kid:
            todo[kid] = code
    ids = list(todo)
    for i in range(0, len(ids), client.BATCH):
        if calls_today(conn) >= call_budget:
            stats["budget_reached"] = 1
            break
        batch = ids[i:i + client.BATCH]
        before = client.calls
        products = client.products(batch, location_id)
        _add_calls(conn, client.calls - before)
        stats["calls"] += client.calls - before
        found = {}
        for raw in products:
            p = parse_product(raw)
            code = next((todo[k] for k in [p["product_id"], (p["upc"] or "").zfill(13)] if k in todo), None)
            if code:
                found[code] = (p, raw)
        now = dt.datetime.now(dt.timezone.utc)
        rows = []
        for kid in batch:
            code = todo[kid]
            p, raw = found.get(code, ({}, None))
            rows.append((code, kid, location_id, code in found, p.get("brand"), p.get("description"),
                         p.get("size"), p.get("price_regular"), p.get("price_promo"), p.get("in_store"),
                         p.get("stock_level"), p.get("aisle"), p.get("image_url"),
                         Jsonb(raw) if raw else None, now))
        conn.cursor().executemany(
            """INSERT INTO kroger.items VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (code, location_id) DO UPDATE SET kroger_id = EXCLUDED.kroger_id,
                 found = EXCLUDED.found, brand = EXCLUDED.brand,
                 description = EXCLUDED.description, size = EXCLUDED.size,
                 price_regular = EXCLUDED.price_regular, price_promo = EXCLUDED.price_promo,
                 in_store = EXCLUDED.in_store, stock_level = EXCLUDED.stock_level,
                 aisle = EXCLUDED.aisle, image_url = EXCLUDED.image_url, raw = EXCLUDED.raw,
                 fetched_at = EXCLUDED.fetched_at""", rows)
        conn.commit()
        stats["looked_up"] += len(batch)
        stats["found"] += len(found)
        if progress:
            progress(f"looked up {stats['looked_up']} of {len(ids)}, {stats['found']} sold at Kroger")
    return dict(stats)


def items_for(conn, codes, client=None, location_id=None):
    """Kroger info for the given store barcodes at one store (``location_id``, default:
    the store chosen in settings). With a client, unknown or stale barcodes are looked
    up first."""
    codes = [c for c in dict.fromkeys(codes) if c]
    lookup_stats = {}
    location = location_id or get_setting(conn, "location_id")
    if not location:
        return {"items": {}, "lookup": {}}
    if client is not None and location and codes:
        try:
            lookup_stats = lookup(conn, client, codes, location)
        except KrogerError as e:
            lookup_stats = {"error": str(e)}
    items = {}
    for code, found, desc, size, price, promo, aisle, image, in_store, stock, fetched, loc in conn.execute(
        """SELECT code, found, description, size, price_regular, price_promo, aisle, image_url,
                  in_store, stock_level, fetched_at, location_id
           FROM kroger.items WHERE code = ANY(%s) AND location_id = %s""", (codes, location)):
        # Kroger answers with its catalog entry even when the store doesn't carry the
        # item (inStore false, no price); only count it as sold there when it does.
        sold = bool(found and (in_store or price is not None))
        items[code] = {"sold": sold, "in_catalog": bool(found), "description": desc, "size": size,
                       "price": promo or price, "regular_price": price, "on_sale": bool(promo),
                       "aisle": aisle, "image_url": image, "in_store": in_store, "stock_level": stock,
                       "as_of": fetched.isoformat(), "location_id": loc}
    return {"items": items, "lookup": lookup_stats}


def scope_codes(conn, categories=None):
    """Barcodes of the products to load: all, or those in any of the given categories."""
    if categories:
        rows = conn.execute("SELECT DISTINCT code FROM product_tags WHERE kind = 'category' AND tag = ANY(%s)",
                            (list(categories),))
    else:
        rows = conn.execute("SELECT code FROM products")
    return [c for (c,) in rows]


def coverage(conn):
    """Per product category: cached lookups and how many are sold at Kroger."""
    rows = conn.execute(
        """SELECT t.tag, count(*), count(*) FILTER (WHERE (i.found AND (i.in_store OR i.price_regular IS NOT NULL)))
           FROM kroger.items i JOIN product_tags t ON t.code = i.code AND t.kind = 'category'
           GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    total, sold = conn.execute("SELECT count(*), count(*) FILTER (WHERE (found AND (in_store OR price_regular IS NOT NULL))) FROM kroger.items").fetchone()
    return {"looked_up": total, "sold": sold,
            "by_category": [{"category": c, "looked_up": n, "sold": s} for c, n, s in rows]}


# --- runs (what the UI triggers) ---------------------------------------------------

def start_run(conn, mode, scope):
    """Record a new run unless one is already running. Returns its id or None."""
    if conn.execute("SELECT 1 FROM kroger.runs WHERE status = 'running'").fetchone():
        return None
    run_id = conn.execute(
        "INSERT INTO kroger.runs (mode, status, message, scope) VALUES (%s, 'running', 'starting', %s) "
        "RETURNING id", (mode, Jsonb(scope))).fetchone()[0]
    conn.commit()
    return run_id


def _update_run(dsn, run_id, **fields):
    sets = ", ".join(f"{k} = %s" for k in fields)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"UPDATE kroger.runs SET {sets} WHERE id = %s",
                  [Jsonb(v) if k == "stats" else v for k, v in fields.items()] + [run_id])


def run_sync(dsn, run_id, mode, categories=None, make_client=KrogerClient):
    """The job behind the UI's Load / Refresh buttons. Progress and the outcome go to
    kroger.runs for the UI to poll."""
    stats = {}
    try:
        with psycopg.connect(dsn) as conn:
            location = get_setting(conn, "location_id")
            if not location:
                raise KrogerError("choose a Kroger store first")
            codes = scope_codes(conn, categories)
            _update_run(dsn, run_id, message=f"{len(codes)} products in scope")
            stats = lookup(conn, make_client(), codes, location,
                           max_age_days=0 if mode == "refresh" else MAX_AGE_DAYS,
                           progress=lambda m: _update_run(dsn, run_id, message=m))
            stats["in_scope"] = len(codes)
        msg = ("daily call budget reached - run Load again tomorrow to continue"
               if stats.get("budget_reached") else "done")
        _update_run(dsn, run_id, status="done", message=msg, stats=stats,
                    finished_at=dt.datetime.now(dt.timezone.utc))
    except Exception as e:  # recorded for the UI; the service keeps running
        _update_run(dsn, run_id, status="failed", message=f"{type(e).__name__}: {e}", stats=stats,
                    finished_at=dt.datetime.now(dt.timezone.utc))
        traceback.print_exc()


def mark_interrupted(conn):
    """Runs left 'running' by a stopped service can't finish; mark them."""
    conn.execute("UPDATE kroger.runs SET status = 'interrupted', finished_at = now() WHERE status = 'running'")
    conn.commit()


def latest_run(conn):
    row = conn.execute("SELECT id, mode, status, started_at, finished_at, message, stats, scope "
                       "FROM kroger.runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    rid, mode, status_, started, finished, message, stats, scope = row
    return {"id": rid, "mode": mode, "status": status_, "message": message, "stats": stats or {},
            "scope": scope, "started_at": started and started.isoformat(),
            "finished_at": finished and finished.isoformat()}


def status(conn):
    total, sold = conn.execute("SELECT count(*), count(*) FILTER (WHERE (found AND (in_store OR price_regular IS NOT NULL))) FROM kroger.items").fetchone()
    return {
        "configured": configured(),
        "location_id": get_setting(conn, "location_id"),
        "location_name": get_setting(conn, "location_name"),
        "looked_up": total, "sold": sold,
        "calls_today": calls_today(conn), "daily_budget": budget(),
        "last_run": latest_run(conn),
    }


def dumps(obj):
    return json.dumps(obj, indent=2, default=str)

