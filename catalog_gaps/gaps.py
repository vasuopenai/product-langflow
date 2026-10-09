"""Gap report and actions, computed from what the crawl stored (no Kroger calls).

Every Kroger food product with a real barcode is checked against four lists:
  in_usda    the full USDA Branded Foods file          (catalog.usda_codes)
  in_off     the full Open Food Facts export           (catalog.off_codes)
  in_off_us  ... the part OFF lists as sold in the US
  in_app     our app's product database                (products)

A group (below) can be reported, exported to CSV, or staged for research and review.
"""

import csv

from . import finder

GROUPS = {
    "not_in_usda": ("NOT in_usda", "Kroger products not in the USDA database"),
    "not_in_off": ("NOT in_off", "Kroger products not in Open Food Facts"),
    "not_in_off_us": ("NOT in_off_us", "Kroger products Open Food Facts doesn't list for the US"),
    "neither": ("NOT in_usda AND NOT in_off", "in neither USDA nor Open Food Facts"),
    "off_not_usda": ("in_off AND NOT in_usda", "in Open Food Facts but not USDA"),
    "not_in_app": ("NOT in_app", "not in our app's database"),
    "usda_not_app": ("in_usda AND NOT in_app", "in USDA but not in our app (skipped at load: incomplete label)"),
}


# The spellings the app's products table may use for a key (USDA GTINs zero-padded to 12-14
# digits, see mobile_api.barcodes.store_variants), so its primary-key index answers "in our app?".
_APP_VARIANTS = """ARRAY[CASE WHEN length(k) <= 12 THEN lpad(k, 12, '0') END,
                         CASE WHEN length(k) <= 13 THEN lpad(k, 13, '0') END,
                         CASE WHEN length(k) <= 14 THEN lpad(k, 14, '0') END, k]"""


def prepare(conn):
    """Temp table `flags`: one row per Kroger food product with its four memberships."""
    conn.execute("DROP TABLE IF EXISTS pg_temp.flags")
    conn.execute(f"""
        CREATE TEMP TABLE flags AS
        SELECT s.product_id, s.keys, s.brand, s.name, s.category, s.term, s.raw,
               EXISTS (SELECT 1 FROM catalog.usda_codes u WHERE u.code = ANY(s.keys)) AS in_usda,
               EXISTS (SELECT 1 FROM catalog.off_codes o WHERE o.code = ANY(s.keys)) AS in_off,
               EXISTS (SELECT 1 FROM catalog.off_codes o WHERE o.code = ANY(s.keys) AND o.us) AS in_off_us,
               EXISTS (SELECT 1 FROM unnest(s.keys) k JOIN products p ON p.code = ANY({_APP_VARIANTS})) AS in_app
        FROM catalog.seen s
        WHERE s.food AND s.keys IS NOT NULL AND s.status NOT IN ('store_code', 'no_barcode')""")


def _where(group=None, category=None, brand=None):
    """SQL over `flags` for a group (None = all products), optionally narrowed by category and brand."""
    if group is not None and group not in GROUPS:
        raise ValueError(f"unknown group {group!r}; one of: {', '.join(GROUPS)}")
    sql, params = (GROUPS[group][0] if group else "TRUE"), []
    if category:
        sql += " AND category ILIKE %s"
        params.append(f"%{category}%")
    if brand:
        sql += " AND brand ILIKE %s"
        params.append(f"%{brand}%")
    return sql, params


def report(conn, top=15):
    prepare(conn)
    refs = {name: {"rows": rows, "built_at": built.isoformat(timespec="minutes")}
            for name, rows, built in conn.execute("SELECT name, rows, built_at FROM catalog.refs")}
    excluded = dict(conn.execute("SELECT status, count(*) FROM catalog.seen "
                                 "WHERE status IN ('non_food', 'store_code', 'no_barcode') GROUP BY 1").fetchall())
    total = conn.execute("SELECT count(*) FROM flags").fetchone()[0]
    groups = {g: conn.execute(f"SELECT count(*) FROM flags WHERE {GROUPS[g][0]}").fetchone()[0] for g in GROUPS}
    matrix = {f"usda_{'yes' if u else 'no'}__off_{'yes' if o else 'no'}": n
              for u, o, n in conn.execute("SELECT in_usda, in_off, count(*) FROM flags GROUP BY 1, 2")}
    by_category = [dict(zip(("category", "products", "not_in_usda", "not_in_off", "neither"), r)) for r in conn.execute(
        """SELECT coalesce(category, '(none)'), count(*), count(*) FILTER (WHERE NOT in_usda),
                  count(*) FILTER (WHERE NOT in_off), count(*) FILTER (WHERE NOT in_usda AND NOT in_off)
           FROM flags GROUP BY 1 ORDER BY 3 DESC, 2 DESC LIMIT %s""", (top,))]
    brands = [dict(zip(("brand", "products", "not_in_usda"), r)) for r in conn.execute(
        """SELECT coalesce(brand, '(none)'), count(*), count(*) FILTER (WHERE NOT in_usda)
           FROM flags GROUP BY 1 HAVING count(*) FILTER (WHERE NOT in_usda) > 0
           ORDER BY 3 DESC, 2 DESC LIMIT %s""", (top,))]
    return {"refs": refs, "kroger_food_products": total, "groups": groups, "usda_x_off": matrix,
            "excluded": excluded, "by_category": by_category, "top_brands_not_in_usda": brands}


def format_report(r):
    pct = lambda n: f"{100 * n / r['kroger_food_products']:.0f}%" if r["kroger_food_products"] else "-"
    lines = [f"Kroger food products crawled: {r['kroger_food_products']:,}"
             + (f"  (excluded: {', '.join(f'{v:,} {k}' for k, v in r['excluded'].items())})" if r["excluded"] else "")]
    if not {"usda", "off"} <= set(r["refs"]):
        lines.append("WARNING: reference lists missing; run build-refs (USDA/OFF columns read 'not in' until then)")
    for g, (_, label) in GROUPS.items():
        lines.append(f"  {r['groups'][g]:>7,}  {pct(r['groups'][g]):>4}  {label}   [{g}]")
    m = r["usda_x_off"]
    lines += ["", "                 in OFF    not in OFF",
              f"  in USDA      {m.get('usda_yes__off_yes', 0):>8,}  {m.get('usda_yes__off_no', 0):>10,}",
              f"  not in USDA  {m.get('usda_no__off_yes', 0):>8,}  {m.get('usda_no__off_no', 0):>10,}",
              "", "By Kroger category (most missing from USDA first):"]
    for c in r["by_category"]:
        lines.append(f"  {c['category'][:28]:<28} {c['products']:>6,} products  {c['not_in_usda']:>6,} not in USDA"
                     f"  {c['neither']:>6,} in neither")
    lines += ["", "Brands with the most products missing from USDA:"]
    lines += [f"  {b['brand'][:28]:<28} {b['not_in_usda']:>5,} of {b['products']:,}" for b in r["top_brands_not_in_usda"]]
    return "\n".join(lines)


def products(conn, group=None, category=None, brand=None, q=None, limit=100, offset=0):
    """A page of Kroger food products with their memberships, for the UI. Returns {"total", "items"}."""
    from kroger_sync.client import parse_product
    prepare(conn)
    where, params = _where(group, category, brand)
    if q:
        where += " AND (name ILIKE %s OR brand ILIKE %s OR %s = ANY(keys))"
        params += [f"%{q}%", f"%{q}%", q.lstrip("0")]
    total = conn.execute(f"SELECT count(*) FROM flags WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT f.keys[1], f.brand, f.name, f.category, f.term, f.in_usda, f.in_off, f.in_off_us, f.in_app,
                   f.raw, sp.status
            FROM flags f LEFT JOIN staging.scanned_products sp ON sp.barcode = ANY(f.keys)
            WHERE {where} ORDER BY f.category, f.brand, f.name LIMIT %s OFFSET %s""",
        params + [limit, offset]).fetchall()
    items = []
    for code, brand_, name, category_, term, in_usda, in_off, in_off_us, in_app, raw, staged in rows:
        p = parse_product(raw) if raw else {}
        items.append({"barcode": code, "brand": brand_, "name": name, "category": category_, "term": term,
                      "size": p.get("size"), "image_url": p.get("image_url"), "in_usda": in_usda, "in_off": in_off,
                      "in_off_us": in_off_us, "in_app": in_app, "review_status": staged})
    return {"total": total, "items": items}


def categories(conn):
    return [{"category": c, "products": n} for c, n in conn.execute(
        "SELECT coalesce(category, '(none)'), count(*) FROM catalog.seen WHERE food GROUP BY 1 ORDER BY 2 DESC")]


def export_csv(conn, path, group=None, category=None, brand=None):
    prepare(conn)
    where, params = _where(group, category, brand)
    rows = conn.execute(f"""SELECT keys[1], brand, name, category, term, in_usda, in_off, in_off_us, in_app
                            FROM flags WHERE {where} ORDER BY category, brand, name""", params).fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["barcode", "brand", "name", "kroger_category", "search_term", "in_usda", "in_off",
                    "in_off_us", "in_app"])
        w.writerows(rows)
    return len(rows)


def stage_group(conn, group, category=None, brand=None, limit=None, dry_run=False):
    """Put a group into staging (status queued, found_via kroger_catalog) from the stored Kroger
    records. Products already staged (by a shopper's scan or earlier) are left alone."""
    prepare(conn)
    where, params = _where(group, category, brand)
    rows = conn.execute(
        f"""SELECT keys, raw, brand, name, category FROM flags
            WHERE {where} AND NOT EXISTS (SELECT 1 FROM staging.scanned_products sp WHERE sp.barcode = ANY(keys))
            ORDER BY category, brand, name {'LIMIT %s' if limit else ''}""",
        params + ([limit] if limit else [])).fetchall()
    if dry_run:
        return {"would_stage": len(rows)}
    staged = 0
    for keys, raw, brand_, name, category_ in rows:
        product = raw or {"upc": keys[0], "brand": brand_, "description": name, "categories": [category_]}
        staged += finder.stage(conn, keys[0], product)
    conn.commit()
    return {"staged": staged}
