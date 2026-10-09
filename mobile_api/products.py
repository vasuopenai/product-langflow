"""Product store lookups for the app (read-only)."""

import json

from off_products.ask import _facts

from .barcodes import store_variants


def _record(rec):
    return rec if isinstance(rec, dict) else json.loads(rec)


def card(rec):
    """What the app shows for a product: the answer facts plus image, category and
    allergens, and where the data came from."""
    return {
        **_facts(rec),
        "image_url": rec.get("image_url"),
        "category": rec.get("main_category"),
        "categories": rec.get("categories") or [],
        "allergens": rec.get("allergens") or [],
        "origin": (rec.get("source") or {}).get("origin") or "usda",
    }


def find(conn, barcode):
    """The product record for a scanned barcode, or None."""
    variants = store_variants(barcode)
    if not variants:
        return None
    row = conn.execute("SELECT record FROM products WHERE code = ANY(%s) LIMIT 1", (variants,)).fetchone()
    return _record(row[0]) if row else None


def records(conn, codes):
    """code -> record, for a list of product codes."""
    if not codes:
        return {}
    return {c: _record(r) for c, r in conn.execute(
        "SELECT code, record FROM products WHERE code = ANY(%s)", (list(codes),))}
