"""Where the time goes, per app request.

Every /api request is timed by a middleware in api.py; code inside marks its steps with
off_products.timing.step() (parse, embed, search, kroger, ...). Each request gets a
Server-Timing response header (readable in browser dev tools) and a row in mobile.timings:
route, status, total and per-step milliseconds, and a few flags (cache_hit, store). No question
text, install ID or location is stored. Rows are kept KEEP_DAYS days. GET /admin/timings reports
percentiles per route and step, and the slowest recent requests.
"""

import re
import threading

from psycopg.types.json import Jsonb

KEEP_DAYS = 30
SCHEMA = [
    "CREATE SCHEMA IF NOT EXISTS mobile",
    """CREATE TABLE IF NOT EXISTS mobile.timings (
        at TIMESTAMPTZ NOT NULL DEFAULT now(), method TEXT NOT NULL, route TEXT NOT NULL,
        status INTEGER NOT NULL, total_ms REAL NOT NULL, steps JSONB NOT NULL, meta JSONB NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS timings_at ON mobile.timings (at)",
]
# Paths with an ID in them are reported as one route.
_ROUTES = [(re.compile(r"^/api/products/[^/]+$"), "/api/products/{barcode}"),
           (re.compile(r"^/api/scan/[^/]+$"), "/api/scan/{barcode}")]
_saves = 0
_lock = threading.Lock()


def init_schema(conn):
    for sql in SCHEMA:
        conn.execute(sql)
    conn.execute("DELETE FROM mobile.timings WHERE at < now() - make_interval(days => %s)", (KEEP_DAYS,))
    conn.commit()


def route(path):
    return next((name for rx, name in _ROUTES if rx.match(path)), path)


def header(timings, total_ms):
    """Server-Timing value, e.g. 'parse;dur=712.3, search;dur=95.0, total;dur=1204.6'."""
    parts = [f"{name};dur={ms}" for name, ms in timings["steps"].items()]
    return ", ".join(parts + [f"total;dur={round(total_ms, 1)}"])


def save(connect, method, path, status, total_ms, timings):
    """Store one request's timings; never raises (timing must not break the app)."""
    global _saves
    try:
        with connect() as conn:
            conn.execute("INSERT INTO mobile.timings (method, route, status, total_ms, steps, meta) "
                         "VALUES (%s, %s, %s, %s, %s, %s)",
                         (method, route(path), status, round(total_ms, 1), Jsonb(timings["steps"]),
                          Jsonb(timings["meta"])))
            with _lock:
                _saves += 1
                prune = _saves % 1000 == 0
            if prune:
                conn.execute("DELETE FROM mobile.timings WHERE at < now() - make_interval(days => %s)", (KEEP_DAYS,))
            conn.commit()
    except Exception as e:
        print(f"timings not saved: {type(e).__name__}: {e}", flush=True)


def summary(conn, hours=24, slowest=20):
    """Per route: count and total-time percentiles; per route and step: percentiles; the slowest
    requests with their breakdown. Milliseconds."""
    since = "at > now() - make_interval(hours => %s)"
    routes = [
        {"route": r, "method": m, "n": n, "p50": p50, "p90": p90, "p99": p99, "max": mx,
         "errors": errors, "cache_hits": hits}
        for m, r, n, p50, p90, p99, mx, errors, hits in conn.execute(
            f"""SELECT method, route, count(*),
                       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms)::numeric, 1)::float,
                       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY total_ms)::numeric, 1)::float,
                       round(percentile_cont(0.99) WITHIN GROUP (ORDER BY total_ms)::numeric, 1)::float,
                       max(total_ms), count(*) FILTER (WHERE status >= 500),
                       count(*) FILTER (WHERE meta->>'cache_hit' = 'true')
                FROM mobile.timings WHERE {since} GROUP BY 1, 2 ORDER BY count(*) DESC""", (hours,))]
    steps = [
        {"route": r, "step": s, "n": n, "p50": p50, "p90": p90, "max": mx}
        for r, s, n, p50, p90, mx in conn.execute(
            f"""SELECT route, key, count(*),
                       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY value::float)::numeric, 1)::float,
                       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY value::float)::numeric, 1)::float,
                       max(value::float)
                FROM mobile.timings, jsonb_each_text(steps) WHERE {since}
                GROUP BY 1, 2 ORDER BY 1, 4 DESC""", (hours,))]
    slow = [
        {"at": at.isoformat(), "method": m, "route": r, "status": st, "total_ms": t, "steps": steps_, "meta": meta}
        for at, m, r, st, t, steps_, meta in conn.execute(
            f"""SELECT at, method, route, status, total_ms, steps, meta FROM mobile.timings
                WHERE {since} ORDER BY total_ms DESC LIMIT %s""", (hours, slowest))]
    return {"hours": hours, "routes": routes, "steps": steps, "slowest": slow}
