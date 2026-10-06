"""HTTP API: POST /ask {"question": "..."} and POST /search {QuerySpec}.

Run: uvicorn off_products.api:app --host 0.0.0.0 --port 8000
Env: DATABASE_URL, OPENAI_API_KEY, optional OFF_EMBEDDER=fake, OFF_CHAT_MODEL.
"""

import os

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ._env import load_dotenv
from .ask import ask, _facts
from .pg import make_embedder, search
from .query import QuerySpec

load_dotenv()
app = FastAPI(title="Open Food Facts product search")
_embedder = None


def embedder():
    # Created on first use so /health works before OPENAI_API_KEY is set.
    global _embedder
    if _embedder is None:
        _embedder = make_embedder(os.getenv("OFF_EMBEDDER", "openai"))
    return _embedder


def _conn():
    return psycopg.connect(os.environ["DATABASE_URL"])


class Question(BaseModel):
    question: str


@app.get("/health")
def health():
    with _conn() as conn:
        exists = conn.execute("SELECT to_regclass('products') IS NOT NULL").fetchone()[0]
        n = conn.execute("SELECT count(*) FROM products").fetchone()[0] if exists else 0
    return {"ok": True, "products": n}


@app.post("/ask")
def ask_endpoint(body: Question):
    with _conn() as conn:
        return ask(conn, body.question, embedder())


@app.post("/search")
def search_endpoint(spec: dict):
    try:
        q = QuerySpec.from_dict(spec)
        with _conn() as conn:
            vector = embedder().embed([q.semantic_query])[0] if q.semantic_query else None
            return {"products": [_facts(r) for r in search(conn, q, vector)]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
