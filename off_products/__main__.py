"""CLI.

Local SQLite (no services needed):
  python -m off_products build food.parquet products.db --country en:united-states
  python -m off_products query products.db '{"semantic_query": "protein bar", ...}'

Postgres + pgvector (DATABASE_URL, OPENAI_API_KEY):
  python -m off_products pg-load food.parquet --country en:united-states
  python -m off_products pg-load data/usda --source usda --off-enrich data/food.parquet
  python -m off_products pg-rederive      # re-apply changed rules to the stored USDA rows
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
import sys

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
    # Product text has characters outside the Windows console code page (cp1252),
    # which crashes print when output is redirected to a file.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
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
    pl.add_argument("src", help="OFF .jsonl/.parquet file, or the unzipped USDA branded CSV directory")
    pl.add_argument("--source", choices=["off", "usda"], default="off",
                    help="usda: FoodData Central Branded Foods (USDA is the source of truth)")
    pl.add_argument("--off-enrich", metavar="FOOD_PARQUET",
                    help="with --source usda: add labels, image, NOVA and popularity from OFF by barcode")
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

    pr = sub.add_parser("pg-rederive", help="re-apply the current parsing/category/label rules to the "
                        "USDA rows stored in Postgres (no source files; embeds only changed text)")
    pr.add_argument("--dsn", **dsn)
    pr.add_argument("--embedder", **emb)
    pr.add_argument("--batch-size", type=int, default=1000)

    rc = sub.add_parser("pg-recheck", help="re-check stored ingredient statements for hydrogenated oils the "
                        "parser missed in sub-ingredients (fast; no re-parse or embeddings)")
    rc.add_argument("--dsn", **dsn)
    rc.add_argument("--dry-run", action="store_true", help="count only; write nothing")

    rt = sub.add_parser("pg-retag", help="add the named category tags where the current rules give them "
                        "to stored products (e.g. --tag cat:baby-food); only adds product_tags rows")
    rt.add_argument("--tag", action="append", required=True, help="category id to add; repeatable")
    rt.add_argument("--dsn", **dsn)
    rt.add_argument("--dry-run", action="store_true", help="count only; write nothing")

    ns = sub.add_parser("pg-nutriscore", help="compute our Nutri-Score (nutriscore/) for every stored product "
                        "and save it; reruns only write changed grades")
    ns.add_argument("--dsn", **dsn)
    ns.add_argument("--dry-run", action="store_true", help="count grades only; write nothing")

    of = sub.add_parser("off-fill", help="fill empty USDA ingredients/nutrients from Open Food Facts, and add "
                        "USDA products skipped at load that become complete (see off_products/off_fill.py)")
    of.add_argument("--parquet", default="data/food.parquet", help="Open Food Facts export")
    of.add_argument("--usda", default="data/usda", help="USDA branded CSV folder")
    of.add_argument("--read-dsn", help="database to read USDA records from (default: --dsn)")
    of.add_argument("--dsn", **dsn)
    of.add_argument("--embedder", **emb)
    of.add_argument("--batch-size", type=int, default=500)
    of.add_argument("--dry-run", action="store_true", help="count only; write nothing")

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
        if args.cmd == "pg-retag":
            from .pg import retag_categories
            with psycopg.connect(args.dsn) as conn:
                added = retag_categories(conn, set(args.tag), dry_run=args.dry_run)
            print(f"{'would add' if args.dry_run else 'added'}: " + (", ".join(
                f"{t} {n}" for t, n in added.most_common()) or "nothing"))
            return
        if args.cmd == "pg-nutriscore":
            from .pg import fill_nutriscore
            grades = fill_nutriscore(args.dsn, dry_run=args.dry_run)
            print(f"{'would grade' if args.dry_run else 'graded'}: " + ", ".join(
                f"{g.upper() if g != 'none' else 'not computable'} {n}" for g, n in sorted(grades.items())))
            return
        if args.cmd == "pg-recheck":
            from .pg import recheck_statements
            with psycopg.connect(args.dsn) as conn:
                changed = recheck_statements(conn, dry_run=args.dry_run)
            print(f"{'would mark' if args.dry_run else 'marked'} {len(changed)} products as containing "
                  f"hydrogenated oils: {' '.join(changed[:20])}{' ...' if len(changed) > 20 else ''}")
            return
        embedder = make_embedder(args.embedder)
        if args.cmd == "pg-load":
            seen, kept = load(
                args.src, args.dsn, embedder, args.country, args.min_completeness,
                args.batch_size, args.limit, not args.keep_incomplete, not args.keep_incomplete,
                source=args.source, off_parquet=args.off_enrich,
            )
            print(f"read {seen} products, loaded {kept}")
            if args.hnsw:
                with psycopg.connect(args.dsn) as conn:
                    create_vector_index(conn)
                print("HNSW index built")
        elif args.cmd == "pg-rederive":
            seen, kept = load(args.dsn, args.dsn, embedder, batch_size=args.batch_size, source="stored")
            print(f"re-derived {seen} products, kept {kept}")
        elif args.cmd == "off-fill":
            from .off_fill import plan
            from .pg import init_schema, upsert_batch
            with psycopg.connect(args.dsn) as conn:
                app_codes = [c for (c,) in conn.execute("SELECT code FROM products")]
            with psycopg.connect(args.read_dsn or args.dsn) as read_conn:
                updated, new, tally = plan(read_conn, args.parquet, args.usda, app_codes)
            print(json.dumps(tally, indent=2))
            print(f"would update {len(updated):,} products and add {len(new):,}")
            if not args.dry_run:
                with psycopg.connect(args.dsn) as conn:
                    init_schema(conn, embedder.dim)
                    # Resumable: products the target already has filled are skipped.
                    done = {c for (c,) in conn.execute(
                        "SELECT code FROM products WHERE jsonb_array_length("
                        "coalesce(record->'source'->'filled_from_off', '[]'::jsonb)) > 0")}
                    records = [r for r in updated if r["code"] not in done] + new
                    print(f"{len(updated) - (len(records) - len(new)):,} already filled; writing {len(records):,}")
                    for i in range(0, len(records), args.batch_size):
                        upsert_batch(conn, records[i:i + args.batch_size], embedder)
                        conn.commit()
                        print(f"written {min(i + args.batch_size, len(records)):,} of {len(records):,}", flush=True)
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
        from .usda_index import build_index

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
