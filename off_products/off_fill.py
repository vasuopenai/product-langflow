"""Fill what USDA leaves empty with Open Food Facts data: the ingredient statement when USDA's is
empty, and per-100 g nutrients USDA doesn't report. USDA's own values always win.

The fill is kept in the USDA raw row's "off" block (``raw["off"]["fill"]``) and applied by
``usda.normalize`` (``with_off_fill``), so re-deriving keeps it and the record lists what was filled
(``source.filled_from_off``). A fill is used only if the product stays plausible with it.

    python -m off_products off-fill --parquet data/food.parquet --usda data/usda \\
        --read-dsn <local db with the USDA records> --dsn <target db> [--dry-run]
"""

import math

from . import usda

# Open Food Facts nutriment (per 100 g) -> (USDA nutrient id, factor to USDA's unit).
# OFF gives sodium and cholesterol in grams; USDA's amounts are milligrams.
OFF_TO_USDA = {
    "energy-kcal": ("1008", 1), "proteins": ("1003", 1), "fat": ("1004", 1), "saturated-fat": ("1258", 1),
    "trans-fat": ("1257", 1), "carbohydrates": ("1005", 1), "sugars": ("2000", 1), "added-sugars": ("1235", 1),
    "fiber": ("1079", 1), "sodium": ("1093", 1000), "cholesterol": ("1253", 1000),
}
TEXT_LANGS = ("en", "main")  # US products: English first


def _ingredients_text(entries):
    by_lang = {e.get("lang"): (e.get("text") or "").strip() for e in entries or [] if e}
    return next((by_lang[l] for l in TEXT_LANGS if by_lang.get(l)), None)


def _nutrients(nutriments):
    out = {}
    for n in nutriments or []:
        spec = OFF_TO_USDA.get((n or {}).get("name"))
        value = (n or {}).get("100g")
        if spec and value is not None and math.isfinite(value) and value >= 0:
            out[spec[0]] = round(value * spec[1], 4)
    return out


def build_index(parquet_path, keys, progress=print):
    """Barcode key -> {"ingredients_text", "nutrients": {USDA id: amount per 100 g}} for ``keys``,
    from the Open Food Facts export (only entries that have something)."""
    import pyarrow.parquet as pq

    out = {}
    pf = pq.ParquetFile(parquet_path)
    for batch in pf.iter_batches(batch_size=200_000, columns=["code", "ingredients_text", "nutriments"]):
        codes = batch.column("code").to_pylist()
        hits = [i for i, c in enumerate(codes) if usda.gtin_key(c) in keys]
        if not hits:
            continue
        texts = batch.column("ingredients_text").to_pylist()
        nutriments = batch.column("nutriments").to_pylist()
        for i in hits:
            entry = {"ingredients_text": _ingredients_text(texts[i]), "nutrients": _nutrients(nutriments[i])}
            if entry["ingredients_text"] or entry["nutrients"]:
                k = usda.gtin_key(codes[i])
                # Several OFF rows can share a key: keep the one with more nutrients.
                if k not in out or len(entry["nutrients"]) > len(out[k]["nutrients"]):
                    out[k] = entry
    progress(f"Open Food Facts: fill data for {len(out):,} of {len(keys):,} barcodes")
    return out


def fill_for(raw, entry):
    """The part of an OFF entry that fills a gap in this USDA row (None if nothing does)."""
    if not entry:
        return None
    have = raw.get("nutrients") or {}
    fill = {}
    if not (raw.get("ingredients") or "").strip() and entry.get("ingredients_text"):
        fill["ingredients_text"] = entry["ingredients_text"]
    missing = {nid: v for nid, v in (entry.get("nutrients") or {}).items() if have.get(nid) is None}
    if missing:
        fill["nutrients"] = missing
    return fill or None


def with_fill(raw, fill):
    return {**raw, "off": {**(raw.get("off") or {}), "fill": fill}}


# A part can't exceed its whole (small slack for label rounding).
PARTS = [("saturated_fat_g", "fat_g", 0.5), ("trans_fat_g", "fat_g", 0.5), ("sugars_g", "carbs_g", 1),
         ("fiber_g", "carbs_g", 1), ("added_sugars_g", "sugars_g", 1)]


def inconsistent(per_100g):
    return [f"{part} > {whole}" for part, whole, slack in PARTS
            if per_100g.get(part) is not None and per_100g.get(whole) is not None
            and per_100g[part] > per_100g[whole] + slack]


def refill(raw, entry):
    """(record with the fill, filled fields) or (None, reason) when the fill doesn't help or would
    make the product implausible, inconsistent (e.g. saturated fat above total fat) or lose calories."""
    fill = fill_for(raw, entry)
    if not fill:
        return None, "nothing to fill"
    before = usda.normalize(raw)
    after = usda.normalize(with_fill(raw, fill))
    if after["nutrition"]["implausible"] and not before["nutrition"]["implausible"]:
        return None, "would be implausible"
    if set(inconsistent(after["nutrition"]["per_100g"])) - set(inconsistent(before["nutrition"]["per_100g"])):
        return None, "would be inconsistent"
    if before["nutrition"]["per_100g"]["energy_kcal"] is not None and after["nutrition"]["per_100g"]["energy_kcal"] is None:
        return None, "would lose calories"
    filled = after["source"]["filled_from_off"]
    return (after, filled) if filled else (None, "nothing to fill")


# --- the two jobs ------------------------------------------------------------------------

MISSING_NUTRIENT = " OR ".join(f"record->'nutrition'->'per_100g'->>'{n}' IS NULL" for n in
                               ("protein_g", "fat_g", "saturated_fat_g", "carbs_g", "sugars_g", "fiber_g", "sodium_g"))


def existing_candidates(conn):
    """USDA-loaded products (they keep their raw row) missing a main nutrient: (code, raw)."""
    return conn.execute(f"""SELECT code, record->'source'->'raw' FROM products
                            WHERE record->'source' ? 'raw' AND ({MISSING_NUTRIENT})""").fetchall()


def missing_usda_rows(usda_dir, app_keys, progress=print):
    """Current USDA rows whose barcode isn't in the app (skipped at load: empty ingredients or nutrition)."""
    for raw in usda.iter_usda(usda_dir, progress=progress):
        if usda.gtin_key(raw["gtin_upc"]) not in app_keys:
            yield raw


def plan(read_conn, parquet_path, usda_dir, app_codes=None, progress=print):
    """Compute both fills without writing. Returns (updated records, new records, tallies).
    ``app_codes``: the target database's product codes (default: read_conn's), so products
    already there from other sources (approved scans, catalog, OFF) aren't added again."""
    if app_codes is None:
        app_codes = [c for (c,) in read_conn.execute("SELECT code FROM products")]
    app_keys = {usda.gtin_key(c) for c in app_codes}
    existing = existing_candidates(read_conn)
    progress(f"products in the app missing a main nutrient: {len(existing):,}")
    missing_raws = list(missing_usda_rows(usda_dir, app_keys, progress))
    progress(f"USDA products not in the app: {len(missing_raws):,}")
    keys = {usda.gtin_key(c) for c, _ in existing} | {usda.gtin_key(r["gtin_upc"]) for r in missing_raws}
    index = build_index(parquet_path, keys, progress)

    updated, new, tally = [], [], {"existing": {}, "missing": {}, "filled_fields": {}}

    def count(part, reason):
        tally[part][reason] = tally[part].get(reason, 0) + 1

    for code, raw in existing:
        record, info = refill(raw, index.get(usda.gtin_key(code)))
        if record is None:
            count("existing", info)
            continue
        count("existing", "filled")
        for f in info:
            tally["filled_fields"][f] = tally["filled_fields"].get(f, 0) + 1
        updated.append(record)
    for raw in missing_raws:
        entry = index.get(usda.gtin_key(raw["gtin_upc"]))
        record, fill = None, fill_for(raw, entry)
        if fill:
            record, _ = refill(raw, entry)  # None if OFF's values would be implausible or inconsistent
        if record is None:
            record, fill = usda.normalize(raw), None
        if not record["name"]:
            count("missing", "no name")
        elif not record["ingredients"]["items"]:
            count("missing", "still no ingredients")
        elif record["nutrition"]["per_100g"]["energy_kcal"] is None:
            count("missing", "still no plausible nutrition")
        else:
            count("missing", "complete with OFF" if fill else "complete already")
            new.append(record)
    return updated, new, tally
