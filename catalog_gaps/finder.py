"""Find food products Kroger sells that aren't in our database, and stage them for review.

    Kroger product search, term by term (50 products a call, paced, daily call budget)
      └─ each product:  in our USDA database?  ── yes ─▶ nothing to do
                        in Open Food Facts?     ── yes ─▶ nothing to do
                        neither ─▶ staging.scanned_products (status "queued", found_via "kroger_catalog")
    research (off by default; CATALOG_RESEARCH_PER_DAY) ─▶ USDA live, Open Food Facts, Kroger, web
      search ─▶ "pending" ─▶ Review tab

Kroger's official API is the only catalog read (no scraping). Everything is resumable:
progress per term in catalog.terms, every product seen in catalog.seen, usage per day in
catalog.usage. Re-running picks up where it stopped.
"""

import datetime as dt
import os
import time
from pathlib import Path

from psycopg.types.json import Jsonb

from kroger_sync.client import parse_product
from kroger_sync.gtin import kroger_keys
from mobile_api import sources, staging
from mobile_api.barcodes import key, store_variants

PAGE = 50
MAX_START = 250          # Kroger's highest filter.start
FOUND_VIA = "kroger_catalog"
TERMS_FILE = Path(__file__).with_name("terms.txt")

# Kroger department names that aren't food. A product is skipped only when all its
# categories are non-food (baby food, for example, stays).
NON_FOOD = ("beauty", "personal care", "health", "pharmacy", "cleaning", "household", "laundry", "paper",
            "pet", "kitchen", "home", "garden", "party", "office", "electronics", "toys", "apparel",
            "automotive", "tobacco", "hardware", "seasonal decor", "floral")

SCHEMA = [
    "CREATE SCHEMA IF NOT EXISTS catalog",
    """CREATE TABLE IF NOT EXISTS catalog.terms (
        term TEXT PRIMARY KEY, next_start INTEGER NOT NULL DEFAULT 1, done_at TIMESTAMPTZ,
        products INTEGER NOT NULL DEFAULT 0, staged INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS catalog.seen (
        product_id TEXT PRIMARY KEY, code TEXT, term TEXT, status TEXT NOT NULL,
        brand TEXT, name TEXT, category TEXT, seen_at TIMESTAMPTZ DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS catalog_seen_status ON catalog.seen (status)",
    """CREATE TABLE IF NOT EXISTS catalog.usage (
        day DATE NOT NULL, what TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (day, what))""",
    # Facts kept per Kroger product so the gap report can be recomputed against any reference:
    # every barcode reading (Kroger drops the check digit) and whether it's food.
    "ALTER TABLE catalog.seen ADD COLUMN IF NOT EXISTS keys TEXT[]",
    "ALTER TABLE catalog.seen ADD COLUMN IF NOT EXISTS food BOOLEAN",
    # Kroger's full product record, so any group can be staged later without crawling again.
    "ALTER TABLE catalog.seen ADD COLUMN IF NOT EXISTS raw JSONB",
    "UPDATE catalog.seen SET keys = ARRAY[code], food = status <> 'non_food' WHERE keys IS NULL AND code IS NOT NULL",
    # Every barcode in the full USDA Branded Foods file and the full Open Food Facts export
    # (canonical keys: digits, leading zeros stripped), built by `python -m catalog_gaps build-refs`.
    "CREATE TABLE IF NOT EXISTS catalog.usda_codes (code TEXT PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS catalog.off_codes (code TEXT PRIMARY KEY, us BOOLEAN NOT NULL)",
    """CREATE TABLE IF NOT EXISTS catalog.refs (
        name TEXT PRIMARY KEY, source TEXT, rows BIGINT, built_at TIMESTAMPTZ DEFAULT now())""",
    # Where a staged product came from: a shopper's scan (NULL) or this catalog crawl.
    "ALTER TABLE staging.scanned_products ADD COLUMN IF NOT EXISTS found_via TEXT",
]


class Settings:
    """Pacing and budgets, from the environment. Kroger allows 10,000 product calls a day
    per app, shared with the app's own price lookups, so the crawl takes a slice of it."""

    def __init__(self, env=os.environ):
        self.kroger_calls_per_day = int(env.get("CATALOG_KROGER_CALLS_PER_DAY", "2000"))
        self.seconds_per_call = float(env.get("CATALOG_SECONDS_PER_CALL", "6"))
        # 0 = store gaps only (analysis); research them from the Review tab or raise this later.
        self.research_per_day = int(env.get("CATALOG_RESEARCH_PER_DAY", "0"))
        self.recrawl_days = int(env.get("CATALOG_RECRAWL_DAYS", "30"))


def init_schema(conn):
    from kroger_sync import sync as kroger_sync
    staging.init_schema(conn)
    kroger_sync.init_schema(conn)  # kroger.calls: the daily Kroger call count shared with the app
    for sql in SCHEMA:
        conn.execute(sql)
    conn.commit()


def read_terms(path=TERMS_FILE):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [t.strip() for t in lines if t.strip() and not t.lstrip().startswith("#")]


def add_terms(conn, terms):
    n = 0
    for t in terms:
        n += conn.execute("INSERT INTO catalog.terms (term) VALUES (%s) ON CONFLICT DO NOTHING", (t,)).rowcount
    conn.commit()
    return n


def used(conn, what):
    row = conn.execute("SELECT n FROM catalog.usage WHERE day = CURRENT_DATE AND what = %s", (what,)).fetchone()
    return row[0] if row else 0


def _count(conn, what, n=1):
    conn.execute("INSERT INTO catalog.usage VALUES (CURRENT_DATE, %s, %s) "
                 "ON CONFLICT (day, what) DO UPDATE SET n = catalog.usage.n + EXCLUDED.n", (what, n))


def is_food(categories):
    cats = [c.lower() for c in categories or [] if c]
    return not cats or any(not any(word in c for word in NON_FOOD) for c in cats)


def in_products(conn, code):
    return conn.execute("SELECT 1 FROM products WHERE code = ANY(%s) LIMIT 1",
                        (store_variants(code),)).fetchone() is not None


def is_store_code(k):
    """Codes no outside database can know: PLU produce numbers and in-store random-weight
    labels (UPC number system 2, e.g. deli and meat counter items)."""
    return len(k) < 8 or (len(k) == 11 and k.startswith("2"))


class OffCheck:
    """Is a barcode in Open Food Facts? Uses catalog.off_codes (the full export, see
    `build-refs`) when it's loaded, otherwise Open Food Facts' public API, at most one read a second."""

    API_INTERVAL = 1.0  # seconds between API reads; Open Food Facts asks for at most 100 a minute

    def __init__(self, fetch=sources.http_json, sleep=time.sleep, clock=time.monotonic):
        self.fetch, self.sleep, self.clock = fetch, sleep, clock
        self._last = None
        self._refs = None

    def __call__(self, conn, keys):
        if self._refs is None:
            self._refs = conn.execute("SELECT EXISTS (SELECT 1 FROM catalog.off_codes)").fetchone()[0]
        if self._refs:
            return conn.execute("SELECT 1 FROM catalog.off_codes WHERE code = ANY(%s) LIMIT 1",
                                (list(keys),)).fetchone() is not None
        if self._last is not None:
            wait = self.API_INTERVAL - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()
        _count(conn, "off_api")
        return sources.open_food_facts(keys[0], self.fetch) is not None


def classify(conn, product, off_check):
    """(status, keys). Status is "in_db" | "in_off" | "staged" | "non_food" | "store_code" |
    "no_barcode"; keys are the product's barcode readings, most likely first."""
    keys = kroger_keys(product.get("upc") or product.get("productId"))
    if not keys:
        return "no_barcode", []
    if is_store_code(keys[0]):
        return "store_code", keys
    if not is_food(product.get("categories")):
        return "non_food", keys
    if any(in_products(conn, k) for k in keys):
        return "in_db", keys
    if off_check(conn, keys):
        return "in_off", keys
    return "staged", keys


def stage(conn, code, product):
    """Queue a product for research and review, with what Kroger says about it.
    Returns False if the barcode is already staged (scanned by a shopper, or staged before)."""
    p = parse_product(product)
    draft = {k: v for k, v in {"name": p["description"], "brand": p["brand"], "package_size": p["size"],
                               "image_url": p["image_url"], "category": p["category"]}.items() if v}
    created = conn.execute(
        """INSERT INTO staging.scanned_products (barcode, status, draft, field_sources, sources, found_via, scan_count)
           VALUES (%s, 'queued', %s, %s, %s, %s, 0) ON CONFLICT (barcode) DO NOTHING""",
        (key(code), Jsonb(draft), Jsonb({f: "kroger" for f in draft}),
         Jsonb({"kroger": {"url": None, "raw": product, "fdc_id": None}}), FOUND_VIA)).rowcount
    return bool(created)


def next_term(conn, recrawl_days):
    return conn.execute(
        """SELECT term, next_start FROM catalog.terms
           WHERE done_at IS NULL OR done_at < now() - make_interval(days => %s)
           ORDER BY done_at NULLS FIRST, term LIMIT 1""", (recrawl_days,)).fetchone()


def crawl_step(conn, client, off_check, settings):
    """Fetch and sort one page of one term. Returns stats, or None when every term is done."""
    from kroger_sync import sync as kroger_sync
    row = next_term(conn, settings.recrawl_days)
    if not row:
        return None
    term, start = row
    page = client.search(term, start=start, limit=PAGE)
    _count(conn, "kroger")
    kroger_sync._add_calls(conn, 1)  # Kroger's daily limit is shared with the app's price lookups
    stats = {"term": term, "start": start, "products": 0, "new": 0, "staged": 0}
    for product in page:
        pid = product.get("productId")
        stats["products"] += 1
        if not pid or conn.execute("SELECT 1 FROM catalog.seen WHERE product_id = %s", (pid,)).fetchone():
            continue
        status, keys = classify(conn, product, off_check)
        if status == "staged" and not stage(conn, keys[0], product):
            status = "already_staged"  # a shopper scanned it first
        conn.execute(
            """INSERT INTO catalog.seen (product_id, code, keys, food, term, status, brand, name, category, raw)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (pid, keys[0] if keys else None, keys, is_food(product.get("categories")), term, status,
             product.get("brand"), product.get("description"), (product.get("categories") or [None])[0],
             Jsonb(product)))
        stats["new"] += 1
        stats["staged"] += status == "staged"
    finished = len(page) < PAGE or start + PAGE > MAX_START
    conn.execute(
        """UPDATE catalog.terms SET next_start = %s, done_at = CASE WHEN %s THEN now() ELSE done_at END,
             products = products + %s, staged = staged + %s WHERE term = %s""",
        (1 if finished else start + PAGE, finished, stats["new"], stats["staged"], term))
    conn.commit()
    stats["term_done"] = finished
    return stats


def research_step(conn, settings, research=None, save=None):
    """Research the oldest queued catalog product, within the daily cap. Returns its barcode or None."""
    if used(conn, "research") >= settings.research_per_day:
        return None
    row = conn.execute("SELECT barcode FROM staging.scanned_products WHERE status = 'queued' AND found_via = %s "
                       "ORDER BY first_scanned LIMIT 1", (FOUND_VIA,)).fetchone()
    if not row:
        return None
    barcode = row[0]
    conn.execute("UPDATE staging.scanned_products SET status = 'researching' WHERE barcode = %s", (barcode,))
    _count(conn, "research")
    conn.commit()
    found = (research or staging.research)(barcode)
    (save or staging.save_research)(conn, barcode, found)
    return barcode


def status(conn):
    terms = conn.execute("SELECT count(*), count(done_at) FROM catalog.terms").fetchone()
    seen = dict(conn.execute("SELECT status, count(*) FROM catalog.seen GROUP BY 1").fetchall())
    queue = dict(conn.execute("SELECT status, count(*) FROM staging.scanned_products WHERE found_via = %s "
                              "GROUP BY 1", (FOUND_VIA,)).fetchall())
    today = dict(conn.execute("SELECT what, n FROM catalog.usage WHERE day = CURRENT_DATE").fetchall())
    return {"terms": {"total": terms[0], "done": terms[1]}, "products_seen": seen, "staged_by_status": queue,
            "today": today}


def seconds_until_tomorrow(now=None):
    now = now or dt.datetime.now()
    return (dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(0, 5)) - now).total_seconds()


def run(dsn, make_client, settings=None, sleep=time.sleep, log=print, max_steps=None, off_check=None):
    """The long-running loop: one Kroger page, then maybe one research, then a pause.
    Sleeps until tomorrow when today's budgets are spent or every term is crawled."""
    import psycopg
    settings = settings or Settings()
    off_check = off_check or OffCheck()
    client = make_client()
    steps = 0
    while max_steps is None or steps < max_steps:
        steps += 1
        with psycopg.connect(dsn) as conn:
            crawled = None
            if used(conn, "kroger") < settings.kroger_calls_per_day:
                try:
                    crawled = crawl_step(conn, client, off_check, settings)
                except Exception as e:  # a bad page shouldn't stop the crawl
                    conn.rollback()
                    log(f"crawl error: {type(e).__name__}: {e}")
                    crawled = {"error": True}
                if crawled and crawled.get("term_done"):
                    log(f"'{crawled['term']}' done")
            researched = None
            try:
                researched = research_step(conn, settings)
            except Exception as e:
                conn.rollback()
                log(f"research error: {type(e).__name__}: {e}")
            if crawled is None and researched is None:
                s = status(conn)
                log(f"nothing to do now (terms {s['terms']['done']}/{s['terms']['total']}, "
                    f"today {s['today']}); sleeping until tomorrow")
                sleep(seconds_until_tomorrow())
                continue
        sleep(settings.seconds_per_call)
