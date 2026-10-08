"""Mobile gateway API.

Run: uvicorn mobile_api.api:app --host 0.0.0.0 --port 8003
Env: DATABASE_URL, OPENAI_API_KEY, KROGER_CLIENT_ID/SECRET (optional), USDA_API_KEY
(optional, default DEMO_KEY), MOBILE_APP_KEY / MOBILE_ADMIN_KEY (optional locally,
set them when hosted), MOBILE_HOURLY_LIMIT, WEB_SEARCH_MODEL, AT_STORE_METERS.

App endpoints (/api, header X-App-Key when MOBILE_APP_KEY is set; X-Client-Id
identifies an install for rate limits and scan logs):
  GET  /api/health
  GET  /api/nearby?lat=&lng=                 store the user is at / stores near them
  POST /api/ask {question, location_id?}     answer + product cards + Kroger price at the store
  POST /api/web {query}                      products from the web (cached a day)
  POST /api/scan {barcode, lat?, lng?, location_id?}   a scan: product, or research starts
  GET  /api/scan/{barcode}                   poll a scan that is being researched
  GET  /api/products/{barcode}?location_id=  product detail
Review endpoints (/admin, header X-Admin-Key when MOBILE_ADMIN_KEY is set) back the
Streamlit "Review" tab.
"""

import collections
import contextlib
import threading
import time

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import config, products, staging, stores, websearch
from .barcodes import key

@contextlib.asynccontextmanager
async def lifespan(app):
    from kroger_sync import sync as kroger_sync
    with _conn() as conn:
        staging.init_schema(conn)
        websearch.init_schema(conn)
        kroger_sync.init_schema(conn)
    yield


app = FastAPI(title="Food app gateway", lifespan=lifespan)
# The Expo web build runs on another origin (http://localhost:8081) while testing.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
_embedder = None
_kroger = None
_calls = collections.defaultdict(collections.deque)
_calls_lock = threading.Lock()


def _conn():
    return psycopg.connect(config.dsn())


def embedder():
    global _embedder
    if _embedder is None:
        from off_products.pg import make_embedder
        _embedder = make_embedder(config.env("OFF_EMBEDDER", "openai"))
    return _embedder


def kroger_client():
    """One shared Kroger client (keeps its token); None without keys."""
    global _kroger
    from kroger_sync.client import KrogerClient, configured
    if _kroger is None and configured():
        _kroger = KrogerClient()
    return _kroger


# --- auth and limits ------------------------------------------------------------------

def app_auth(request: Request, x_app_key: str | None = Header(None), x_client_id: str | None = Header(None)):
    if config.APP_KEY and x_app_key != config.APP_KEY:
        raise HTTPException(401, "missing or wrong X-App-Key")
    return x_client_id or (request.client.host if request.client else "unknown")


def admin_auth(x_admin_key: str | None = Header(None)):
    if config.ADMIN_KEY and x_admin_key != config.ADMIN_KEY:
        raise HTTPException(401, "missing or wrong X-Admin-Key")


def spend(client_id):
    """Count one paid call (LLM, web search) for this client; 429 over the hourly limit."""
    now = time.time()
    with _calls_lock:
        q = _calls[client_id]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= config.HOURLY_LIMIT:
            raise HTTPException(429, "Too many requests this hour - try again later.")
        q.append(now)


# --- helpers ---------------------------------------------------------------------------

def kroger_items(conn, codes, location_id):
    if not location_id or not codes:
        return {}
    from kroger_sync import sync as kroger_sync
    return kroger_sync.items_for(conn, codes, kroger_client(), location_id)["items"]


def with_kroger(conn, cards, location_id):
    items = kroger_items(conn, [c["code"] for c in cards], location_id)
    return [{**c, "kroger": items.get(c["code"])} for c in cards]


def staged_view(s):
    """What the app sees of a product under review (no raw source payloads)."""
    if not s:
        return None
    return {"barcode": s["barcode"], "status": s["status"], "draft": s["draft"] or {},
            "confidence": s["confidence"], "scan_count": s["scan_count"],
            "sources": {k: v.get("url") for k, v in (s["sources"] or {}).items() if v}}


# --- app endpoints ---------------------------------------------------------------------

class Question(BaseModel):
    question: str
    location_id: str | None = None


class WebQuery(BaseModel):
    query: str


class Scan(BaseModel):
    barcode: str
    lat: float | None = None
    lng: float | None = None
    location_id: str | None = None


@app.get("/api/health")
def health():
    with _conn() as conn:
        n = conn.execute("SELECT count(*) FROM products").fetchone()[0]
    from kroger_sync.client import configured
    return {"ok": True, "products": n, "kroger": configured()}


@app.get("/api/nearby")
def nearby(lat: float, lng: float, client_id: str = Depends(app_auth)):
    try:
        return stores.nearby(lat, lng)
    except Exception as e:
        raise HTTPException(502, f"store lookup failed: {e}")


@app.post("/api/ask")
def ask(body: Question, client_id: str = Depends(app_auth)):
    from off_products.ask import ask as run_ask
    if not body.question.strip():
        raise HTTPException(400, "question is empty")
    spend(client_id)
    with _conn() as conn:
        result = run_ask(conn, body.question.strip(), embedder())
        recs = products.records(conn, [p["code"] for p in result["products"]])
        cards = [products.card(recs[p["code"]]) for p in result["products"] if p["code"] in recs]
        return {"question": result["question"], "answer": result["answer"], "notes": result["notes"],
                "filters": {k: v for k, v in result["spec"].items() if v not in ([], None, "")},
                "products": with_kroger(conn, cards, body.location_id), "location_id": body.location_id}


@app.post("/api/web")
def web(body: WebQuery, client_id: str = Depends(app_auth)):
    with _conn() as conn:
        cached = conn.execute("SELECT 1 FROM mobile.web_cache WHERE query = %s", (websearch._norm(body.query),)).fetchone()
        if not cached:
            spend(client_id)
        try:
            return websearch.search(conn, body.query)
        except Exception as e:
            raise HTTPException(502, f"web search failed: {e}")


@app.post("/api/scan")
def scan(body: Scan, client_id: str = Depends(app_auth)):
    if not key(body.barcode):
        raise HTTPException(400, "not a barcode")
    with _conn() as conn:
        rec = products.find(conn, body.barcode)
        staging.log_event(conn, body.barcode, rec is not None, client_id, body.lat, body.lng, body.location_id)
        if rec:
            return {"status": "found", "product": with_kroger(conn, [products.card(rec)], body.location_id)[0]}
        is_new = staging.note_scan(conn, body.barcode)
        staged = staging.get(conn, body.barcode)
        if is_new or staged["status"] == "error":
            spend(client_id)
            if not is_new:
                conn.execute("UPDATE staging.scanned_products SET status = 'researching' WHERE barcode = %s",
                             (key(body.barcode),))
                conn.commit()
            staging.research_async(config.dsn(), body.barcode, body.location_id)
            staged = staging.get(conn, body.barcode)
        return {"status": staged["status"], "staged": staged_view(staged)}


@app.get("/api/scan/{barcode}")
def scan_status(barcode: str, location_id: str | None = None, client_id: str = Depends(app_auth)):
    with _conn() as conn:
        rec = products.find(conn, barcode)
        if rec:
            return {"status": "found", "product": with_kroger(conn, [products.card(rec)], location_id)[0]}
        staged = staging.get(conn, barcode)
        if not staged:
            raise HTTPException(404, "this barcode hasn't been scanned")
        return {"status": staged["status"], "staged": staged_view(staged)}


@app.get("/api/products/{barcode}")
def product(barcode: str, location_id: str | None = None, client_id: str = Depends(app_auth)):
    with _conn() as conn:
        rec = products.find(conn, barcode)
        if not rec:
            raise HTTPException(404, "product not found")
        return with_kroger(conn, [products.card(rec)], location_id)[0]


# --- review endpoints (Streamlit "Review" tab) -----------------------------------------------

class Approve(BaseModel):
    edits: dict | None = None
    note: str | None = None


class Reject(BaseModel):
    note: str | None = None


@app.get("/admin/review", dependencies=[Depends(admin_auth)])
def review_list(status: str | None = "pending", limit: int = 100):
    with _conn() as conn:
        return {"counts": staging.counts(conn), "items": staging.list_queue(conn, status or None, limit)}


@app.get("/admin/review/{barcode}", dependencies=[Depends(admin_auth)])
def review_item(barcode: str):
    with _conn() as conn:
        s = staging.get(conn, barcode)
        if not s:
            raise HTTPException(404, "not in the review queue")
        events = conn.execute(
            "SELECT scanned_at, client_id, location_id, lat, lng FROM staging.scan_events WHERE barcode = %s "
            "ORDER BY scanned_at DESC LIMIT 20", (key(barcode),)).fetchall()
        s["events"] = [{"at": e[0].isoformat(), "client": e[1], "location_id": e[2], "lat": e[3], "lng": e[4]}
                       for e in events]
        return s


@app.post("/admin/review/{barcode}/approve", dependencies=[Depends(admin_auth)])
def review_approve(barcode: str, body: Approve):
    with _conn() as conn:
        try:
            rec = staging.approve(conn, barcode, body.edits, embedder(), body.note)
        except staging.ApproveError as e:
            raise HTTPException(400, str(e))
        return {"status": "approved", "product": products.card(rec)}


@app.post("/admin/review/{barcode}/reject", dependencies=[Depends(admin_auth)])
def review_reject(barcode: str, body: Reject):
    with _conn() as conn:
        staging.reject(conn, barcode, body.note)
        return {"status": "rejected"}


@app.post("/admin/review/{barcode}/research", dependencies=[Depends(admin_auth)])
def review_research(barcode: str):
    with _conn() as conn:
        if not staging.get(conn, barcode):
            raise HTTPException(404, "not in the review queue")
        started = staging.restart_research(conn, config.dsn(), barcode)
        return {"status": "researching", "started": started}
