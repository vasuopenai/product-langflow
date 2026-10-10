"""Compare our Nutri-Score (computed from the USDA records in Postgres) with the
grade Open Food Facts publishes for the same barcode.

    python -m nutriscore validate                  # all matched products
    python -m nutriscore validate --sample 20000   # random sample
    python -m nutriscore validate --json report.json --examples 20

Read-only on the products database. For every disagreement it re-scores the
product with one of OFF's inputs swapped in (its category variant, its nutrient
values, its fiber, its fruit/vegetable/legume estimate, its sweetener flag) to name
the input that explains the difference.
"""

import collections
import json
import random
import time

from . import algorithm as alg
from . import off
from .usda import KCAL_TO_KJ, compute

GRADES = "abcde"

# Only the parts of the record compute() reads, so 400k rows don't ship whole records.
_SLIM_SQL = """
SELECT code, jsonb_build_object(
  'code', code,
  'name', record->'name',
  'main_category', record->'main_category',
  'categories', record->'categories',
  'nutrition', jsonb_build_object('per_100g', record->'nutrition'->'per_100g',
                                  'implausible', record->'nutrition'->'implausible'),
  'ingredients', jsonb_build_object('items', record->'ingredients'->'items'),
  'derived', jsonb_build_object('groups', jsonb_build_object(
      'artificial_sweeteners', record->'derived'->'groups'->'artificial_sweeteners')),
  'source', jsonb_build_object('raw', jsonb_build_object('nutrients', jsonb_build_object(
      '1018', record->'source'->'raw'->'nutrients'->'1018'))))
FROM products WHERE code = ANY(%s)
"""


def fetch_records(conn, codes, batch=5000):
    for i in range(0, len(codes), batch):
        yield from conn.execute(_SLIM_SQL, (codes[i:i + batch],)).fetchall()


# --- OFF's own inputs, for the counterfactuals ---------------------------------------

def off_variant(row):
    """Approximation of OFF's is_beverage/is_water/is_cheese/is_fat_oil_nuts_seeds
    from its category tags (OFF also uses HS codes from its taxonomy for nuts/seeds
    and red meat, which the export does not carry)."""
    cats = set(row.get("categories_tags") or [])
    if "en:spring-waters" in cats and not cats & {"en:flavored-waters", "en:flavoured-waters"}:
        return alg.WATER
    beverage = bool(cats & {"en:beverages", "en:beverage-preparations"}) and not cats & {
        "en:meal-replacement", "en:soups"}
    if cats & {"en:milks", "en:plant-based-milk-alternatives", "en:dairy-drinks", "en:plant-based-beverages",
               "en:tea-based-beverages", "en:iced-teas", "en:herbal-tea-beverages", "en:coffee-beverages",
               "en:coffee-drinks", "en:coffees", "en:herbal-teas", "en:teas"}:
        beverage = True
    if beverage:
        return alg.BEVERAGE
    if "en:cheeses" in cats:
        return alg.CHEESE
    if "en:chestnuts" not in cats and cats & {"en:fats", "en:creams", "en:seeds", "en:nuts", "en:peanuts",
                                               "en:nut-butters", "en:peanut-butters", "en:nuts-and-their-products"}:
        return alg.FATS
    return alg.GENERAL


def off_inputs(row):
    n = row.get("nutriments") or {}
    energy = n.get("energy-kj")
    if energy is None and n.get("energy") is not None:
        energy = n["energy"]  # OFF's "energy" is kJ
    if energy is None and n.get("energy-kcal") is not None:
        energy = n["energy-kcal"] * KCAL_TO_KJ
    salt = n.get("salt")
    if salt is None and n.get("sodium") is not None:
        salt = n["sodium"] * 2.5
    sat, sugars = n.get("saturated-fat"), n.get("sugars")
    if sat is None and n.get("fat") == 0:
        sat = 0
    if sugars is None and n.get("carbohydrates") == 0:
        sugars = 0
    fvl = n.get("fruits-vegetables-legumes")
    if fvl is None:
        fvl = n.get("fruits-vegetables-legumes-estimate-from-ingredients")
    return {"energy_kj": energy, "sugars_g": sugars, "saturated_fat_g": sat, "fat_g": n.get("fat"),
            "salt_g": salt, "fiber_g": n.get("fiber"), "proteins_g": n.get("proteins"),
            "fruits_vegetables_legumes_pct": fvl,
            "non_nutritive_sweeteners": bool(row.get("with_non_nutritive_sweeteners"))}


def our_inputs(res):
    c = res["components"]

    def val(name):
        return c[name]["value"] if name in c else None

    sat = val("saturated_fat")
    if sat is None and "energy_from_saturated_fat" in c and c["energy_from_saturated_fat"]["value"] is not None:
        sat = c["energy_from_saturated_fat"]["value"] / alg.SATURATED_FAT_KJ_PER_G
    return {"energy_kj": val("energy"), "sugars_g": val("sugars"), "saturated_fat_g": sat,
            "fat_g": res.get("_fat_g"), "salt_g": val("salt"), "fiber_g": val("fiber"),
            "proteins_g": val("proteins"), "fruits_vegetables_legumes_pct": val("fruits_vegetables_legumes"),
            "non_nutritive_sweeteners": bool(val("non_nutritive_sweeteners"))}


def _grade(variant, inputs, red_meat):
    if variant == alg.GENERAL and red_meat:
        variant = alg.RED_MEAT
    try:
        return alg.score(variant, **inputs)["grade"]
    except TypeError:  # an input is None where a number is needed
        return None


def explain(res, row, record):
    """The first single OFF input that, swapped into our computation, reproduces
    OFF's grade; else "several inputs" / "unexplained"."""
    target = row["nutriscore_grade"]
    ours = our_inputs(res)
    ours["fat_g"] = (record["nutrition"]["per_100g"] or {}).get("fat_g")
    theirs = off_inputs(row)
    var = res["category_variant"]
    red = var == alg.RED_MEAT
    base_var = alg.GENERAL if red else var
    ov = off_variant(row)

    if ov != base_var and _grade(ov, ours, red and ov == alg.GENERAL) == target:
        return "category variant"
    nutrient_keys = ["energy_kj", "sugars_g", "saturated_fat_g", "fat_g", "salt_g", "proteins_g"]
    if ours["fiber_g"] is None and theirs["fiber_g"] is not None:
        if _grade(base_var, {**ours, "fiber_g": theirs["fiber_g"]}, red) == target:
            return "fiber missing in USDA"
    if all(theirs[k] is not None for k in nutrient_keys):
        swapped = {**ours, **{k: theirs[k] for k in nutrient_keys + ["fiber_g"]}}
        if _grade(base_var, swapped, red) == target:
            return "nutrient values differ"
    if _grade(base_var, {**ours, "fruits_vegetables_legumes_pct": theirs["fruits_vegetables_legumes_pct"]},
              red) == target:
        return "fruit/veg/legume estimate"
    if var == alg.BEVERAGE and ours["non_nutritive_sweeteners"] != theirs["non_nutritive_sweeteners"]:
        if _grade(base_var, {**ours, "non_nutritive_sweeteners": theirs["non_nutritive_sweeteners"]},
                  red) == target:
            return "non-nutritive sweetener detection"
    if red and _grade(alg.GENERAL, ours, False) == target:
        return "red meat protein cap"
    if not red and base_var == alg.GENERAL and _grade(alg.RED_MEAT, ours, False) == target:
        return "red meat protein cap"
    if all(theirs[k] is not None for k in nutrient_keys) and _grade(ov, theirs, red) == target:
        return "several inputs differ"
    return "unexplained"


# --- the run ---------------------------------------------------------------------------

def run(conn, parquet_path, sample=None, seed=0, examples=10, progress=print, off_rows=None):
    t0 = time.time()
    codes = [r[0] for r in conn.execute("SELECT code FROM products").fetchall()]
    by_key = {off.key(c): c for c in codes}
    if off_rows is None:
        off_rows = off.load(parquet_path, set(by_key), progress=progress)
    progress(f"{len(codes)} USDA products, {len(off_rows)} found in OFF ({time.time() - t0:.0f}s)")

    matched = sorted(by_key[k] for k in off_rows)
    if sample and sample < len(matched):
        matched = sorted(random.Random(seed).sample(matched, sample))

    status = collections.Counter()  # (ours, OFF) computed / not computed
    reasons_not_computed = collections.Counter()
    reasons_only_off = collections.Counter()
    confusion = collections.Counter()
    by_variant = collections.defaultdict(collections.Counter)
    causes = collections.Counter()
    causes_by_variant = collections.defaultdict(collections.Counter)
    score_diffs = collections.Counter()
    off_inputs_check = collections.Counter()
    same_variant = collections.defaultdict(collections.Counter)
    vs_2021 = collections.Counter()
    samples = collections.defaultdict(list)
    n = 0
    for code, record in fetch_records(conn, matched):
        n += 1
        row = off_rows[off.key(code)]
        res = compute(record)
        og = row.get("nutriscore_grade")
        off_has = og in tuple(GRADES)
        we_have = res["grade"] is not None
        status[(we_have, off_has)] += 1
        if not we_have:
            reason = (res.get("status"), res["reasons"][0] if res["reasons"] else "")
            reasons_not_computed[reason] += 1
            if off_has:
                reasons_only_off[reason] += 1
        if not (we_have and off_has):
            continue
        var = res["category_variant"]
        confusion[(res["grade"], og)] += 1
        agree = res["grade"] == og
        by_variant[var]["n"] += 1
        by_variant[var]["agree"] += agree
        by_variant[var]["within_one"] += abs(GRADES.index(res["grade"]) - GRADES.index(og)) <= 1
        if row.get("grade_2021"):  # sanity check that we are on the 2023 algorithm, like OFF's current grade
            vs_2021["n"] += 1
            vs_2021["agree"] += res["grade"] == row["grade_2021"]
        if row.get("nutriscore_score") is not None:
            score_diffs[max(-10, min(10, res["score"] - row["nutriscore_score"]))] += 1
        # Algorithm fidelity: OFF's own inputs through our algorithm vs OFF's score.
        theirs = off_inputs(row)
        ov = off_variant(row)
        # Same variant on both sides (judged from OFF's category tags): isolates the
        # nutrient and ingredient inputs from categorisation differences.
        if ov == (alg.GENERAL if var == alg.RED_MEAT else var):
            same_variant[var]["n"] += 1
            same_variant[var]["agree"] += agree
        if row.get("nutriscore_score") is not None and (ov == alg.WATER or all(
                theirs[k] is not None for k in ("energy_kj", "sugars_g", "saturated_fat_g", "salt_g", "proteins_g"))):
            s = alg.score(ov if not (var == alg.RED_MEAT and ov == alg.GENERAL) else alg.RED_MEAT, **theirs)
            off_inputs_check["n"] += 1
            off_inputs_check["grade_match"] += s["grade"] == og
            off_inputs_check["score_match"] += s["score"] == row["nutriscore_score"]
        if not agree:
            cause = explain(res, row, record)
            causes[cause] += 1
            causes_by_variant[var][cause] += 1
            if len(samples[cause]) < examples:
                samples[cause].append({
                    "code": code, "name": record.get("name"), "usda_category": record.get("main_category"),
                    "variant": var, "ours": f"{res['grade']} ({res['score']})",
                    "off": f"{og} ({row.get('nutriscore_score')})", "off_categories": (row.get("categories_tags") or [])[-3:],
                    "points": {k: v["points"] for k, v in res["components"].items()},
                })
        if n % 50000 == 0:
            progress(f"  scored {n} ({time.time() - t0:.0f}s)")

    both = sum(confusion.values())
    agree = sum(v for (a, b), v in confusion.items() if a == b)
    within = sum(v for (a, b), v in confusion.items() if abs(GRADES.index(a) - GRADES.index(b)) <= 1)
    report = {
        "usda_products": len(codes),
        "found_in_off": len(off_rows),
        "evaluated": n,
        "sampled": bool(sample and sample < len(off_rows)),
        "coverage": {
            "both_graded": status[(True, True)],
            "only_ours": status[(True, False)],
            "only_off": status[(False, True)],
            "neither": status[(False, False)],
        },
        "agreement": {"n": both, "exact": agree, "exact_pct": _pct(agree, both),
                      "within_one_grade_pct": _pct(within, both)},
        "by_variant": {v: {"n": c["n"], "exact_pct": _pct(c["agree"], c["n"]),
                           "within_one_grade_pct": _pct(c["within_one"], c["n"])}
                       for v, c in sorted(by_variant.items(), key=lambda x: -x[1]["n"])},
        "same_variant_as_off": {
            "n": sum(c["n"] for c in same_variant.values()),
            "exact_pct": _pct(sum(c["agree"] for c in same_variant.values()),
                              sum(c["n"] for c in same_variant.values())),
            "by_variant": {v: {"n": c["n"], "exact_pct": _pct(c["agree"], c["n"])}
                           for v, c in sorted(same_variant.items(), key=lambda x: -x[1]["n"])}},
        "agreement_with_off_2021_grade": {"n": vs_2021["n"], "exact_pct": _pct(vs_2021["agree"], vs_2021["n"])},
        "confusion_ours_vs_off": {a: {b: confusion[(a, b)] for b in GRADES} for a in GRADES},
        "score_difference_ours_minus_off": {str(k): score_diffs[k] for k in sorted(score_diffs)},
        "disagreement_causes": dict(causes.most_common()),
        "disagreement_causes_by_variant": {v: dict(c.most_common()) for v, c in causes_by_variant.items()},
        "not_computed_by_us": [{"status": s, "reason": r, "n": c}
                               for (s, r), c in reasons_not_computed.most_common(25)],
        "not_computed_by_us_but_graded_by_off": [{"status": s, "reason": r, "n": c}
                                                 for (s, r), c in reasons_only_off.most_common(25)],
        "algorithm_check_with_off_inputs": {
            "n": off_inputs_check["n"],
            "grade_match_pct": _pct(off_inputs_check["grade_match"], off_inputs_check["n"]),
            "score_match_pct": _pct(off_inputs_check["score_match"], off_inputs_check["n"])},
        "examples": samples,
        "seconds": round(time.time() - t0),
    }
    return report


def _pct(a, b):
    return round(100 * a / b, 1) if b else None


def print_report(r):
    print(f"\nUSDA products: {r['usda_products']}   found in OFF by barcode: {r['found_in_off']}   "
          f"evaluated: {r['evaluated']}{' (sample)' if r['sampled'] else ''}")
    c = r["coverage"]
    print(f"graded by both: {c['both_graded']}   only us: {c['only_ours']}   only OFF: {c['only_off']}   "
          f"neither: {c['neither']}")
    a = r["agreement"]
    print(f"\nAgreement on {a['n']} products graded by both: {a['exact_pct']}% exact, "
          f"{a['within_one_grade_pct']}% within one grade")
    print("\nBy variant (ours):")
    for v, s in r["by_variant"].items():
        print(f"  {v:22} n={s['n']:7}  exact {s['exact_pct']}%  within one {s['within_one_grade_pct']}%")
    v21 = r["agreement_with_off_2021_grade"]
    print(f"(For contrast, vs OFF's old 2021-algorithm grade, n={v21['n']}: {v21['exact_pct']}% exact)")
    sv = r["same_variant_as_off"]
    print(f"\nWhere OFF's categories imply the same variant as ours: n={sv['n']}  exact {sv['exact_pct']}%")
    for v, s in sv["by_variant"].items():
        print(f"  {v:22} n={s['n']:7}  exact {s['exact_pct']}%")
    print("\nConfusion (rows = ours, columns = OFF):")
    print("      " + "".join(f"{g:>8}" for g in GRADES))
    for g, row in r["confusion_ours_vs_off"].items():
        print(f"  {g}   " + "".join(f"{row[h]:8}" for h in GRADES))
    print("\nDisagreement causes (first single OFF input that reproduces OFF's grade):")
    for k, v in r["disagreement_causes"].items():
        print(f"  {v:7}  {k}")
    print("\nNot computed by us (top reasons):")
    for x in r["not_computed_by_us"][:12]:
        print(f"  {x['n']:7}  {x['status']}: {x['reason']}")
    print("\n...of which OFF did publish a grade:")
    for x in r["not_computed_by_us_but_graded_by_off"][:12]:
        print(f"  {x['n']:7}  {x['status']}: {x['reason']}")
    ac = r["algorithm_check_with_off_inputs"]
    print(f"\nAlgorithm check, OFF's own inputs through our code: n={ac['n']}  grade match {ac['grade_match_pct']}%  "
          f"score match {ac['score_match_pct']}%")
    print(f"\n{r['seconds']}s")


def main(argv=None):
    import argparse
    import os

    import psycopg

    ap = argparse.ArgumentParser(prog="nutriscore validate")
    ap.add_argument("--db", default=os.environ.get("DATABASE_URL",
                                                   "postgresql://postgres:postgres@127.0.0.1:5432/food"))
    ap.add_argument("--parquet", default="data/food.parquet")
    ap.add_argument("--sample", type=int, default=None, help="score a random sample of matched products")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--examples", type=int, default=10, help="examples kept per disagreement cause")
    ap.add_argument("--json", help="write the full report (with examples) here")
    ap.add_argument("--off-cache", help="pickle of OFF matches to reuse between runs (created if missing)")
    args = ap.parse_args(argv)

    off_rows = None
    if args.off_cache and os.path.exists(args.off_cache):
        import pickle
        with open(args.off_cache, "rb") as f:
            off_rows = pickle.load(f)
    with psycopg.connect(args.db) as conn:
        conn.read_only = True
        if off_rows is None and args.off_cache:
            import pickle
            codes = [r[0] for r in conn.execute("SELECT code FROM products").fetchall()]
            off_rows = off.load(args.parquet, {off.key(c) for c in codes}, progress=print)
            with open(args.off_cache, "wb") as f:
                pickle.dump(off_rows, f)
        report = run(conn, args.parquet, sample=args.sample, seed=args.seed, examples=args.examples,
                     off_rows=off_rows)
    print_report(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1, ensure_ascii=False, default=str)
