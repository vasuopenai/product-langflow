"""CLI.

Local SQLite (no services needed):
  python -m off_products build food.parquet products.db --country en:united-states
  python -m off_products query products.db '{"semantic_query": "protein bar", ...}'

Postgres + pgvector (DATABASE_URL, OPENAI_API_KEY):
  python -m off_products pg-load food.parquet --country en:united-states
  python -m off_products pg-search '{"semantic_query": "protein bar", ...}'
  python -m off_products ask "protein bar with at least 20 g protein and no seed oils"

Kroger (KROGER_CLIENT_ID, KROGER_CLIENT_SECRET):
  python -m off_products kroger-locations --zip 45202
  python -m off_products kroger-crawl --location 01400943 --terms examples/kroger_terms.txt
  python -m off_products usda-index path/to/FoodData_Central_branded_food_csv
  python -m off_products kroger-match            # report + tag products sold at Kroger
"""

import argparse
import json
import os
import sqlite3

from ._env import load_dotenv
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


def _spec(arg):
    """JSON text, or @path to a JSON file (avoids shell quoting, e.g. in PowerShell)."""
    if arg.startswith("@"):
        with open(arg[1:], encoding="utf-8") as f:
            return QuerySpec.from_dict(json.load(f))
    return QuerySpec.from_dict(json.loads(arg))


def main():
    load_dotenv()
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
    q.add_argument("spec", help="QuerySpec JSON, or @file.json")

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
    ps.add_argument("spec", help="QuerySpec JSON, or @file.json")
    ps.add_argument("--dsn", **dsn)
    ps.add_argument("--embedder", **emb)

    a = sub.add_parser("ask", help="answer a natural-language question (needs OPENAI_API_KEY)")
    a.add_argument("question")
    a.add_argument("--dsn", **dsn)
    a.add_argument("--embedder", **emb)
    a.add_argument("--json", action="store_true", help="print spec, products and answer as JSON")

    kl = sub.add_parser("kroger-locations", help="find Kroger-family stores near a ZIP code")
    kl.add_argument("--zip", required=True)

    kc = sub.add_parser("kroger-crawl", help="resumable product crawl for one store")
    kc.add_argument("--location", help="locationId from kroger-locations (needed for price/aisle)")
    kc.add_argument("--terms", default="examples/kroger_terms.txt", help="file with one search term per line")
    kc.add_argument("--out", default="data/kroger")
    kc.add_argument("--max-calls", type=int, default=9000, help="daily call budget (public limit ~10,000)")

    ui = sub.add_parser("usda-index", help="index your USDA branded-foods dump by barcode")
    ui.add_argument("src", help="CSV folder with branded_food.csv, or the branded .json file")
    ui.add_argument("--db", default="data/usda_index.db")

    km = sub.add_parser("kroger-match", help="join Kroger crawl to USDA and the product store")
    km.add_argument("--kroger", default="data/kroger/products.jsonl")
    km.add_argument("--usda", default="data/usda_index.db", help="index from usda-index ('' to skip)")
    km.add_argument("--report", default="data/kroger/coverage.csv")
    km.add_argument("--dsn", default=os.getenv("DATABASE_URL"), help="Postgres store to tag (default $DATABASE_URL)")
    km.add_argument("--sqlite", help="tag a SQLite store from `build` instead of Postgres")

    args = ap.parse_args()

    if args.cmd.startswith(("kroger-", "usda-")):
        return _retail_commands(args)

    if args.cmd == "build":
        seen, kept = build(args.src, args.db, args.country, args.min_completeness, args.jsonl_out)
        print(f"read {seen} products, kept {kept}")
    elif args.cmd == "query":
        sql, params = to_sql(_spec(args.spec))
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
            spec = _spec(args.spec)
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


def _retail_commands(args):
    from pathlib import Path

    if args.cmd == "kroger-locations":
        from .kroger import KrogerClient

        for loc in KrogerClient().locations(args.zip):
            print(f"{loc['location_id']}  {loc['chain']:<10} {loc['name']} - {loc['address']}")
    elif args.cmd == "kroger-crawl":
        from .kroger import KrogerClient, crawl

        terms = [t.strip() for t in Path(args.terms).read_text(encoding="utf-8").splitlines()
                 if t.strip() and not t.startswith("#")]
        client = KrogerClient()
        n = crawl(client, terms, args.out, args.location, args.max_calls)
        print(f"wrote {n} new products to {args.out}/products.jsonl ({client.calls} API calls this run)")
    elif args.cmd == "usda-index":
        from .usda import build_index

        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        read, unique = build_index(args.src, args.db)
        print(f"read {read} USDA records, {unique} unique barcodes -> {args.db}")
    else:
        from .retail import link_to_store, match, store_keys, summarize, write_report

        conn, ph = None, "?"
        if args.sqlite:
            conn = sqlite3.connect(args.sqlite)
        elif args.dsn:
            import psycopg

            conn, ph = psycopg.connect(args.dsn), "%s"
        keys = store_keys(conn) if conn else {}
        rows = match(args.kroger, args.usda or None, keys)
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        write_report(rows, args.report)
        print(f"{'category':<40} {'total':>6} {'in store':>9} {'USDA ingr':>10} {'Kroger ingr':>12} {'any label':>10}")
        for cat, c in summarize(rows).items():
            print(f"{cat[:40]:<40} {c['total']:>6} {c['in_store']:>9} {c['usda_ingredients']:>10} "
                  f"{c['kroger_ingredients']:>12} {c['any_label']:>10}")
        print(f"report: {args.report}")
        if conn:
            print(f"tagged {link_to_store(conn, rows, ph)} store products as sold at Kroger")
            conn.close()


if __name__ == "__main__":
    main()
