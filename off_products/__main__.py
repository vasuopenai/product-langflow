"""CLI.

Local SQLite (no services needed):
  python -m off_products build food.parquet products.db --country en:united-states
  python -m off_products query products.db '{"semantic_query": "protein bar", ...}'

Postgres + pgvector (DATABASE_URL, OPENAI_API_KEY):
  python -m off_products pg-load food.parquet --country en:united-states
  python -m off_products pg-search '{"semantic_query": "protein bar", ...}'
  python -m off_products ask "protein bar with at least 20 g protein and no seed oils"
"""

import argparse
import json
import os
import sqlite3

from .query import QuerySpec, to_sql
from .store import build


def _print_record(r, similarity=None):
    serv = r["nutrition"]["per_serving"]
    free_of = [g for g, v in r["derived"]["groups"].items() if v["status"] == "none"]
    sim = f"  sim={similarity:.3f}" if similarity is not None else ""
    print(
        f"{r['code']}  {r['name']} ({r['brand']})  protein/serving={serv['protein_g']}  "
        f"ingredients={r['ingredients']['count_total']}{sim}  free_of={free_of}"
    )


def main():
    ap = argparse.ArgumentParser(prog="off_products")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="normalize an OFF export into SQLite")
    b.add_argument("src", help=".jsonl, .jsonl.gz or .parquet")
    b.add_argument("db")
    b.add_argument("--country", action="append", help="e.g. en:united-states (repeatable)")
    b.add_argument("--min-completeness", type=float, default=0.0)
    b.add_argument("--jsonl-out", help="also write canonical records as JSONL")

    q = sub.add_parser("query", help="run a QuerySpec (JSON) against the SQLite store")
    q.add_argument("db")
    q.add_argument("spec")

    dsn = dict(default=os.getenv("DATABASE_URL"), help="Postgres URL (default: $DATABASE_URL)")
    emb = dict(choices=["openai", "fake"], default=os.getenv("OFF_EMBEDDER", "openai"),
               help="'fake' = offline hashed embeddings for smoke tests")

    pl = sub.add_parser("pg-load", help="normalize, embed and load into Postgres")
    pl.add_argument("src")
    pl.add_argument("--dsn", **dsn)
    pl.add_argument("--embedder", **emb)
    pl.add_argument("--country", action="append")
    pl.add_argument("--min-completeness", type=float, default=0.0)
    pl.add_argument("--limit", type=int, help="stop after loading this many products")
    pl.add_argument("--batch-size", type=int, default=500)
    pl.add_argument("--keep-incomplete", action="store_true",
                    help="also load products without ingredients or nutrition")
    pl.add_argument("--hnsw", action="store_true",
                    help="build an HNSW index after loading (needs pgvector >= 0.8 for filtered search)")

    ps = sub.add_parser("pg-search", help="run a QuerySpec (JSON) against Postgres")
    ps.add_argument("spec")
    ps.add_argument("--dsn", **dsn)
    ps.add_argument("--embedder", **emb)

    a = sub.add_parser("ask", help="answer a natural-language question (needs OPENAI_API_KEY)")
    a.add_argument("question")
    a.add_argument("--dsn", **dsn)
    a.add_argument("--embedder", **emb)
    a.add_argument("--json", action="store_true", help="print spec, products and answer as JSON")

    args = ap.parse_args()

    if args.cmd == "build":
        seen, kept = build(args.src, args.db, args.country, args.min_completeness, args.jsonl_out)
        print(f"read {seen} products, kept {kept}")
    elif args.cmd == "query":
        sql, params = to_sql(QuerySpec.from_dict(json.loads(args.spec)))
        for _, _, _, record in sqlite3.connect(args.db).execute(sql, params):
            _print_record(json.loads(record))
    else:
        import psycopg

        from .pg import create_vector_index, load, make_embedder, search

        if not args.dsn:
            ap.error("set DATABASE_URL or pass --dsn")
        embedder = make_embedder(args.embedder)
        if args.cmd == "pg-load":
            seen, kept = load(
                args.src, args.dsn, embedder, args.country, args.min_completeness,
                args.batch_size, args.limit, not args.keep_incomplete, not args.keep_incomplete,
            )
            print(f"read {seen} products, loaded {kept}")
            if args.hnsw:
                with psycopg.connect(args.dsn) as conn:
                    create_vector_index(conn)
                print("HNSW index built")
        elif args.cmd == "pg-search":
            spec = QuerySpec.from_dict(json.loads(args.spec))
            vector = embedder.embed([spec.semantic_query])[0] if spec.semantic_query else None
            with psycopg.connect(args.dsn) as conn:
                for r in search(conn, spec, vector):
                    _print_record(r, r["similarity"])
        else:
            from .ask import ask

            with psycopg.connect(args.dsn) as conn:
                result = ask(conn, args.question, embedder)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print("Filters:", json.dumps({k: v for k, v in result["spec"].items() if v}, ensure_ascii=False))
                for note in result["notes"]:
                    print("Note:", note)
                print()
                print(result["answer"])


if __name__ == "__main__":
    main()
