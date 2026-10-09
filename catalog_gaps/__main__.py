"""Catalog gap finder command line.

  python -m catalog_gaps run                 # long-running: crawl, sort, stage; sleeps when budgets are spent
  python -m catalog_gaps run --steps 5       # just a few Kroger pages (testing)
  python -m catalog_gaps status              # progress, what was found, today's usage
  python -m catalog_gaps add-terms FILE      # more search terms (one per line)
  python -m catalog_gaps gaps [--limit 50]   # the staged products, newest first

Settings (environment): DATABASE_URL, KROGER_CLIENT_ID/SECRET, CATALOG_KROGER_CALLS_PER_DAY (2000),
CATALOG_SECONDS_PER_CALL (6), CATALOG_RESEARCH_PER_DAY (0 = store only), CATALOG_RECRAWL_DAYS (30),
OFF_DATABASE_URL (local Open Food Facts copy; without it the Open Food Facts API is used).
"""

import argparse
import json
import os
import sys

import psycopg

from mobile_api.config import load_dotenv

from . import finder


def main(argv=None):
    load_dotenv()
    p = argparse.ArgumentParser(prog="catalog_gaps", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--steps", type=int, help="stop after this many loop steps")
    sub.add_parser("status")
    t = sub.add_parser("add-terms")
    t.add_argument("file")
    g = sub.add_parser("gaps")
    g.add_argument("--limit", type=int, default=50)
    args = p.parse_args(argv)

    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn) as conn:
        finder.init_schema(conn)
        finder.add_terms(conn, finder.read_terms())  # the bundled terms; new ones are added, none removed

        if args.cmd == "status":
            print(json.dumps(finder.status(conn), indent=2))
            return
        if args.cmd == "add-terms":
            print(f"added {finder.add_terms(conn, finder.read_terms(args.file))} new terms")
            return
        if args.cmd == "gaps":
            rows = conn.execute(
                """SELECT barcode, status, draft->>'brand', draft->>'name', draft->>'category'
                   FROM staging.scanned_products WHERE found_via = %s ORDER BY first_scanned DESC LIMIT %s""",
                (finder.FOUND_VIA, args.limit)).fetchall()
            for b, st, brand, name, cat in rows:
                print(f"{b:>14}  {st:<10} {(brand or '')[:22]:<22} {(name or '')[:50]:<50} {cat or ''}")
            return

    from kroger_sync.client import KrogerClient, configured
    if not configured():
        sys.exit("KROGER_CLIENT_ID and KROGER_CLIENT_SECRET are required")
    finder.run(dsn, KrogerClient, max_steps=args.steps, log=lambda m: print(m, flush=True))


if __name__ == "__main__":
    main()
