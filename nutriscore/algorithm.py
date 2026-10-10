"""The Nutri-Score 2023 algorithm on already-prepared inputs (no product mapping here).

Tables and rules follow the Scientific Committee's update reports (foods, June 2022;
beverages, March 2023) and Open Food Facts' implementation
(openfoodfacts-server, lib/ProductOpener/Nutriscore.pm, compute_nutriscore_score_2023):

- a value scores one point for every threshold it is strictly greater than
  (">"); only the saturated-fat/fat ratio uses ">=";
- no rounding of input values;
- final score = negative points - positive points, protein counted only for
  beverages, cheese, fats/oils/nuts/seeds with negative < 7, other foods with
  negative < 11; red meat products get at most 2 protein points.
"""

GENERAL = "general"
CHEESE = "cheese"
RED_MEAT = "red_meat"
FATS = "fats_oils_nuts_seeds"
BEVERAGE = "beverage"
WATER = "water"
VARIANTS = (GENERAL, CHEESE, RED_MEAT, FATS, BEVERAGE, WATER)

# Upper bounds of each point band: value > thresholds[i] earns point i + 1.
THRESHOLDS = {
    # negative ("A") components, per 100 g / 100 ml
    "energy": [335, 670, 1005, 1340, 1675, 2010, 2345, 2680, 3015, 3350],  # kJ
    "energy_beverages": [30, 90, 150, 210, 240, 270, 300, 330, 360, 390],  # kJ
    "energy_from_saturated_fat": [120, 240, 360, 480, 600, 720, 840, 960, 1080, 1200],  # kJ (fats)
    "sugars": [3.4, 6.8, 10, 14, 17, 20, 24, 27, 31, 34, 37, 41, 44, 48, 51],  # g
    "sugars_beverages": [0.5, 2, 3.5, 5, 6, 7, 8, 9, 10, 11],  # g
    "saturated_fat": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],  # g
    "saturated_fat_ratio": [10, 16, 22, 28, 34, 40, 46, 52, 58, 64],  # % of fat (fats), uses >=
    "salt": [0.2, 0.4, 0.6, 0.8, 1, 1.2, 1.4, 1.6, 1.8, 2, 2.2, 2.4, 2.6, 2.8, 3, 3.2, 3.4, 3.6, 3.8, 4],  # g
    # positive ("C") components
    "proteins": [2.4, 4.8, 7.2, 9.6, 12, 14, 17],  # g
    "proteins_beverages": [1.2, 1.5, 1.8, 2.1, 2.4, 2.7, 3.0],  # g
    "fiber": [3.0, 4.1, 5.2, 6.3, 7.4],  # g (AOAC)
    # repeated thresholds make the jumps: foods 1/2/5 points, beverages 2/4/6
    "fruits_vegetables_legumes": [40, 60, 80, 80, 80],  # %
    "fruits_vegetables_legumes_beverages": [40, 40, 60, 60, 80, 80],  # %
}
NON_NUTRITIVE_SWEETENERS_POINTS = 4  # beverages only, presence/absence
SATURATED_FAT_KJ_PER_G = 37  # energy from saturates = saturated fat (g) x 37

# Highest score for each grade, checked in order; anything above the last is E.
GRADE_BOUNDS = {
    "foods": [("a", 0), ("b", 2), ("c", 10), ("d", 18)],
    FATS: [("a", -6), ("b", 2), ("c", 10), ("d", 18)],
    BEVERAGE: [("b", 2), ("c", 6), ("d", 9)],  # only water is A
}


def points(component, value, beverage=False):
    """Points for one component value (None counts as 0 points)."""
    key = component + "_beverages" if beverage and component + "_beverages" in THRESHOLDS else component
    table = THRESHOLDS[key]
    if value is None:
        return 0, len(table)
    if component == "saturated_fat_ratio":
        return sum(value >= t for t in table), len(table)
    return sum(value > t for t in table), len(table)


def grade(score, variant):
    if variant == WATER:
        return "a"
    bounds = GRADE_BOUNDS.get(variant, GRADE_BOUNDS["foods"])
    for letter, upper in bounds:
        if score <= upper:
            return letter
    return "e"


def score(variant, energy_kj=None, sugars_g=None, saturated_fat_g=None, fat_g=None, salt_g=None,
          fiber_g=None, proteins_g=None, fruits_vegetables_legumes_pct=None,
          non_nutritive_sweeteners=False, red_meat=False):
    """Score one product. ``variant`` is one of VARIANTS; ``red_meat`` caps protein
    points at 2 (the RED_MEAT variant implies it). Returns
    {"score", "grade", "components", "negative_points", "positive_points", "count_proteins"}."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    if variant == WATER:
        return {"score": 0, "grade": "a", "components": {}, "negative_points": 0,
                "positive_points": 0, "count_proteins": None}
    beverage = variant == BEVERAGE
    fats = variant == FATS
    red_meat = red_meat or variant == RED_MEAT

    comps = {}

    def add(name, value, unit, kind, component=None):
        pts, mx = points(component or name, value, beverage)
        comps[name] = {"value": value, "unit": unit, "points": pts, "max": mx, "kind": kind}

    if fats:
        e_sat = saturated_fat_g * SATURATED_FAT_KJ_PER_G if saturated_fat_g is not None else None
        add("energy_from_saturated_fat", e_sat, "kJ", "negative")
    else:
        add("energy", energy_kj, "kJ", "negative")
    add("sugars", sugars_g, "g", "negative")
    if fats:
        add("saturated_fat_ratio", saturated_fat_ratio(saturated_fat_g, fat_g), "%", "negative")
    else:
        add("saturated_fat", saturated_fat_g, "g", "negative")
    add("salt", salt_g, "g", "negative")
    if beverage:
        comps["non_nutritive_sweeteners"] = {
            "value": bool(non_nutritive_sweeteners), "unit": "presence", "kind": "negative",
            "points": NON_NUTRITIVE_SWEETENERS_POINTS if non_nutritive_sweeteners else 0,
            "max": NON_NUTRITIVE_SWEETENERS_POINTS}
    negative = sum(c["points"] for c in comps.values() if c["kind"] == "negative")

    add("proteins", proteins_g, "g", "positive")
    if red_meat and comps["proteins"]["points"] > 2:
        comps["proteins"]["points"] = 2
        comps["proteins"]["capped"] = "red_meat_product"
    add("fiber", fiber_g, "g", "positive")
    add("fruits_vegetables_legumes", fruits_vegetables_legumes_pct, "%", "positive")

    if beverage or variant == CHEESE:
        count_proteins = True
    elif fats:
        count_proteins = negative < 7
    else:
        count_proteins = negative < 11
    comps["proteins"]["counted"] = count_proteins
    positive = comps["fiber"]["points"] + comps["fruits_vegetables_legumes"]["points"]
    if count_proteins:
        positive += comps["proteins"]["points"]

    total = negative - positive
    return {"score": total, "grade": grade(total, variant), "components": comps,
            "negative_points": negative, "positive_points": positive, "count_proteins": count_proteins}


def saturated_fat_ratio(saturated_fat_g, fat_g):
    """Saturated fat as % of total fat, as OFF computes it (0 when there is no saturated fat)."""
    if saturated_fat_g is None or saturated_fat_g <= 0:
        return 0.0 if saturated_fat_g is not None else None
    if fat_g is None or fat_g <= 0:
        fat_g = saturated_fat_g
    return round(saturated_fat_g / fat_g * 100, 1)  # OFF keeps one decimal
