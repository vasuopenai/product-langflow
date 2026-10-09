"""Catalog gap finder command line.

Crawl (long-running, Kroger's official API):
  python -m catalog_gaps run                    # crawl, sort, stage; sleeps when the day's budget is spent
  python -m catalog_gaps run --steps 5          # just a few Kroger pages (testing)
  python -m catalog_gaps status                 # crawl progress and today's usage
  python -m catalog_gaps add-terms FILE         # more search terms (one per line)

Reference lists (once, and after new USDA / Open Food Facts downloads):
  python -m catalog_gaps build-refs --usda data/usda/branded_food.csv --off data/food.parquet

Gaps (from what was crawled; no Kroger calls):
  python -m catalog_gaps report                 # X Kroger products not in USDA, not in OFF, in neither ...
  python -m catalog_gaps export gaps.csv [--group not_in_usda] [--category snacks] [--brand kind]
  python -m catalog_gaps stage not_in_usda [--category snacks] [--brand kind] [--limit 100] [--dry-run]
  python -m catalog_gaps gaps [--limit 50]      # what's in staging from the catalog, newest first

Groups: not_in_usda, not_in_off, not_in_off_us, neither, off_not_usda, not_in_app, usda_not_app

Settings (environment): DATABASE_URL, KROGER_CLIENT_ID/SECRET, CATALOG_KROGER_CALLS_PER_DAY (2000),
CATALOG_SECONDS_PER_CALL (6), CATALOG_RESEARCH_PER_DAY (0 = store only), CATALOG_RECRAWL_DAYS (30).
"""

import argparse
import json
import os
import sys

import psycopg

from mobile_api.config import load_dotenv

from . import finder, gaps


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
    b = sub.add_parser("build-refs")
    b.add_argument("--usda", help="USDA branded_food.csv")
    b.add_argument("--off", help="Open Food Facts food.parquet")
    rep = sub.add_parser("report")
    rep.add_argument("--json", action="store_true")
    for name in ("export", "stage"):
        s = sub.add_parser(name)
        if name == "export":
            s.add_argument("path")
            s.add_argument("--group", choices=list(gaps.GROUPS))
        else:
            s.add_argument("group", choices=list(gaps.GROUPS))
            s.add_argument("--limit", type=int)
            s.add_argument("--dry-run", action="store_true")
        s.add_argument("--category", help="Kroger category contains this")
        s.add_argument("--brand", help="brand contains this")
    g = sub.add_parser("gaps")
    g.add_argument("--limit", type=int, default=50)
    args = p.parse_args(argv)

    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn) as conn:
        finder.init_schema(conn)
        finder.add_terms(conn, finder.read_terms())  # the bundled terms; new ones are added, none removed

        if args.cmd == "status":
            print(json.dumps(finder.status(conn), indent=2))
        elif args.cmd == "add-terms":
            print(f"added {finder.add_terms(conn, finder.read_terms(args.file))} new terms")
        elif args.cmd == "build-refs":
            from . import refs
            if not (args.usda or args.off):
                sys.exit("give --usda and/or --off")
            if args.usda:
                refs.build_usda(conn, args.usda)
            if args.off:
                refs.build_off(conn, args.off)
        elif args.cmd == "report":
            r = gaps.report(conn)
            print(json.dumps(r, indent=2) if args.json else gaps.format_report(r))
        elif args.cmd == "export":
            n = gaps.export_csv(conn, args.path, args.group, args.category, args.brand)
            print(f"wrote {n:,} products to {args.path}")
        elif args.cmd == "stage":
            print(json.dumps(gaps.stage_group(conn, args.group, args.category, args.brand, args.limit, args.dry_run)))
        elif args.cmd == "gaps":
            rows = conn.execute(
                """SELECT barcode, status, draft->>'brand', draft->>'name', draft->>'category'
                   FROM staging.scanned_products WHERE found_via = %s ORDER BY first_scanned DESC LIMIT %s""",
                (finder.FOUND_VIA, args.limit)).fetchall()
            for bc, st, brand, name, cat in rows:
                print(f"{bc:>14}  {st:<10} {(brand or '')[:22]:<22} {(name or '')[:50]:<50} {cat or ''}")
        if args.cmd != "run":
            return

    from kroger_sync.client import KrogerClient, configured
    if not configured():
        sys.exit("KROGER_CLIENT_ID and KROGER_CLIENT_SECRET are required")
    finder.run(dsn, KrogerClient, max_steps=args.steps, log=lambda m: print(m, flush=True))


if __name__ == "__main__":
    main()
