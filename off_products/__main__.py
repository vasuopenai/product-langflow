"""CLI.

  python -m off_products build openfoodfacts-products.jsonl.gz products.db --country en:united-states
  python -m off_products build food.parquet products.db --country en:united-states
  python -m off_products query products.db '{"semantic_query": "protein bar", ...}'
"""

import argparse
import json
import sqlite3

from .query import QuerySpec, to_sql
from .store import build


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
    args = ap.parse_args()

    if args.cmd == "build":
        seen, kept = build(args.src, args.db, args.country, args.min_completeness, args.jsonl_out)
        print(f"read {seen} products, kept {kept}")
    else:
        sql, params = to_sql(QuerySpec.from_dict(json.loads(args.spec)))
        for code, name, brand, record in sqlite3.connect(args.db).execute(sql, params):
            r = json.loads(record)
            serv = r["nutrition"]["per_serving"]
            free_of = [g for g, v in r["derived"]["groups"].items() if v["status"] == "none"]
            print(
                f"{code}  {name} ({brand})  protein/serving={serv['protein_g']}  "
                f"ingredients={r['ingredients']['count_total']}  free_of={free_of}"
            )


if __name__ == "__main__":
    main()
