"""Nutri-Score 2023: worked examples for every variant, then our USDA record mapping.

Sources of the expected values:
- OFF unit tests: openfoodfacts-server tests/unit/expected_test_results/nutriscore/<name>.json
  (inputs and points as published there; the "nutriscore.2023" section).
- OFF products: real products from the Open Food Facts export (data/food.parquet) whose
  published 2023 nutriscore_score is reproduced from their nutriments.
- Thresholds: Scientific Committee update reports (foods 2022, beverages 2023), appendix tables.
"""

import pytest

from nutriscore import algorithm as alg
from nutriscore.usda import compute, fvl_percent, is_fvl, is_red_meat, reestimate, scope, variant


def pts(res):
    return {k: v["points"] for k, v in res["components"].items()}


# --- point tables ----------------------------------------------------------------

@pytest.mark.parametrize("component,value,beverage,expected", [
    ("energy", 335, False, 0), ("energy", 336, False, 1), ("energy", 3351, False, 10),
    ("sugars", 3.4, False, 0), ("sugars", 3.5, False, 1), ("sugars", 51.1, False, 15),
    ("salt", 0.2, False, 0), ("salt", 0.21, False, 1), ("salt", 4.01, False, 20),
    ("saturated_fat", 10.5, False, 10),
    ("proteins", 2.4, False, 0), ("proteins", 17.1, False, 7),
    ("fiber", 3.0, False, 0), ("fiber", 7.5, False, 5),
    ("fruits_vegetables_legumes", 40, False, 0), ("fruits_vegetables_legumes", 41, False, 1),
    ("fruits_vegetables_legumes", 61, False, 2), ("fruits_vegetables_legumes", 81, False, 5),
    ("energy", 30, True, 0), ("energy", 31, True, 1), ("energy", 391, True, 10),
    ("sugars", 0.5, True, 0), ("sugars", 0.6, True, 1), ("sugars", 11.1, True, 10),
    ("proteins", 1.2, True, 0), ("proteins", 3.1, True, 7),
    ("fruits_vegetables_legumes", 41, True, 2), ("fruits_vegetables_legumes", 61, True, 4),
    ("fruits_vegetables_legumes", 81, True, 6),
    # the saturated fat / fat ratio is the one ">=" table
    ("saturated_fat_ratio", 9.9, False, 0), ("saturated_fat_ratio", 10, False, 1), ("saturated_fat_ratio", 64, False, 10),
    ("energy_from_saturated_fat", 120, False, 0), ("energy_from_saturated_fat", 1201, False, 10),
])
def test_point_tables(component, value, beverage, expected):
    assert alg.points(component, value, beverage)[0] == expected


@pytest.mark.parametrize("variant,score,grade", [
    (alg.GENERAL, 0, "a"), (alg.GENERAL, 1, "b"), (alg.GENERAL, 2, "b"), (alg.GENERAL, 3, "c"),
    (alg.GENERAL, 10, "c"), (alg.GENERAL, 11, "d"), (alg.GENERAL, 18, "d"), (alg.GENERAL, 19, "e"),
    (alg.CHEESE, 0, "a"), (alg.RED_MEAT, 19, "e"),
    (alg.FATS, -6, "a"), (alg.FATS, -5, "b"), (alg.FATS, 2, "b"), (alg.FATS, 3, "c"), (alg.FATS, 19, "e"),
    (alg.BEVERAGE, -10, "b"), (alg.BEVERAGE, 2, "b"), (alg.BEVERAGE, 3, "c"), (alg.BEVERAGE, 6, "c"),
    (alg.BEVERAGE, 7, "d"), (alg.BEVERAGE, 9, "d"), (alg.BEVERAGE, 10, "e"),
    (alg.WATER, 0, "a"),
])
def test_grade_thresholds(variant, score, grade):
    assert alg.grade(score, variant) == grade


# --- worked examples: general foods ------------------------------------------------

def test_off_cookies_protein_not_counted_at_11_negative_points():
    # OFF test "cookies": 3460 kJ, 0 g sugars, 15 g saturated fat, 0 g salt -> 20 (E)
    res = alg.score(alg.GENERAL, energy_kj=3460, sugars_g=0, saturated_fat_g=15, salt_g=0, fiber_g=0,
                    proteins_g=8, fruits_vegetables_legumes_pct=None)
    assert pts(res)["energy"] == 10 and pts(res)["saturated_fat"] == 10
    assert res["negative_points"] == 20 and not res["count_proteins"]
    assert (res["score"], res["grade"]) == (20, "e")


def test_off_breakfast_cereals():
    # OFF test "breakfast-cereals": 2450 kJ, 31 g sugars, 1.03 g sat fat, 0.55 g salt, 6.9 g fiber -> 14 (D)
    res = alg.score(alg.GENERAL, energy_kj=2450, sugars_g=31, saturated_fat_g=1.03, salt_g=0.55, fiber_g=6.9,
                    proteins_g=9, fruits_vegetables_legumes_pct=None)
    assert {k: pts(res)[k] for k in ("energy", "sugars", "saturated_fat", "salt", "fiber")} == {
        "energy": 7, "sugars": 8, "saturated_fat": 1, "salt": 2, "fiber": 4}
    assert (res["negative_points"], res["positive_points"]) == (18, 4)
    assert (res["score"], res["grade"]) == (14, "d")


def test_off_canned_green_beans_fruit_vegetables_and_protein_counted():
    # OFF test "fr-canned-green-beans": 315 kJ, 3.6 g sugars, 0.1 g sat fat, 1.2 g salt,
    # 5 g protein, 5.3 g fiber, 50% vegetables -> 6 - 6 = 0 (A)
    res = alg.score(alg.GENERAL, energy_kj=315, sugars_g=3.6, saturated_fat_g=0.1, salt_g=1.2, fiber_g=5.3,
                    proteins_g=5, fruits_vegetables_legumes_pct=50)
    assert pts(res) == {"energy": 0, "sugars": 1, "saturated_fat": 0, "salt": 5,
                        "proteins": 2, "fiber": 3, "fruits_vegetables_legumes": 1}
    assert (res["score"], res["grade"]) == (0, "a")


def test_off_product_instant_oatmeal():
    # OFF 0011110719072 "instant oatmeal original": published 2023 score -7 (A)
    res = alg.score(alg.GENERAL, energy_kj=1746.33, sugars_g=0, saturated_fat_g=0, salt_g=0, fiber_g=10.7,
                    proteins_g=17.86, fruits_vegetables_legumes_pct=0)
    assert (res["negative_points"], res["positive_points"]) == (5, 12)
    assert (res["score"], res["grade"]) == (-7, "a")


# --- cheese ---------------------------------------------------------------------------

def test_off_product_cheddar_cheese_keeps_protein():
    # OFF 0011110501677 "Mild Cheddar, Finely Shredded": published 2023 score 15 (D)
    inputs = dict(energy_kj=1644, sugars_g=0, saturated_fat_g=17.86, salt_g=1.6075, fiber_g=0, proteins_g=25,
                  fruits_vegetables_legumes_pct=0)
    res = alg.score(alg.CHEESE, **inputs)
    assert pts(res) == {"energy": 4, "sugars": 0, "saturated_fat": 10, "salt": 8, "proteins": 7, "fiber": 0,
                        "fruits_vegetables_legumes": 0}
    assert res["count_proteins"] and (res["score"], res["grade"]) == (15, "d")
    # Scored as a general food, the 22 negative points would drop the protein: 22 (E).
    assert (alg.score(alg.GENERAL, **inputs)["score"], alg.score(alg.GENERAL, **inputs)["grade"]) == (22, "e")


# --- red meat --------------------------------------------------------------------------

def test_off_red_meat_protein_capped_at_two_points():
    # OFF test "en-red-meat-category-no-ingredients": 82 kJ, 10 g sat fat, 50 g protein -> 9 - 2 = 7 (C)
    res = alg.score(alg.RED_MEAT, energy_kj=82, sugars_g=0, saturated_fat_g=10, salt_g=0, fiber_g=None,
                    proteins_g=50, fruits_vegetables_legumes_pct=None)
    assert res["components"]["proteins"]["points"] == 2
    assert res["components"]["proteins"]["capped"] == "red_meat_product"
    assert (res["score"], res["grade"]) == (7, "c")
    # The same food without the red-meat rule keeps 7 protein points: 2 (B).
    assert alg.score(alg.GENERAL, energy_kj=82, sugars_g=0, saturated_fat_g=10, salt_g=0,
                     proteins_g=50)["score"] == 2


def test_off_product_beef_sliders_and_ground_beef():
    # OFF 0011110602855 "Mini beef burgers sliders": published 5 (C)
    res = alg.score(alg.RED_MEAT, energy_kj=895, sugars_g=0, saturated_fat_g=5.36, salt_g=0.1775, fiber_g=0,
                    proteins_g=19.64, fruits_vegetables_legumes_pct=0)
    assert (res["negative_points"], res["score"], res["grade"]) == (7, 5, "c")
    # OFF 0011110979209 "Ground Beef": published 13 (D); 13 negative points, protein not counted
    res = alg.score(alg.RED_MEAT, energy_kj=1290, sugars_g=0, saturated_fat_g=10.6, salt_g=0.165, fiber_g=0,
                    proteins_g=15, fruits_vegetables_legumes_pct=0)
    assert not res["count_proteins"] and (res["score"], res["grade"]) == (13, "d")


# --- fats, oils, nuts and seeds ------------------------------------------------------------

def test_off_olive_oil():
    # OFF test "olive-oil": energy from saturates 518 kJ (14 g x 37), ratio 15.2%, 100% olive -> 5 - 5 = 0 (B)
    res = alg.score(alg.FATS, energy_kj=3700, sugars_g=0, saturated_fat_g=14, fat_g=92, salt_g=0, fiber_g=0,
                    proteins_g=0, fruits_vegetables_legumes_pct=100)
    assert res["components"]["energy_from_saturated_fat"]["value"] == 518
    assert res["components"]["saturated_fat_ratio"]["value"] == 15.2
    assert pts(res) == {"energy_from_saturated_fat": 4, "sugars": 0, "saturated_fat_ratio": 1, "salt": 0,
                        "proteins": 0, "fiber": 0, "fruits_vegetables_legumes": 5}
    assert (res["score"], res["grade"]) == (0, "b")


def test_off_sunflower_oil_ratio_uses_greater_or_equal():
    # OFF test "sunflower-oil": 10 g sat fat in 100 g fat -> ratio exactly 10% = 1 point; 370 kJ -> 3: 4 (C)
    res = alg.score(alg.FATS, sugars_g=0, saturated_fat_g=10, fat_g=100, salt_g=0, fiber_g=0, proteins_g=0,
                    fruits_vegetables_legumes_pct=None)
    assert pts(res)["saturated_fat_ratio"] == 1 and pts(res)["energy_from_saturated_fat"] == 3
    assert (res["score"], res["grade"]) == (4, "c")


def test_off_products_almonds_peanut_butter_butter():
    # OFF 0011213146690 "Almonds, roasted & salted": published -6 (A): 6 negative (< 7) so protein counts
    res = alg.score(alg.FATS, sugars_g=3.57, saturated_fat_g=3.57, fat_g=57.14, salt_g=0.8475, fiber_g=10.7,
                    proteins_g=21.43, fruits_vegetables_legumes_pct=0)
    assert res["negative_points"] == 6 and res["count_proteins"]
    assert (res["score"], res["grade"]) == (-6, "a")
    # OFF 0011110016188 "Peanut Butter": published 9 (C): 12 negative (>= 7), protein dropped
    res = alg.score(alg.FATS, sugars_g=12.5, saturated_fat_g=7.8125, fat_g=46.875, salt_g=1.171875, fiber_g=6.25,
                    proteins_g=21.875, fruits_vegetables_legumes_pct=3.2)
    assert res["negative_points"] == 12 and not res["count_proteins"]
    assert (res["score"], res["grade"]) == (9, "c")
    # OFF 0011150504676 "European style unsalted butter": published 20 (E)
    res = alg.score(alg.FATS, sugars_g=0, saturated_fat_g=57.14, fat_g=85.71, salt_g=0, fiber_g=0, proteins_g=0,
                    fruits_vegetables_legumes_pct=0)
    assert (res["score"], res["grade"]) == (20, "e")


# --- beverages and water --------------------------------------------------------------------

def test_off_milk_is_a_beverage():
    # OFF test "milk": 3378 kJ, 10 g sugars, 5 g sat fat, 5 g protein, 2 g fiber -> 22 - 7 = 15 (E)
    res = alg.score(alg.BEVERAGE, energy_kj=3378, sugars_g=10, saturated_fat_g=5, salt_g=0, fiber_g=2, proteins_g=5,
                    fruits_vegetables_legumes_pct=0)
    assert pts(res) == {"energy": 10, "sugars": 8, "saturated_fat": 4, "salt": 0, "non_nutritive_sweeteners": 0,
                        "proteins": 7, "fiber": 0, "fruits_vegetables_legumes": 0}
    assert (res["score"], res["grade"]) == (15, "e")


def test_off_ice_tea_with_sweetener_gets_four_points():
    # OFF test "fr-ice-tea-with-sweetener" (steviol glycosides): 82 kJ, 4.5 g sugars, 0.03 g salt -> 8 (D)
    res = alg.score(alg.BEVERAGE, energy_kj=82, sugars_g=4.5, saturated_fat_g=0, salt_g=0.03, fiber_g=None,
                    proteins_g=0, fruits_vegetables_legumes_pct=0.1, non_nutritive_sweeteners=True)
    assert pts(res)["non_nutritive_sweeteners"] == 4
    assert (res["score"], res["grade"]) == (8, "d")
    without = alg.score(alg.BEVERAGE, energy_kj=82, sugars_g=4.5, saturated_fat_g=0, salt_g=0.03, proteins_g=0)
    assert (without["score"], without["grade"]) == (4, "c")


def test_off_orange_juice_and_soy_drink():
    # OFF test "en-orange-juice-category-and-ingredients": 182 kJ, 8.9 g sugars, 0.5 g salt, 50% fruit -> 10 (E)
    res = alg.score(alg.BEVERAGE, energy_kj=182, sugars_g=8.9, saturated_fat_g=0, salt_g=0.5, fiber_g=0.5,
                    proteins_g=0.2, fruits_vegetables_legumes_pct=50)
    assert pts(res)["fruits_vegetables_legumes"] == 2 and (res["score"], res["grade"]) == (10, "e")
    # OFF test "fr-plant-beverages-soy-milk": 178 kJ, 0.5 g sugars, 3.9 g protein, 8% soy -> -4 (B, not A)
    res = alg.score(alg.BEVERAGE, energy_kj=178, sugars_g=0.5, saturated_fat_g=0.6, salt_g=0.03, fiber_g=0.5,
                    proteins_g=3.9, fruits_vegetables_legumes_pct=8)
    assert pts(res)["proteins"] == 7 and (res["score"], res["grade"]) == (-4, "b")


def test_off_product_sodas():
    # OFF 0011110021625 "Sparkling soda": published 11 (E); 0000764001787 "Ryan's, Lemonade": 14 (E)
    assert alg.score(alg.BEVERAGE, energy_kj=159, sugars_g=9.17, saturated_fat_g=0, salt_g=0.03, fiber_g=0,
                     proteins_g=0, fruits_vegetables_legumes_pct=0)["score"] == 11
    assert alg.score(alg.BEVERAGE, energy_kj=226, sugars_g=13.33, saturated_fat_g=0, salt_g=0.01, fiber_g=0,
                     proteins_g=0, fruits_vegetables_legumes_pct=15.8)["score"] == 14


def test_water_is_always_a():
    # OFF test "spring-water-no-nutrition"
    assert alg.score(alg.WATER)["grade"] == "a"


# --- our USDA records -> inputs --------------------------------------------------------------

def item(text, pct=None, depth=0, minor=False):
    return {"id": None, "text": text, "depth": depth, "percent": None, "percent_estimate": pct, "minor": minor}


def record(category, name, n100, items=(), groups=None, alcohol=None):
    base = dict.fromkeys(["energy_kcal", "fat_g", "saturated_fat_g", "sugars_g", "carbs_g", "salt_g",
                          "sodium_mg", "fiber_g", "protein_g"])
    base.update(n100)
    return {
        "name": name, "main_category": category,
        "nutrition": {"per_100g": base, "implausible": []},
        "ingredients": {"items": list(items)},
        "derived": {"groups": groups or {"artificial_sweeteners": {"status": "none"}}},
        "source": {"raw": {"nutrients": {"1018": alcohol} if alcohol is not None else {}}},
    }


SODA = {"energy_kcal": 42, "fat_g": 0, "saturated_fat_g": 0, "sugars_g": 10.6, "salt_g": 0.02, "protein_g": 0}


def test_compute_beverage_from_usda_category_with_units_converted():
    rec = record("Soda", "Cola", SODA, [item("carbonated water", 85), item("high fructose corn syrup", 15)])
    res = compute(rec)
    assert res["category_variant"] == alg.BEVERAGE and res["version"] == "2023"
    assert res["components"]["energy"]["value"] == pytest.approx(42 * 4.184)  # kcal -> kJ: 176 kJ = 3 points
    assert pts(res)["energy"] == 3 and pts(res)["sugars"] == 9
    assert (res["score"], res["grade"]) == (12, "e")
    assert "fiber_g" in res["missing"]  # missing fiber scores 0, as at OFF


def test_compute_sodium_only_is_converted_to_salt():
    rec = record("Soda", "Cola", {**SODA, "salt_g": None, "sodium_mg": 400})  # 400 mg sodium = 1.0 g salt
    assert compute(rec)["components"]["salt"]["value"] == pytest.approx(1.0)


def test_compute_sweetener_in_beverage():
    diet = {**SODA, "energy_kcal": 0, "sugars_g": 0}
    rec = record("Soda", "Diet Cola", diet, [item("carbonated water", 99), item("aspartame", 1)],
                 groups={"artificial_sweeteners": {"status": "contains"}})
    res = compute(rec)
    assert pts(res)["non_nutritive_sweeteners"] == 4 and res["grade"] == "c"
    stevia = record("Iced & Bottle Tea", "Green Tea", diet, [item("brewed tea", 99), item("stevia leaf extract", 1)])
    assert pts(compute(stevia))["non_nutritive_sweeteners"] == 4
    erythritol = record("Soda", "Soda", diet, [item("carbonated water", 99), item("erythritol", 1)])
    assert pts(compute(erythritol))["non_nutritive_sweeteners"] == 0  # polyols are not counted


def test_plain_water_vs_flavored_water():
    zero = {"energy_kcal": 0, "fat_g": 0, "saturated_fat_g": 0, "sugars_g": 0, "salt_g": 0, "protein_g": 0}
    plain = record("Water", "Purified Drinking Water", zero,
                   [item("purified water", 99), item("magnesium sulfate", 0.5), item("potassium bicarbonate", 0.5)])
    assert compute(plain)["category_variant"] == alg.WATER and compute(plain)["grade"] == "a"
    flavored = record("Water", "Sparkling Water, Lemon Lime", zero, [item("carbonated water", 99), item("natural flavor", 1)])
    res = compute(flavored)
    assert res["category_variant"] == alg.BEVERAGE and (res["score"], res["grade"]) == (0, "b")


def test_milk_category_yogurt_is_not_a_drink_but_kefir_is():
    n = {"energy_kcal": 60, "fat_g": 3, "saturated_fat_g": 2, "sugars_g": 5, "salt_g": 0.1, "protein_g": 3.3}
    assert variant(record("Milk/Milk Substitutes", "Greek Yogurt Lemon Curd", n))[0] == alg.GENERAL
    assert variant(record("Milk", "2% Reduced Fat Milk", n))[0] == alg.BEVERAGE
    assert variant(record("Yogurt", "Plain Kefir", n))[0] == alg.BEVERAGE


def test_cheese_category():
    n = {"energy_kcal": 393, "fat_g": 32.14, "saturated_fat_g": 17.86, "sugars_g": 0, "salt_g": 1.6075,
         "protein_g": 25, "fiber_g": 0}
    res = compute(record("Cheese", "Mild Cheddar, Finely Shredded", n, [item("cheddar cheese", 100)]))
    assert res["category_variant"] == alg.CHEESE and (res["score"], res["grade"]) == (15, "d")
    assert variant(record("Cheese", "Vegan Cheddar Shreds", n))[0] == alg.GENERAL


def test_fats_oils_nuts_seeds():
    oil = {"energy_kcal": 884, "fat_g": 100, "saturated_fat_g": 14, "sugars_g": 0, "salt_g": 0, "protein_g": 0}
    res = compute(record("Vegetable & Cooking Oils", "Extra Virgin Olive Oil", oil, [item("extra virgin olive oil", 100)]))
    assert res["category_variant"] == alg.FATS
    assert res["components"]["fruits_vegetables_legumes"]["value"] == 100  # olive oil counts in this category only
    assert (res["score"], res["grade"]) == (0, "b")
    nuts = {"energy_kcal": 607, "fat_g": 57.14, "saturated_fat_g": 3.57, "sugars_g": 3.57, "salt_g": 0.8475,
            "protein_g": 21.43, "fiber_g": 10.7}
    res = compute(record("Popcorn, Peanuts, Seeds & Related Snacks", "Almonds, Roasted & Salted", nuts,
                         [item("almonds", 99), item("sea salt", 1)]))
    assert res["category_variant"] == alg.FATS and (res["score"], res["grade"]) == (-6, "a")
    assert variant(record("Popcorn, Peanuts, Seeds & Related Snacks", "Caramel Popcorn", nuts,
                          [item("popcorn", 80)]))[0] == alg.GENERAL
    assert variant(record("Popcorn, Peanuts, Seeds & Related Snacks", "Milk Chocolate Covered Almonds", nuts,
                          [item("milk chocolate", 60), item("almonds", 40)]))[0] == alg.GENERAL


def test_red_meat_from_ingredients_and_name():
    n = {"energy_kcal": 214, "fat_g": 16.07, "saturated_fat_g": 5.36, "sugars_g": 0, "salt_g": 0.1775,
         "protein_g": 19.64, "fiber_g": 0}
    res = compute(record("Frozen Patties and Burgers", "Mini Beef Sliders", n, [item("beef", 100)]))
    assert res["category_variant"] == alg.RED_MEAT and (res["score"], res["grade"]) == (5, "c")
    res = compute(record("Other Meats", "Beef Cubed Steak", n, [item("beef and natural flavorings", 100)]))
    assert res["category_variant"] == alg.RED_MEAT
    res = compute(record("Poultry, Chicken & Turkey", "Turkey Burgers", n, [item("turkey", 95), item("salt", 5)]))
    assert res["category_variant"] == alg.GENERAL
    assert is_red_meat("pork") and not is_red_meat("beef broth") and not is_red_meat("turkey ham")


def test_fruit_vegetable_legume_estimate_from_ingredients():
    assert is_fvl("organic diced tomatoes") and is_fvl("black beans") and is_fvl("sugar snap peas")
    assert not is_fvl("tomato powder") and not is_fvl("pea protein") and not is_fvl("potatoes")
    assert not is_fvl("olive oil") and is_fvl("olive oil", fats=True)
    assert not is_fvl("vanilla bean") and not is_fvl("black pepper") and not is_fvl("coconut water")
    n = {"energy_kcal": 90, "fat_g": 0.5, "saturated_fat_g": 0, "sugars_g": 1, "salt_g": 0.8, "protein_g": 6,
         "fiber_g": 6}
    # Canned beans: salt 5% -> capped at the 0.8 g salt on the label, beans 78.3 / water 20.9 pro rata;
    # the packing water is not eaten, so beans = 78.3 / (100 - 20.9) = 99%.
    rec = record("Canned & Bottled Beans", "Black Beans", n,
                 [item("black beans", 75), item("water", 20), item("salt", 5)])
    pct, source = fvl_percent(rec)
    assert source == "ingredients" and pct == 99.0
    # Nested: only the vegetable sub-ingredients of a sauce count.
    rec = record("Prepared Pasta & Pizza Sauces", "Pasta Sauce", n,
                 [item("tomato puree", 60), item("water", 20, ), item("seasoning", 20),
                  item("onion powder", 10, depth=1), item("salt", 10, depth=1)])
    assert fvl_percent(rec)[0] == 60.0
    # No ingredient list: OFF's category fallback (frozen vegetables -> 90%)
    assert fvl_percent(record("Frozen Vegetables", "Cut Green Beans", n)) == (90.0, "category")


def test_salt_reestimate_moves_share_to_main_ingredients():
    items = [item("cucumbers", 60), item("water", 25), item("salt", 15)]
    out = reestimate(items, salt_g=2.0)
    assert [round(i["percent_estimate"], 1) for i in out] == [69.2, 28.8, 2.0]
    assert items[2]["percent_estimate"] == 15  # input untouched


def test_out_of_scope_and_prepared_products():
    n = {"energy_kcal": 40, "fat_g": 0, "saturated_fat_g": 0, "sugars_g": 3, "salt_g": 0, "protein_g": 0.5}
    assert scope(record("Alcohol", "Lager Beer", n))[0] == "not_applicable"
    assert scope(record("Other Drinks", "Hard Cider", n))[0] == "not_applicable"
    assert scope(record("Other Drinks", "Kombucha", n, alcohol=2.0))[0] == "not_applicable"
    assert scope(record("Herbs & Spices", "Ground Cumin", n))[0] == "not_applicable"
    assert scope(record("Baby/Infant  Foods/Beverages", "Apple Puree", n))[0] == "not_applicable"
    assert scope(record("Pickles, Olives, Peppers & Relishes", "Distilled White Vinegar", n))[0] == "not_applicable"
    assert scope(record("Powdered Drinks", "Lemonade Drink Mix", n))[0] == "needs_prepared_values"
    assert scope(record("Coffee", "Medium Roast Ground Coffee", n))[0] == "needs_prepared_values"
    assert scope(record("Coffee", "Vanilla Latte Iced Coffee Drink", n)) is None
    assert scope(record("Cake, Cookie & Cupcake Mixes", "Yellow Cake Mix", n))[0] == "needs_prepared_values"
    res = compute(record("Alcohol", "Lager Beer", n))
    assert res["grade"] is None and res["status"] == "not_applicable" and res["reasons"] == ["alcoholic beverage"]


def test_missing_data_is_reported_not_guessed():
    res = compute(record("Candy", "Gummy Bears", {"energy_kcal": 350, "fat_g": 0, "sugars_g": None, "salt_g": 0.1,
                                                  "protein_g": 6}))
    assert res["grade"] is None and res["status"] == "missing_data"
    assert res["missing"] == ["sugars_g", "fiber_g"]  # saturated fat is 0 because fat is 0 (OFF's rule)
    # Sugars may also be deduced from 0 g carbohydrates.
    res = compute(record("Other Meats", "Chicken Breast", {"energy_kcal": 120, "fat_g": 2, "saturated_fat_g": 0.5,
                                                          "carbs_g": 0, "salt_g": 0.2, "protein_g": 23}))
    assert res["grade"] == "a" and res["components"]["sugars"]["value"] == 0
