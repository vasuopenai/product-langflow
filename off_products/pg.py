"""Postgres + pgvector store: one database for filters, ingredient amounts and embeddings.

Same tables as the SQLite reference store (products / product_tags /
product_ingredients) plus an ``embedding vector(N)`` column, so the WHERE
clauses from ``query.build_where`` run unchanged.
"""

import hashlib
import json
import math
import os
import re

import psycopg
from psycopg.types.json import Jsonb

from .normalize import ingredient_amounts, to_flat
from .query import QuerySpec, build_where, order_by
from .store import TAG_KINDS, _scalar_columns, iter_raw, keep
from .normalize import normalize

_INT_COLS = {"ingredients_n", "ingredients_top_level_n", "nova_group", "unique_scans_n"}
_TEXT_COLS = {"name", "brand", "main_category", "first_ingredient", "nutriscore_grade", "image_url"}


def _col_type(col):
    if col in _INT_COLS:
        return "INTEGER"
    if col in _TEXT_COLS:
        return "TEXT"
    if col == "vegan":
        return "BOOLEAN"
    return "DOUBLE PRECISION"


def _coerce(col, value):
    if value is None:
        return None
    if col in _INT_COLS:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    if col in _TEXT_COLS:
        return str(value)
    if col == "vegan":
        return bool(value)
    return float(value)


# --- embeddings ---------------------------------------------------------------

class OpenAIEmbedder:
    def __init__(self, model=None, dim=1536):
        from openai import OpenAI

        self.client = OpenAI()
        self.model = model or os.getenv("OFF_EMBED_MODEL", "text-embedding-3-small")
        self.dim = dim

    def embed(self, texts):
        out = []
        for i in range(0, len(texts), 256):
            resp = self.client.embeddings.create(model=self.model, input=texts[i:i + 256])
            out.extend(d.embedding for d in resp.data)
        return out


class HashEmbedder:
    """Offline stand-in for smoke tests: hashed bag of words, no API key needed.
    Gives crude lexical similarity only; use OpenAIEmbedder for real search."""

    model = "fake-hash"

    def __init__(self, dim=1536):
        self.dim = dim

    def embed(self, texts):
        vecs = []
        for text in texts:
            v = [0.0] * self.dim
            for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
                v[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs.append([x / norm for x in v])
        return vecs


def make_embedder(kind):
    return HashEmbedder() if kind == "fake" else OpenAIEmbedder()


def _vec(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


# --- schema and load ----------------------------------------------------------

def init_schema(conn, dim=1536):
    cols = _scalar_columns()
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS products (code TEXT PRIMARY KEY, obsolete SMALLINT NOT NULL DEFAULT 0, "
        + ", ".join(f'"{c}" {_col_type(c)}' for c in cols)
        + f", search_text TEXT, record JSONB, embedding vector({dim}))"
    )
    # Which model made each vector, so reloads can reuse unchanged embeddings.
    # Checked first: ALTER takes an exclusive lock even when the column exists,
    # which would wait on any open read (e.g. the API) during a reload.
    if not conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'products' AND column_name = 'embed_model'"
    ).fetchone():
        conn.execute("ALTER TABLE products ADD COLUMN embed_model TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS product_tags (code TEXT, kind TEXT, tag TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS tags_kind_tag ON product_tags(kind, tag, code)")
    conn.execute("CREATE INDEX IF NOT EXISTS tags_code ON product_tags(code)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS product_ingredients (code TEXT, ingredient TEXT, rank INTEGER, "
        "declared SMALLINT, percent DOUBLE PRECISION, percent_min DOUBLE PRECISION, "
        "percent_max DOUBLE PRECISION, grams_per_100g DOUBLE PRECISION, grams_per_serving DOUBLE PRECISION)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ing_ingredient ON product_ingredients(ingredient, code)")
    conn.execute("CREATE INDEX IF NOT EXISTS ing_code ON product_ingredients(code)")
    for c in ("protein_g_serving", "protein_g_100g", "sugars_g_serving", "ingredients_n", "unique_scans_n"):
        conn.execute(f'CREATE INDEX IF NOT EXISTS products_{c} ON products("{c}")')
    conn.commit()


def create_vector_index(conn):
    """Approximate index for large tables. Filtered queries rely on
    hnsw.iterative_scan (pgvector >= 0.8) so filters don't starve the results."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS products_embedding ON products "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    conn.commit()


def _embed_changed(conn, records, embedder):
    """Embed only records whose search_text or embedding model changed since
    the last load; reuse stored vectors for the rest. Returns (vectors, n_embedded)."""
    rows = conn.execute(
        "SELECT code, search_text, embedding::text FROM products "
        "WHERE code = ANY(%s) AND embed_model = %s AND embedding IS NOT NULL",
        ([r["code"] for r in records], embedder.model),
    ).fetchall()
    stored = {code: (text, vec) for code, text, vec in rows}
    todo = [r for r in records if stored.get(r["code"], (None,))[0] != r["search_text"]]
    fresh = dict(zip((r["code"] for r in todo), embedder.embed([r["search_text"] for r in todo]) if todo else []))
    return [_vec(fresh[r["code"]]) if r["code"] in fresh else stored[r["code"]][1] for r in records], len(todo)


def upsert_batch(conn, records, embedder):
    cols = _scalar_columns()
    vectors, n_embedded = _embed_changed(conn, records, embedder)
    codes = [r["code"] for r in records]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM product_tags WHERE code = ANY(%s)", (codes,))
        cur.execute("DELETE FROM product_ingredients WHERE code = ANY(%s)", (codes,))
        all_cols = ["code", "obsolete", *cols, "search_text", "record", "embed_model", "embedding"]
        cur.executemany(
            "INSERT INTO products (" + ", ".join(f'"{c}"' for c in all_cols) + ") VALUES ("
            + ", ".join(["%s"] * (len(all_cols) - 1)) + ", %s::vector) ON CONFLICT (code) DO UPDATE SET "
            + ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in all_cols[1:]),
            [
                [r["code"], int(r["quality"]["obsolete"])]
                + [_coerce(c, flat.get(c)) for c in cols]
                + [r["search_text"], Jsonb(r), embedder.model, v]
                for r, v, flat in ((r, v, to_flat(r)) for r, v in zip(records, vectors))
            ],
        )
        cur.executemany(
            "INSERT INTO product_tags (code, kind, tag) VALUES (%s, %s, %s)",
            [
                (r["code"], kind, tag)
                for r in records
                for kind, key in TAG_KINDS.items()
                for tag in set(to_flat(r)[key])
            ],
        )
        cur.executemany(
            "INSERT INTO product_ingredients VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (r["code"], a["ingredient"], a["rank"], int(a["declared"]), a["percent"],
                 a["percent_min"], a["percent_max"], a["grams_per_100g"], a["grams_per_serving"])
                for r in records
                for a in ingredient_amounts(r)
            ],
        )
    conn.commit()
    return n_embedded


def delete_products(conn, codes):
    """Remove products loaded earlier that a reload now rejects."""
    for table in ("product_tags", "product_ingredients", "products"):
        conn.execute(f"DELETE FROM {table} WHERE code = ANY(%s)", (codes,))
    conn.commit()


def load(src, dsn, embedder, countries=None, min_completeness=0.0, batch_size=500,
         limit=None, require_ingredients=True, require_nutrition=True, progress=print):
    """Stream an OFF export into Postgres. Returns (read, loaded).

    Re-running is cheap: unchanged products reuse their stored embeddings."""
    with psycopg.connect(dsn) as conn:
        init_schema(conn, embedder.dim)
        batch, rejected, seen, kept, embedded = [], [], 0, 0, 0

        def flush():
            nonlocal kept, embedded
            if batch:
                embedded += upsert_batch(conn, batch, embedder)
                kept += len(batch)
                batch.clear()
            if rejected:
                delete_products(conn, rejected)
                rejected.clear()

        for raw in iter_raw(src):
            seen += 1
            r = normalize(raw)
            if not keep(r, countries, min_completeness):
                continue
            if require_ingredients and not r["ingredients"]["items"]:
                continue
            if require_nutrition and r["nutrition"]["per_100g"]["energy_kcal"] is None:
                if r["nutrition"]["implausible"]:
                    rejected.append(r["code"])
                continue
            batch.append(r)
            if len(batch) >= batch_size:
                flush()
                progress(f"read {seen}, loaded {kept}, newly embedded {embedded}")
            if limit and kept + len(batch) >= limit:
                break
        flush()
        return seen, kept


# --- search -------------------------------------------------------------------

def search(conn, spec: QuerySpec, query_vector=None):
    """Hard filters, then explicit sort, then semantic similarity, then popularity."""
    where, params = build_where(spec, ph="%s")
    order = order_by(spec)
    select_sim = "NULL::float AS similarity"
    sim_params = []
    if query_vector is not None:
        select_sim = "1 - (p.embedding <=> %s::vector) AS similarity"
        sim_params = [_vec(query_vector)]
        order.append("p.embedding <=> %s::vector")
        params_order = [_vec(query_vector)]
    else:
        params_order = []
    order.append("p.unique_scans_n DESC NULLS LAST")
    sql = (
        f"SELECT p.code, p.record, {select_sim} FROM products p WHERE "
        + " AND ".join(where)
        + " ORDER BY " + ", ".join(order) + " LIMIT %s"
    )
    with conn.transaction():
        try:
            with conn.transaction():
                conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
        except psycopg.Error:
            pass  # pgvector < 0.8: no iterative scan; exact scan still correct
        rows = conn.execute(sql, sim_params + params + params_order + [spec.limit]).fetchall()
    return [
        {**(rec if isinstance(rec, dict) else json.loads(rec)), "similarity": sim}
        for _, rec, sim in rows
    ]


def known_tags(conn, kind, tags):
    """Subset of ``tags`` that exist in the data (to drop ids the LLM made up)."""
    if not tags:
        return []
    rows = conn.execute(
        "SELECT DISTINCT tag FROM product_tags WHERE kind = %s AND tag = ANY(%s)", (kind, list(tags))
    ).fetchall()
    found = {r[0] for r in rows}
    return [t for t in tags if t in found]
