"""Whole Foods brand products service (read-only).

Run: uvicorn wholefoods.api:app --host 0.0.0.0 --port 8002
Env: DATABASE_URL (the product store database).
"""

import os

import psycopg
from fastapi import FastAPI

from . import store


def _load_dotenv(path=".env"):
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
app = FastAPI(title="Whole Foods brand products")


def _conn():
    return psycopg.connect(os.environ["DATABASE_URL"])


@app.on_event("startup")
def _startup():
    with _conn() as conn:
        store.refresh(conn)


@app.get("/health")
def health():
    with _conn() as conn:
        n = conn.execute("SELECT count(*) FROM wholefoods.products").fetchone()[0]
    return {"ok": True, "products": n}


@app.post("/refresh")
def refresh():
    """Recompute the set after a product reload."""
    with _conn() as conn:
        return {"products": store.refresh(conn)}


@app.get("/products")
def products(q: str | None = None, category: str | None = None, brand: str | None = None,
             limit: int = 50, offset: int = 0):
    with _conn() as conn:
        return store.list_products(conn, q, category, brand, limit, offset)


@app.get("/facets")
def facets():
    with _conn() as conn:
        return store.facets(conn)
