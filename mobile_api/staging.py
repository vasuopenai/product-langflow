"""Scanned barcodes that aren't in the product store: research, review, approve.

    scan ──▶ staging.scanned_products (status: researching ─▶ pending | not_found)
                 │ review (Streamlit "Review" tab)
                 ├─ approve ─▶ products (origin "scan", searchable) ─ status approved
                 └─ reject  ─▶ status rejected

Nothing is added to the product store without approval. Every scan is logged in
staging.scan_events (when, where, which store, found or not).
"""

import datetime as dt
import threading
import traceback

import psycopg
from psycopg.types.json import Jsonb

from . import config, sources
from .barcodes import key

SCHEMA = [
    "CREATE SCHEMA IF NOT EXISTS staging",
    """CREATE TABLE IF NOT EXISTS staging.scanned_products (
        barcode TEXT PRIMARY KEY, status TEXT NOT NULL, draft JSONB, field_sources JSONB,
        sources JSONB, confidence TEXT, error TEXT, scan_count INTEGER NOT NULL DEFAULT 0,
        first_scanned TIMESTAMPTZ DEFAULT now(), last_scanned TIMESTAMPTZ DEFAULT now(),
        researched_at TIMESTAMPTZ, reviewed_at TIMESTAMPTZ, review_note TEXT, product_code TEXT)""",
    """CREATE TABLE IF NOT EXISTS staging.scan_events (
        id BIGSERIAL PRIMARY KEY, barcode TEXT NOT NULL, scanned_at TIMESTAMPTZ DEFAULT now(),
        client_id TEXT, lat DOUBLE PRECISION, lng DOUBLE PRECISION, location_id TEXT,
        found BOOLEAN NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS scan_events_barcode ON staging.scan_events (barcode)",
]
# Order sources are trusted in, per field: the label data USDA holds first.
SOURCE_ORDER = ["usda", "open_food_facts", "web", "kroger"]
REVIEW_FIELDS = ["name", "brand", "brand_owner", "ingredients", "category", "serving_size_g", "serving_unit",
                 "serving_text", "package_size", "image_url", "labels"]
_running = set()
_lock = threading.Lock()


def init_schema(conn):
    for sql in SCHEMA:
        conn.execute(sql)
    conn.commit()


def log_event(conn, barcode, found, client_id=None, lat=None, lng=None, location_id=None):
    conn.execute("INSERT INTO staging.scan_events (barcode, client_id, lat, lng, location_id, found) "
                 "VALUES (%s, %s, %s, %s, %s, %s)", (key(barcode), client_id, lat, lng, location_id, found))
    conn.commit()


def get(conn, barcode):
    row = conn.execute(
        """SELECT barcode, status, draft, field_sources, sources, confidence, error, scan_count,
                  first_scanned, last_scanned, researched_at, reviewed_at, review_note, product_code
           FROM staging.scanned_products WHERE barcode = %s""", (key(barcode),)).fetchone()
    if not row:
        return None
    names = ["barcode", "status", "draft", "field_sources", "sources", "confidence", "error", "scan_count",
             "first_scanned", "last_scanned", "researched_at", "reviewed_at", "review_note", "product_code"]
    out = dict(zip(names, row))
    for k in ("first_scanned", "last_scanned", "researched_at", "reviewed_at"):
        out[k] = out[k] and out[k].isoformat()
    return out


def note_scan(conn, barcode):
    """Count a scan of an unknown barcode. Returns True if research should start."""
    k = key(barcode)
    row = conn.execute(
        """INSERT INTO staging.scanned_products (barcode, status, scan_count) VALUES (%s, 'researching', 1)
           ON CONFLICT (barcode) DO UPDATE SET scan_count = staging.scanned_products.scan_count + 1,
             last_scanned = now()
           RETURNING (xmax = 0)""", (k,)).fetchone()
    conn.commit()
    return bool(row[0])  # True when the row was just created


def merge(found):
    """Combine source drafts field by field in SOURCE_ORDER. Returns (draft, field_sources)."""
    draft, origin = {}, {}
    for name in SOURCE_ORDER:
        part = (found.get(name) or {}).get("draft") or {}
        for field, value in part.items():
            if field == "nutrients_100g":
                for n, v in value.items():
                    if n not in draft.setdefault("nutrients_100g", {}):
                        draft["nutrients_100g"][n] = v
                        origin[f"nutrients_100g.{n}"] = name
            elif field not in draft:
                draft[field] = value
                origin[field] = name
    return draft, origin


def confidence(found):
    if found.get("usda"):
        return "high"
    off = (found.get("open_food_facts") or {}).get("draft") or {}
    if off.get("ingredients") and off.get("nutrients_100g"):
        return "medium"
    if found.get("web"):
        return found["web"].get("confidence") or "low"
    return "low" if found else None


def research(barcode, location_id=None, fetch=sources.http_json, web_client=None, kroger_factory=None,
             use_web=True):
    """Ask every source. Returns {source name: result} for the ones that found it."""
    found = {}
    for name, call in (("usda", lambda: sources.usda_live(barcode, fetch)),
                       ("open_food_facts", lambda: sources.open_food_facts(barcode, fetch)),
                       ("kroger", lambda: sources.kroger(barcode, location_id, kroger_factory))):
        try:
            result = call()
        except Exception as e:  # one failing source shouldn't stop the others
            result = None
            found.setdefault("_errors", {})[name] = f"{type(e).__name__}: {e}"
        if result:
            found[name] = result
    # Web search costs money; skip it when USDA already has the full label.
    if use_web and not found.get("usda"):
        known, _ = merge(found)
        hint = " ".join(v for v in (known.get("brand"), known.get("name"), known.get("package_size")) if v) or None
        try:
            result = sources.web(barcode, web_client, hint=hint)
            if result:
                found["web"] = result
        except Exception as e:
            found.setdefault("_errors", {})["web"] = f"{type(e).__name__}: {e}"
    return found


def save_research(conn, barcode, found):
    errors = found.pop("_errors", None)
    draft, origin = merge(found)
    status = "pending" if draft.get("name") else "not_found"
    conn.execute(
        """UPDATE staging.scanned_products SET status = %s, draft = %s, field_sources = %s, sources = %s,
             confidence = %s, error = %s, researched_at = now() WHERE barcode = %s""",
        (status, Jsonb(draft), Jsonb(origin),
         Jsonb({k: {"url": v.get("url"), "raw": v.get("raw"), "fdc_id": v.get("fdc_id")} for k, v in found.items()}),
         confidence(found), str(errors) if errors else None, key(barcode)))
    conn.commit()
    return status


def research_async(dsn, barcode, location_id=None, **kw):
    """Research in a background thread (web search takes several seconds); the app polls."""
    k = key(barcode)
    with _lock:
        if k in _running:
            return False
        _running.add(k)

    def job():
        try:
            found = research(k, location_id, **kw)
            with psycopg.connect(dsn) as conn:
                save_research(conn, k, found)
        except Exception as e:
            traceback.print_exc()
            with psycopg.connect(dsn) as conn:
                conn.execute("UPDATE staging.scanned_products SET status = 'error', error = %s WHERE barcode = %s",
                             (f"{type(e).__name__}: {e}", k))
                conn.commit()
        finally:
            with _lock:
                _running.discard(k)

    threading.Thread(target=job, daemon=True).start()
    return True


def restart_research(conn, dsn, barcode, location_id=None, **kw):
    conn.execute("UPDATE staging.scanned_products SET status = 'researching', error = NULL WHERE barcode = %s",
                 (key(barcode),))
    conn.commit()
    return research_async(dsn, barcode, location_id, **kw)


def list_queue(conn, status="pending", limit=100):
    rows = conn.execute(
        """SELECT barcode FROM staging.scanned_products WHERE (%s::text IS NULL OR status = %s)
           ORDER BY scan_count DESC, last_scanned DESC LIMIT %s""", (status, status, limit)).fetchall()
    return [get(conn, b) for (b,) in rows]


def counts(conn):
    return dict(conn.execute("SELECT status, count(*) FROM staging.scanned_products GROUP BY 1").fetchall())


# --- approve / reject ------------------------------------------------------------------

def to_usda_row(barcode, draft, staged_sources=None):
    """A reviewed draft as the USDA-shaped row the loader turns into a product record."""
    k = key(barcode)
    usda = (staged_sources or {}).get("usda") or {}
    urls = [s.get("url") for s in (staged_sources or {}).values() if s and s.get("url")]
    unit = (draft.get("serving_unit") or "g").lower()
    nutrients = {sources.USDA_IDS[n]: v for n, v in (draft.get("nutrients_100g") or {}).items()
                 if n in sources.USDA_IDS and v is not None}
    return {
        "fdc_id": usda.get("fdc_id") or f"scan-{k}",
        "gtin_upc": k.zfill(12),
        "brand_owner": draft.get("brand_owner") or draft.get("brand"),
        "brand_name": draft.get("brand"),
        "description": draft.get("name"),
        "ingredients": draft.get("ingredients"),
        "serving_size": draft.get("serving_size_g"),
        "serving_size_unit": "ml" if unit == "ml" else "g",
        "household_serving_fulltext": draft.get("serving_text"),
        "branded_food_category": draft.get("category"),
        "package_weight": draft.get("package_size"),
        "market_country": "United States",
        "available_date": dt.date.today().isoformat(),
        "modified_date": dt.date.today().isoformat(),
        "nutrients": nutrients,
        "off": {"labels": draft.get("labels") or [], "image_url": draft.get("image_url"),
                "nova_group": None, "unique_scans_n": 0, "allergens": []},
        "origin": "scan",
        "url": urls[0] if urls else None,
        "sources": {name: s.get("url") for name, s in (staged_sources or {}).items() if s},
    }


class ApproveError(ValueError):
    pass


def approve(conn, barcode, edits=None, embedder=None, note=None):
    """Add the reviewed product to the store (searchable) and mark it approved."""
    from off_products import usda
    from off_products.pg import init_schema as init_products, make_embedder, upsert_batch

    staged = get(conn, barcode)
    if not staged:
        raise ApproveError("no staged product for this barcode")
    draft = {**(staged["draft"] or {}), **(edits or {})}
    if edits and "nutrients_100g" in edits:
        draft["nutrients_100g"] = {**((staged["draft"] or {}).get("nutrients_100g") or {}), **edits["nutrients_100g"]}
    if not draft.get("name"):
        raise ApproveError("a product name is required")
    record = usda.normalize(to_usda_row(barcode, draft, staged["sources"]))
    if record["nutrition"]["implausible"] and record["nutrition"]["per_100g"]["energy_kcal"] is None:
        raise ApproveError("nutrition values look impossible: " + "; ".join(record["nutrition"]["implausible"]))
    embedder = embedder or make_embedder(config.env("OFF_EMBEDDER", "openai"))
    init_products(conn, embedder.dim)
    upsert_batch(conn, [record], embedder)
    conn.execute(
        """UPDATE staging.scanned_products SET status = 'approved', draft = %s, reviewed_at = now(),
             review_note = %s, product_code = %s WHERE barcode = %s""",
        (Jsonb(draft), note, record["code"], key(barcode)))
    conn.commit()
    return record


def reject(conn, barcode, note=None):
    conn.execute("UPDATE staging.scanned_products SET status = 'rejected', reviewed_at = now(), review_note = %s "
                 "WHERE barcode = %s", (note, key(barcode)))
    conn.commit()
