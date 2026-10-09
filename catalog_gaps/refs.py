"""Barcode reference lists for the gap report: every product in the full USDA Branded Foods
file and the full Open Food Facts export, as canonical keys (digits, leading zeros stripped,
the same form kroger_keys() produces). Built on a machine that has the downloads:

    python -m catalog_gaps build-refs --usda data/usda/branded_food.csv --off data/food.parquet

and copied to the server with `deploy/lightsail/lightsail.sh push-refs`.
"""

import csv
import sys

from mobile_api.barcodes import key


def _usable(k):
    return bool(k) and len(k) >= 8


def _replace(conn, table, columns, rows, insert_sql, name, source, progress):
    conn.execute(f"CREATE TEMP TABLE _refs ({columns}) ON COMMIT DROP")
    n = 0
    with conn.cursor().copy(f"COPY _refs FROM STDIN") as cp:
        for row in rows:
            cp.write_row(row)
            n += 1
            if n % 500_000 == 0:
                progress(f"  {name}: {n:,} rows read")
    conn.execute(f"TRUNCATE {table}")
    conn.execute(insert_sql)
    distinct = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    conn.execute("INSERT INTO catalog.refs (name, source, rows, built_at) VALUES (%s, %s, %s, now()) "
                 "ON CONFLICT (name) DO UPDATE SET source = EXCLUDED.source, rows = EXCLUDED.rows, built_at = now()",
                 (name, source, distinct))
    conn.commit()
    progress(f"{name}: {n:,} rows read, {distinct:,} distinct barcodes")
    return distinct


def build_usda(conn, csv_path, progress=print):
    """All barcodes in USDA's branded_food.csv (every product and version USDA lists)."""
    csv.field_size_limit(sys.maxsize if sys.maxsize < 2**31 else 2**31 - 1)

    def rows():
        with open(csv_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                k = key(r.get("gtin_upc"))
                if _usable(k):
                    yield (k,)

    return _replace(conn, "catalog.usda_codes", "code TEXT", rows(),
                    "INSERT INTO catalog.usda_codes SELECT DISTINCT code FROM _refs",
                    "usda", str(csv_path), progress)


def build_off(conn, parquet_path, progress=print):
    """All barcodes in the Open Food Facts export, with whether OFF lists them as sold in the US."""
    import pyarrow.parquet as pq

    def rows():
        f = pq.ParquetFile(parquet_path)
        for batch in f.iter_batches(columns=["code", "countries_tags"], batch_size=200_000):
            codes = batch.column("code").to_pylist()
            countries = batch.column("countries_tags").to_pylist()
            for code, tags in zip(codes, countries):
                k = key(code)
                if _usable(k):
                    yield (k, bool(tags) and "en:united-states" in tags)

    return _replace(conn, "catalog.off_codes", "code TEXT, us BOOLEAN", rows(),
                    "INSERT INTO catalog.off_codes SELECT code, bool_or(us) FROM _refs GROUP BY code",
                    "off", str(parquet_path), progress)
