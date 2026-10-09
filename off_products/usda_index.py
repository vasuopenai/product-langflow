"""Index a USDA FoodData Central Branded Foods dump by barcode, for joining.

Accepts either download format:
  * CSV folder containing branded_food.csv (and food.csv for descriptions)
  * the branded JSON file ({"BrandedFoods": [...]}); streamed with ijson
    (pip install ijson) because the file is several GB

Writes a small SQLite table usda(gtin_key, fdc_id, ...) with one row per
barcode, keeping the most recently published record.
"""

import csv
import os
import sqlite3
import sys

from .gtin import key

FIELDS = ["gtin_key", "fdc_id", "gtin_upc", "description", "brand_owner", "brand_name",
          "category", "ingredients", "serving_size", "serving_size_unit", "available_date"]


def _iter_csv(folder):
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    descriptions = {}
    food_csv = os.path.join(folder, "food.csv")
    if os.path.exists(food_csv):
        with open(food_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                descriptions[row.get("fdc_id")] = row.get("description")
    with open(os.path.join(folder, "branded_food.csv"), newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            yield {
                "fdc_id": row.get("fdc_id"),
                "gtin_upc": row.get("gtin_upc"),
                "description": descriptions.get(row.get("fdc_id")) or row.get("short_description"),
                "brand_owner": row.get("brand_owner"),
                "brand_name": row.get("brand_name"),
                "category": row.get("branded_food_category"),
                "ingredients": row.get("ingredients"),
                "serving_size": row.get("serving_size"),
                "serving_size_unit": row.get("serving_size_unit"),
                "available_date": row.get("available_date") or row.get("modified_date"),
            }


def _iter_json(path):
    try:
        import ijson
    except ImportError as e:
        raise SystemExit("the USDA JSON dump needs ijson: pip install ijson (or use the CSV download)") from e
    with open(path, "rb") as f:
        for food in ijson.items(f, "BrandedFoods.item"):
            yield {
                "fdc_id": food.get("fdcId"),
                "gtin_upc": food.get("gtinUpc"),
                "description": food.get("description"),
                "brand_owner": food.get("brandOwner"),
                "brand_name": food.get("brandName"),
                "category": food.get("brandedFoodCategory"),
                "ingredients": food.get("ingredients"),
                "serving_size": food.get("servingSize"),
                "serving_size_unit": food.get("servingSizeUnit"),
                "available_date": food.get("availableDate") or food.get("modifiedDate"),
            }


def iter_usda(path):
    if os.path.isdir(path):
        return _iter_csv(path)
    if path.endswith(".json"):
        return _iter_json(path)
    raise SystemExit("point at the CSV folder (with branded_food.csv) or the branded .json file")


def build_index(src, db_path, progress=print):
    db = sqlite3.connect(db_path)
    db.execute(f"CREATE TABLE IF NOT EXISTS usda ({', '.join(FIELDS)}, PRIMARY KEY (gtin_key))")
    n = 0
    for row in iter_usda(src):
        k = key(row["gtin_upc"])
        if not k:
            continue
        row = {**row, "gtin_key": k, "fdc_id": str(row["fdc_id"])}
        # newest record per barcode wins (dates are ISO-ish; compare as text)
        db.execute(
            f"INSERT INTO usda ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))}) "
            "ON CONFLICT(gtin_key) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in FIELDS[1:])
            + " WHERE coalesce(excluded.available_date, '') >= coalesce(usda.available_date, '')",
            [row[c] for c in FIELDS],
        )
        n += 1
        if n % 100000 == 0:
            db.commit()
            progress(f"indexed {n} USDA records")
    db.commit()
    count = db.execute("SELECT count(*) FROM usda").fetchone()[0]
    db.close()
    return n, count


def lookup(db, keys):
    """First USDA row matching any candidate key, or None."""
    for k in keys:
        row = db.execute(f"SELECT {', '.join(FIELDS)} FROM usda WHERE gtin_key = ?", (k,)).fetchone()
        if row:
            return dict(zip(FIELDS, row))
    return None
