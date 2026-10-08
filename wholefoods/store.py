"""Find and list Whole Foods Market brand products in the product store.

The set of barcodes is computed once (``refresh``) into wholefoods.products, so
listing doesn't scan every product's record. Only the product store's tables are
read; nothing outside the ``wholefoods`` schema is written.
"""

# USDA brand owner spellings of Whole Foods Market ("Whole Foods Market, Inc.",
# "WHOLE FOODS MARKETS", "365 by Whole Foods Market Services", ...), but not other
# companies with "whole foods" in their name ("Franco Whole Foods, LLC").
OWNER_PATTERN = r"^(365 (by )?)?whole foods( markets?)?( services| - pgc|,? inc\.?)?$"

SCHEMA = [
    "CREATE SCHEMA IF NOT EXISTS wholefoods",
    "CREATE TABLE IF NOT EXISTS wholefoods.products (code TEXT PRIMARY KEY, brand_owner TEXT)",
]

_NUTRIENTS = ("energy_kcal", "protein_g", "fat_g", "carbs_g", "sugars_g", "added_sugars_g",
              "fiber_g", "sodium_mg")


def init_schema(conn):
    for sql in SCHEMA:
        conn.execute(sql)
    conn.commit()


def refresh(conn):
    """Recompute which products are Whole Foods brand. Returns the count."""
    init_schema(conn)
    conn.execute("DELETE FROM wholefoods.products")
    conn.execute(
        """INSERT INTO wholefoods.products
           SELECT code, record->'source'->'raw'->>'brand_owner' FROM products
           WHERE trim(record->'source'->'raw'->>'brand_owner') ~* %s""", (OWNER_PATTERN,))
    conn.commit()
    return conn.execute("SELECT count(*) FROM wholefoods.products").fetchone()[0]


def _round(name, v):
    if v is None:
        return None
    return round(v) if name in ("energy_kcal", "sodium_mg") else round(v, 1)


def _row(rec):
    nut = rec.get("nutrition") or {}
    serv = nut.get("per_serving") or {}
    groups = (rec.get("derived") or {}).get("groups") or {}
    ing = rec.get("ingredients") or {}
    return {
        "code": rec["code"], "name": rec.get("name"), "brand": rec.get("brand"),
        "category": rec.get("main_category"), "categories": rec.get("categories") or [],
        "serving_size": nut.get("serving_size"),
        "per_serving": {k: _round(k, serv.get(k)) for k in _NUTRIENTS if serv.get(k) is not None},
        "ingredients": (ing.get("text") or "")[:600], "ingredient_count": ing.get("count_total"),
        "free_of": [g for g, v in groups.items() if v.get("status") == "none"],
        "labels": rec.get("labels") or [], "allergens": rec.get("allergens") or [],
        "image_url": rec.get("image_url"), "url": rec.get("url"),
    }


def _where(q=None, category=None, brand=None):
    where, params = ["TRUE"], []
    if q:
        where.append("(p.name ILIKE %s OR p.record->'ingredients'->>'text' ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if category:
        where.append("EXISTS (SELECT 1 FROM product_tags t WHERE t.code = p.code "
                     "AND t.kind = 'category' AND t.tag = %s)")
        params.append(category)
    if brand:
        where.append("p.brand = %s")
        params.append(brand)
    return " AND ".join(where), params


def list_products(conn, q=None, category=None, brand=None, limit=50, offset=0):
    """A page of Whole Foods brand products, by name, with the total match count."""
    where, params = _where(q, category, brand)
    base = f"FROM wholefoods.products w JOIN products p ON p.code = w.code WHERE {where}"
    total = conn.execute(f"SELECT count(*) {base}", params).fetchone()[0]
    rows = conn.execute(f"SELECT p.record {base} ORDER BY p.name, p.code LIMIT %s OFFSET %s",
                        params + [min(int(limit), 200), max(int(offset), 0)]).fetchall()
    return {"total": total, "offset": offset, "products": [_row(r) for (r,) in rows]}


def facets(conn):
    """Brands and categories with counts, for the UI's filters."""
    brands = conn.execute(
        "SELECT p.brand, count(*) FROM wholefoods.products w JOIN products p USING (code) "
        "GROUP BY 1 ORDER BY 2 DESC").fetchall()
    cats = conn.execute(
        "SELECT t.tag, count(*) FROM wholefoods.products w JOIN product_tags t "
        "ON t.code = w.code AND t.kind = 'category' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    return {"brands": [{"brand": b, "count": n} for b, n in brands if b],
            "categories": [{"category": c, "count": n} for c, n in cats]}
