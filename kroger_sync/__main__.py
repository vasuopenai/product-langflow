"""CLI for the Kroger service (the UI uses the HTTP API; this is for scripts).

  python -m kroger_sync locations 45202          # find a store
  python -m kroger_sync store 01400943 "Kroger Vine St"
  python -m kroger_sync load [--category cat:snacks ...]
  python -m kroger_sync refresh [--category ...]
  python -m kroger_sync lookup 012345678905 ...   # look up barcodes now
  python -m kroger_sync status | coverage
Env: DATABASE_URL, KROGER_CLIENT_ID, KROGER_CLIENT_SECRET (read from .env too).
"""

import argparse
import os

import psycopg

from . import sync
from .api import _load_dotenv
from .client import KrogerClient


def main():
    _load_dotenv()
    ap = argparse.ArgumentParser(prog="kroger_sync")
    ap.add_argument("--dsn", default=os.getenv("DATABASE_URL"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("locations").add_argument("zip")
    s = sub.add_parser("store")
    s.add_argument("location_id")
    s.add_argument("name", nargs="?")
    for mode in sync.MODES:
        sub.add_parser(mode).add_argument("--category", action="append", help="e.g. cat:snacks; repeatable")
    sub.add_parser("lookup").add_argument("codes", nargs="+")
    sub.add_parser("status")
    sub.add_parser("coverage")
    args = ap.parse_args()
    if not args.dsn:
        ap.error("set DATABASE_URL or pass --dsn")

    if args.cmd == "locations":
        for loc in KrogerClient().locations(args.zip):
            print(f"{loc['location_id']}  {loc['chain'] or '':10s} {loc['name']} - {loc['address']}")
        return
    with psycopg.connect(args.dsn) as conn:
        sync.init_schema(conn)
        if args.cmd == "store":
            sync.set_setting(conn, "location_id", args.location_id)
            sync.set_setting(conn, "location_name", args.name or args.location_id)
            print(sync.dumps(sync.status(conn)))
        elif args.cmd in sync.MODES:
            run_id = sync.start_run(conn, args.cmd, {"categories": args.category})
            if run_id is None:
                ap.error("a Kroger run is already in progress")
            sync.run_sync(args.dsn, run_id, args.cmd, args.category)
            print(sync.dumps(sync.latest_run(conn)))
        elif args.cmd == "lookup":
            print(sync.dumps(sync.items_for(conn, args.codes, KrogerClient())))
        elif args.cmd == "status":
            print(sync.dumps(sync.status(conn)))
        else:
            print(sync.dumps(sync.coverage(conn)))


if __name__ == "__main__":
    main()
