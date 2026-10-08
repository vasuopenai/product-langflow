"""Products from the web for a question or product name (OpenAI web search).

Results are cached per query for a day in mobile.web_cache, since each search costs
money.
"""

import re

from psycopg.types.json import Jsonb

from . import config
from .sources import parse_json, responses_web_search

SCHEMA = [
    "CREATE SCHEMA IF NOT EXISTS mobile",
    """CREATE TABLE IF NOT EXISTS mobile.web_cache (
        query TEXT PRIMARY KEY, result JSONB NOT NULL, created_at TIMESTAMPTZ DEFAULT now())""",
]
CACHE_HOURS = 24

PROMPT = """A shopper in the United States asked: "{query}"
Search the web for packaged food products sold in the US that match. Prefer products
whose labels confirm the request (nutrition, ingredients). Up to {n} products.

Reply with JSON only (no prose, no code fences):
[{{"title": str, "brand": str, "why": str (one short line: how it fits the request),
   "price": str or null (as shown, e.g. "$3.99"), "retailer": str or null,
   "url": str (the page you used), "image_url": str or null}}]"""


def init_schema(conn):
    for sql in SCHEMA:
        conn.execute(sql)
    conn.commit()


def _norm(query):
    return re.sub(r"\s+", " ", (query or "").strip().lower())[:300]


def search(conn, query, client=None, n=6):
    """{"query", "products": [...], "citations": [...], "cached": bool}."""
    q = _norm(query)
    if not q:
        return {"query": query, "products": [], "citations": [], "cached": False}
    row = conn.execute("SELECT result FROM mobile.web_cache WHERE query = %s "
                       "AND created_at > now() - make_interval(hours => %s)", (q, CACHE_HOURS)).fetchone()
    if row:
        return {**row[0], "cached": True}
    if client is None:
        from openai import OpenAI
        client = OpenAI()
    text, citations = responses_web_search(client, config.WEB_SEARCH_MODEL, PROMPT.format(query=query, n=n))
    data = parse_json(text)
    products = [p for p in (data if isinstance(data, list) else []) if isinstance(p, dict) and p.get("title")][:n]
    result = {"query": query, "products": products, "citations": citations}
    conn.execute("INSERT INTO mobile.web_cache VALUES (%s, %s, now()) "
                 "ON CONFLICT (query) DO UPDATE SET result = EXCLUDED.result, created_at = now()",
                 (q, Jsonb(result)))
    conn.commit()
    return {**result, "cached": False}
