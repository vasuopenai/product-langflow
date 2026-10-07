"""Write the tiny USDA Branded Foods sample in tests/fixtures/usda/ (run once; output is committed)."""

import csv
from pathlib import Path

OUT = Path(__file__).parent / "usda"
BRANDED_COLS = ["fdc_id", "brand_owner", "brand_name", "subbrand_name", "gtin_upc", "ingredients",
                "serving_size", "serving_size_unit", "household_serving_fulltext",
                "branded_food_category", "package_weight", "market_country", "discontinued_date",
                "available_date", "modified_date"]

# fdc_id, gtin, brand_owner, brand_name, description, ingredients, serving g, household,
# category, discontinued, available, nutrients per 100 g {usda nutrient id: amount}
PRODUCTS = [
    ("1001", "012345678905", "COASTLINE FOODS INC.", "COASTLINE", "AVOCADO OIL SEA SALT KETTLE CHIPS",
     "POTATOES, EXPELLER PRESSED AVOCADO OIL, SEA SALT.", "28", "1 ONZ", "Chips, Pretzels & Snacks", "",
     "2025-01-10", {"1008": 536, "1003": 7.1, "1004": 32.1, "1005": 53.6, "2000": 0, "1079": 3.6, "1093": 268}),
    ("1002", "012345678912", "COASTLINE FOODS INC.", "COASTLINE", "CLASSIC POTATO CHIPS",
     "POTATOES, CANOLA AND/OR SUNFLOWER OIL, SALT.", "28", "1 ONZ", "Chips, Pretzels & Snacks", "",
     "2025-01-10", {"1008": 536, "1003": 7.1, "1004": 32.1, "1005": 53.6, "2000": 0, "1079": 3.6, "1093": 500}),
    ("1003", "0098765432109", "TRAILHEAD NUTRITION LLC", "TRAILHEAD", "CHOCOLATE ALMOND PROTEIN BAR",
     "PROTEIN BLEND (WHEY PROTEIN ISOLATE, MILK PROTEIN ISOLATE), ALMONDS, HONEY, CHICORY ROOT FIBER, "
     "COCOA, SEA SALT, NATURAL FLAVOR. CONTAINS: MILK, ALMONDS.", "60", "1 BAR (60 g)",
     "Snack, Energy & Granola Bars", "", "2025-03-01",
     {"1008": 400, "1003": 35, "1004": 15, "1005": 40, "2000": 8.3, "1235": 6.7, "1079": 15, "1093": 250}),
    # Older record of the same GTIN: must be replaced by 1003.
    ("0903", "98765432109", "TRAILHEAD NUTRITION LLC", "TRAILHEAD", "PROTEIN BAR OLD FORMULA",
     "PROTEIN BLEND (SOY PROTEIN ISOLATE), CORN SYRUP.", "60", "1 BAR", "Snack, Energy & Granola Bars", "",
     "2021-06-01", {"1008": 380, "1003": 30, "1004": 10, "1005": 45}),
    ("1004", "011111111111", "OLD SNACKS CO", "OLD SNACKS", "DISCONTINUED CRACKERS",
     "WHEAT FLOUR, PALM OIL, SALT.", "30", "5 CRACKERS", "Crackers & Biscotti", "2024-02-01",
     "2020-01-01", {"1008": 480, "1003": 9, "1004": 20, "1005": 65}),
    # Per-100 g numbers that can't be real (150 g protein): rejected by the plausibility checks.
    ("1005", "022222222222", "BAD DATA CO", "BAD DATA", "BEEF JERKY",
     "BEEF, SUGAR, SALT.", "28", "1 ONZ", "Other Meats", "", "2025-01-01",
     {"1008": 980, "1003": 150, "1004": 12, "1005": 50}),
    ("1006", "033333333333", "RIDGE CHOCOLATE CO", "RIDGE", "85% DARK CHOCOLATE",
     "CHOCOLATE LIQUOR, SUGAR, COCOA BUTTER, VANILLA BEANS.", "40", "4 PIECES (40 g)", "Chocolate", "",
     "2025-02-01", {"1008": 600, "1003": 10, "1004": 50, "1005": 30, "2000": 12, "1079": 12, "1093": 10}),
]


def main():
    OUT.mkdir(exist_ok=True)
    with open(OUT / "branded_food.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(BRANDED_COLS)
        for fdc, gtin, owner, brand, _, ingr, serving, house, cat, disc, avail, _ in PRODUCTS:
            w.writerow([fdc, owner, brand, "", gtin, ingr, serving, "g", house, cat, "", "United States",
                        disc, avail, avail])
    with open(OUT / "food.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["fdc_id", "data_type", "description"])
        for p in PRODUCTS:
            w.writerow([p[0], "branded_food", p[4]])
    with open(OUT / "food_nutrient.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["id", "fdc_id", "nutrient_id", "amount"])
        n = 0
        for p in PRODUCTS:
            for nid, amount in p[-1].items():
                n += 1
                w.writerow([n, p[0], nid, amount])


if __name__ == "__main__":
    main()
