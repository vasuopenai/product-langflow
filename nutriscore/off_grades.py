"""Open Food Facts' published Nutri-Score per product, shown in place of ours when OFF has one.

Kept in a small table, ``nutriscore_off (code, grade)``, instead of inside each product record:
refreshing it from a newer OFF export rewrites ~275k small rows, not 435k product rows (whose
rewrite also re-inserts every row into the vector index). Readers combine it with our grade:
mobile_api.products adds it to records as scores.nutriscore_off, and the max_nutriscore_grade
filter uses COALESCE(OFF grade, our grade).

    python -m nutriscore off-grades --parquet data/food.parquet [--csv data/off_grades.csv]
    python -m nutriscore off-grades --load data/off_grades.csv      # e.g. on the server

Both write the table (replacing its contents); --csv also saves the grades to a file so a
server without the 8 GB parquet can load them.
"""

import csv
import re

GRADES = set("abcde")
SCHEMA = "CREATE TABLE IF NOT EXISTS nutriscore_off (code TEXT PRIMARY KEY, grade TEXT NOT NULL)"


def _key(code):
    return re.sub(r"\D", "", code or "").lstrip("0") or None


def from_parquet(parquet_path, codes, progress=print):
    """{our product code: OFF grade 'a'..'e'} for the given codes. Reads only two columns."""
    import pyarrow.parquet as pq

    ours = {_key(c): c for c in codes}
    out = {}
    pf = pq.ParquetFile(parquet_path)
    for i in range(pf.num_row_groups):
        table = pf.read_row_group(i, columns=["code", "nutriscore_grade"])
        for code, grade in zip(table["code"].to_pylist(), table["nutriscore_grade"].to_pylist()):
            mine = ours.get(_key(code))
            if mine and (grade or "").lower() in GRADES:
                out[mine] = grade.lower()
        if progress and i % 500 == 0:
            progress(f"OFF parquet: row group {i}/{pf.num_row_groups}, {len(out)} graded matches")
    return out


def save(conn, grades):
    """Replace the table's contents with ``grades`` ({code: grade}) in one transaction."""
    conn.execute(SCHEMA)
    conn.execute("TRUNCATE nutriscore_off")
    with conn.cursor().copy("COPY nutriscore_off (code, grade) FROM STDIN") as copy:
        for code, grade in grades.items():
            copy.write_row((code, grade))
    conn.commit()


def write_csv(path, grades):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "grade"])
        w.writerows(sorted(grades.items()))


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {r["code"]: r["grade"] for r in csv.DictReader(f) if r["grade"] in GRADES}


def main(argv):
    import argparse
    import collections
    import os

    import psycopg

    ap = argparse.ArgumentParser(prog="python -m nutriscore off-grades")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--parquet", help="Open Food Facts export (data/food.parquet)")
    src.add_argument("--load", help="CSV written earlier with --csv")
    ap.add_argument("--csv", help="also save the grades to this CSV")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/food"))
    args = ap.parse_args(argv)
    with psycopg.connect(args.dsn) as conn:
        if args.parquet:
            codes = [c for (c,) in conn.execute("SELECT code FROM products")]
            grades = from_parquet(args.parquet, codes)
        else:
            grades = read_csv(args.load)
        if args.csv:
            write_csv(args.csv, grades)
        save(conn, grades)
    counts = collections.Counter(grades.values())
    print(f"saved {len(grades)} OFF grades: " + ", ".join(f"{g.upper()} {counts[g]}" for g in "abcde"))
