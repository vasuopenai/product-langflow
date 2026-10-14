"""Product store lookups for the app (read-only)."""

import json

from off_products.ask import _facts
from off_products.timing import step

from .barcodes import store_variants


def _record(rec):
    return rec if isinstance(rec, dict) else json.loads(rec)


def _with_off_grade(rec, grade):
    """Open Food Facts' published Nutri-Score (nutriscore_off table) into the record, where cards
    look for it; a grade already in the record (from a USDA load with OFF extras) is kept."""
    if grade:
        rec.setdefault("scores", {})
        rec["scores"]["nutriscore_off"] = rec["scores"].get("nutriscore_off") or grade
    return rec


def card(rec):
    """What the app shows for a product: the answer facts plus image, category and
    allergens, and where the data came from."""
    return {
        **_facts(rec),
        "image_url": rec.get("image_url"),
        "category": rec.get("main_category"),
        "categories": rec.get("categories") or [],
        "allergens": rec.get("allergens") or [],
        "origin": origin(rec),
    }


def origin(rec):
    """Where a product's data came from: "usda" (the USDA load), "scan" or "kroger" (approved
    from the review queue), or "open_food_facts" (records loaded from OFF have no source block)."""
    source = rec.get("source")
    if not source:
        return "open_food_facts"
    return source.get("origin") or "usda"


def find(conn, barcode):
    """The product record for a scanned barcode, or None."""
    variants = store_variants(barcode)
    if not variants:
        return None
    with step("lookup"):
        row = conn.execute("SELECT p.record, o.grade FROM products p LEFT JOIN nutriscore_off o ON o.code = p.code "
                           "WHERE p.code = ANY(%s) LIMIT 1", (variants,)).fetchone()
    return _with_off_grade(_record(row[0]), row[1]) if row else None


def records(conn, codes):
    """code -> record, for a list of product codes."""
    if not codes:
        return {}
    return {c: _with_off_grade(_record(r), g) for c, r, g in conn.execute(
        "SELECT p.code, p.record, o.grade FROM products p LEFT JOIN nutriscore_off o ON o.code = p.code "
        "WHERE p.code = ANY(%s)", (list(codes),))}
