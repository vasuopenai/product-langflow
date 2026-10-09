"""Read Open Food Facts' published Nutri-Score for a set of barcodes from the full
parquet export (data/food.parquet).

OFF's ``nutriscore_grade`` / ``nutriscore_score`` are the *current* version, which
has been the 2023 algorithm since OFF switched over (its misc tags
"en:nutriscore-2021-X-2023-Y" record the old/new pair during the migration). The
export has no separate 2021 column, so the 2021 grade is only recoverable from those
tags; we keep them for reference.
"""

import re

COLUMNS = ["code", "nutriscore_grade", "nutriscore_score", "categories_tags", "misc_tags",
           "nutriments", "with_non_nutritive_sweeteners", "product_name", "countries_tags"]
# Nutriments kept (per 100 g / 100 ml values) for the "OFF inputs" check.
NUTRIMENTS = {"energy-kj", "energy-kcal", "energy", "sugars", "saturated-fat", "fat", "salt", "sodium",
              "fiber", "proteins", "carbohydrates",
              "fruits-vegetables-legumes", "fruits-vegetables-legumes-estimate-from-ingredients"}
_V2021 = re.compile(r"^en:nutriscore-2021-([a-e])-2023-([a-e])$")


def key(code):
    """Barcode key: digits with leading zeros stripped (same as mobile_api.barcodes.key
    without UPC-E expansion, which OFF codes don't need)."""
    digits = re.sub(r"\D", "", code or "")
    return digits.lstrip("0") or None


def load(parquet_path, keys, progress=None):
    """{barcode key: OFF row dict} for the barcodes in ``keys``. Reads the code column
    first and only materialises the other columns for matching rows."""
    import pyarrow.parquet as pq

    keys = set(keys)
    pf = pq.ParquetFile(parquet_path)
    out = {}
    for i in range(pf.num_row_groups):
        codes = pf.read_row_group(i, columns=["code"])["code"].to_pylist()
        idx = [j for j, c in enumerate(codes) if key(c) in keys]
        if idx:
            table = pf.read_row_group(i, columns=COLUMNS).take(idx)
            for row in table.to_pylist():
                out[key(row["code"])] = _slim(row)
        if progress and i % 500 == 0:
            progress(f"OFF parquet: row group {i}/{pf.num_row_groups}, {len(out)} matches")
    return out


def _slim(row):
    nutr = {}
    for n in row.pop("nutriments") or []:
        if n["name"] in NUTRIMENTS and n.get("100g") is not None:
            nutr[n["name"]] = n["100g"]
    row["nutriments"] = nutr
    misc = row.get("misc_tags") or []
    row["misc_tags"] = [m for m in misc if m.startswith(("en:nutriscore", "en:nutrition"))]
    row["grade_2021"] = next((m.group(1) for m in map(_V2021.match, misc) if m), None)
    return row
