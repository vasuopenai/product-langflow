"""Read OFF exports and write normalized products to SQLite.

SQLite keeps the reference implementation dependency-free; the same flat
columns map 1:1 to Postgres/DuckDB columns or Pinecone metadata.
"""

import gzip
import json
import sqlite3

from .normalize import normalize, to_flat, ingredient_amounts

TAG_KINDS = {
    "category": "category_tags",
    "label": "label_tags",
    "allergen": "allergen_tags",
    "country": "country_tags",
    "ingredient": "ingredient_tags",
    "oil": "oil_tags",
    "additive": "additive_tags",
    "free_of": "free_of",
    "contains_group": "contains_groups",
}


def iter_raw(path):
    """Yield raw OFF products from .jsonl, .jsonl.gz or .parquet."""
    if path.endswith(".parquet"):
        import pyarrow.parquet as pq  # optional dependency

        for batch in pq.ParquetFile(path).iter_batches(batch_size=2000):
            yield from batch.to_pylist()
        return
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def keep(record, countries=None, min_completeness=0.0):
    if record["quality"]["obsolete"] or not record["name"]:
        return False
    if countries and not set(countries) & set(record["countries"]):
        return False
    return (record["quality"]["completeness"] or 0) >= min_completeness


def _scalar_columns():
    from .query import NUTRIENT_NAMES

    cols = [
        "name", "brand", "main_category", "serving_g", "ingredients_n",
        "ingredients_top_level_n", "first_ingredient", "protein_kcal_pct",
        "nova_group", "nutriscore_grade", "vegan", "unique_scans_n", "completeness",
        "image_url",
    ]
    for basis in ("100g", "serving"):
        cols += [f"{n}_{basis}" for n in NUTRIENT_NAMES + ["sodium_g"] if f"{n}_{basis}" not in cols]
    return cols


def open_db(path):
    db = sqlite3.connect(path)
    cols = _scalar_columns()
    db.executescript(
        "CREATE TABLE IF NOT EXISTS products (code TEXT PRIMARY KEY, obsolete INTEGER, "
        + ", ".join(f'"{c}"' for c in cols)
        + ", search_text TEXT, record TEXT);"
        "CREATE TABLE IF NOT EXISTS product_tags (code TEXT, kind TEXT, tag TEXT);"
        "CREATE INDEX IF NOT EXISTS tags_kind_tag ON product_tags(kind, tag, code);"
        "CREATE INDEX IF NOT EXISTS tags_code ON product_tags(code);"
        "CREATE TABLE IF NOT EXISTS product_ingredients (code TEXT, ingredient TEXT, rank INTEGER, "
        "declared INTEGER, percent REAL, percent_min REAL, percent_max REAL, "
        "grams_per_100g REAL, grams_per_serving REAL);"
        "CREATE INDEX IF NOT EXISTS ing_ingredient ON product_ingredients(ingredient, code);"
        "CREATE INDEX IF NOT EXISTS ing_code ON product_ingredients(code);"
    )
    return db


def upsert(db, record):
    flat = to_flat(record)
    cols = _scalar_columns()
    db.execute("DELETE FROM product_tags WHERE code = ?", (record["code"],))
    db.execute("DELETE FROM product_ingredients WHERE code = ?", (record["code"],))
    db.execute(
        "INSERT OR REPLACE INTO products (code, obsolete, "
        + ", ".join(f'"{c}"' for c in cols)
        + ", search_text, record) VALUES (" + ", ".join("?" * (len(cols) + 4)) + ")",
        [record["code"], int(record["quality"]["obsolete"])]
        + [flat.get(c) for c in cols]
        + [record["search_text"], json.dumps(record, ensure_ascii=False)],
    )
    db.executemany(
        "INSERT INTO product_tags (code, kind, tag) VALUES (?, ?, ?)",
        [(record["code"], kind, tag) for kind, key in TAG_KINDS.items() for tag in set(flat[key])],
    )
    db.executemany(
        "INSERT INTO product_ingredients VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (record["code"], r["ingredient"], r["rank"], int(r["declared"]), r["percent"],
             r["percent_min"], r["percent_max"], r["grams_per_100g"], r["grams_per_serving"])
            for r in ingredient_amounts(record)
        ],
    )


def build(src, db_path, countries=None, min_completeness=0.0, jsonl_out=None):
    db = open_db(db_path)
    out = open(jsonl_out, "w", encoding="utf-8") if jsonl_out else None
    seen = kept = 0
    for raw in iter_raw(src):
        seen += 1
        record = normalize(raw)
        if not keep(record, countries, min_completeness):
            continue
        upsert(db, record)
        if out:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
        kept += 1
        if kept % 10000 == 0:
            db.commit()
    db.commit()
    if out:
        out.close()
    return seen, kept
