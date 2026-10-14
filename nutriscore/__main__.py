"""CLI.

  python -m nutriscore validate [--sample N] [--json report.json]   # ours vs Open Food Facts
  python -m nutriscore show 0016000275287 [...]                       # one product, with points
  python -m nutriscore off-grades --parquet data/food.parquet [--csv F]  # OFF's grades (off_grades.py)
"""

import json
import os
import sys


def show(codes, db):
    import psycopg

    from .usda import compute
    from .validate import fetch_records

    with psycopg.connect(db) as conn:
        conn.read_only = True
        variants = {}
        for c in codes:
            digits = "".join(ch for ch in c if ch.isdigit()).lstrip("0")
            for n in (12, 13, 14):
                if len(digits) <= n:
                    variants[digits.zfill(n)] = c
            variants[digits] = c
        found = list(fetch_records(conn, list(variants)))
        if not found:
            print("no product found")
        for code, record in found:
            res = compute(record)
            print(f"{code}  {record.get('name')}  [{record.get('main_category')}]")
            print(json.dumps({k: v for k, v in res.items()}, indent=1, default=str))


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2 or sys.argv[1] not in ("validate", "show", "off-grades"):
        print(__doc__)
        sys.exit(2)
    if sys.argv[1] == "off-grades":
        from .off_grades import main as off_grades_main
        off_grades_main(sys.argv[2:])
    elif sys.argv[1] == "validate":
        from .validate import main as validate_main
        validate_main(sys.argv[2:])
    else:
        show(sys.argv[2:], os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/food"))


main()
