"""Kroger service: store choice, barcode lookups for the UI, and on-demand bulk runs.

Run: uvicorn kroger_sync.api:app --host 0.0.0.0 --port 8001
Env: DATABASE_URL (same database as the product API), KROGER_CLIENT_ID,
KROGER_CLIENT_SECRET, optional KROGER_MAX_CALLS_PER_DAY (default 9000) and
KROGER_MAX_AGE_DAYS (default 7: how long a cached answer counts as fresh).

Independent of the product API: it only reads public.products / product_tags and
writes the kroger schema.
"""

import os
import threading

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import sync
from .client import KrogerClient, KrogerError, configured


def _load_dotenv(path=".env"):
    """KEY=VALUE lines from ./.env into os.environ (existing variables win)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.split(" #", 1)[0].strip().strip("'\""))
    except FileNotFoundError:
        pass


_load_dotenv()
app = FastAPI(title="Kroger sync")
_client = None


def _dsn():
    return os.environ["DATABASE_URL"]


def _conn():
    return psycopg.connect(_dsn())


def client():
    """One shared client (keeps its access token); None until keys are set."""
    global _client
    if _client is None and configured():
        _client = KrogerClient()
    return _client


@app.on_event("startup")
def _startup():
    with _conn() as conn:
        sync.init_schema(conn)
        sync.mark_interrupted(conn)


class Store(BaseModel):
    location_id: str
    name: str | None = None


class Run(BaseModel):
    mode: str = "load"  # load | refresh
    categories: list[str] | None = None  # product categories (e.g. cat:snacks); None = all


class Codes(BaseModel):
    codes: list[str]
    lookup: bool = True  # look up unknown/stale barcodes live; False = cache only
    location_id: str | None = None  # a store; default: the store chosen in settings


@app.get("/health")
def health():
    with _conn() as conn:
        return {"ok": True, **sync.status(conn)}


@app.get("/locations")
def locations(zip: str):
    if not configured():
        raise HTTPException(400, "set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET first")
    try:
        return {"locations": client().locations(zip)}
    except KrogerError as e:
        raise HTTPException(400, str(e))


@app.put("/store")
def choose_store(store: Store):
    with _conn() as conn:
        sync.set_setting(conn, "location_id", store.location_id)
        sync.set_setting(conn, "location_name", store.name or store.location_id)
        return sync.status(conn)


@app.post("/items")
def items(body: Codes):
    """Kroger price, aisle, image and stock for the given product barcodes."""
    with _conn() as conn:
        return sync.items_for(conn, body.codes, client() if body.lookup else None, body.location_id)


@app.post("/runs", status_code=202)
def start(run: Run):
    if run.mode not in sync.MODES:
        raise HTTPException(400, f"mode must be one of {sync.MODES}")
    if not configured():
        raise HTTPException(400, "set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET first")
    with _conn() as conn:
        if not sync.get_setting(conn, "location_id"):
            raise HTTPException(400, "choose a Kroger store first")
        run_id = sync.start_run(conn, run.mode, {"categories": run.categories})
    if run_id is None:
        raise HTTPException(409, "a Kroger run is already in progress")
    threading.Thread(target=sync.run_sync, args=(_dsn(), run_id, run.mode, run.categories, client),
                     daemon=True).start()
    return {"id": run_id, "mode": run.mode, "status": "running"}


@app.get("/runs/latest")
def latest():
    with _conn() as conn:
        return sync.latest_run(conn) or {}


@app.get("/coverage")
def coverage():
    with _conn() as conn:
        return sync.coverage(conn)
