# Nutri-Score: how it is calculated, and our implementation for USDA products

This is a stopgap: it scores our USDA products with the official Nutri-Score (2023
algorithm) until we design our own method. It explains the calculation in plain
terms, what changed in 2023, how Open Food Facts (OFF) computes it, how we map our
USDA records onto it, and how closely our grades match the ones OFF publishes.

Code: `nutriscore/` (pure Python, no network). Tests: `tests/test_nutriscore.py`.

```
python -m nutriscore show 0016000275287            # one product, with every point
python -m nutriscore validate --parquet data/food.parquet [--sample 20000] [--json report.json]
```

```python
from nutriscore import compute
compute(record)   # record = a products.record from Postgres (off_products.usda.normalize)
# {"score": 15, "grade": "d", "category_variant": "cheese", "components": {...points per component...},
#  "version": "2023", "missing": ["fiber_g"], "estimated": [...], "reasons": [], "status": "computed"}
```

---

## 0. Which grade the app shows

The grade Open Food Facts publishes for the barcode when OFF has one (card field
`nutriscore_source: "off"`), otherwise ours (`"computed"`). OFF's grades live in the small
`nutriscore_off (code, grade)` table, refreshed from a new OFF export without rewriting product
rows (`nutriscore/off_grades.py`):

```
python -m nutriscore off-grades --parquet data/food.parquet --csv data/off_grades.csv   # local
deploy/lightsail/lightsail.sh push-off-grades                                             # server
```

Records also keep both: `scores.nutriscore_computed` (ours), `scores.nutriscore_off` (from a
USDA load with OFF extras) and `scores.nutriscore_grade` (the one shown). The
`max_nutriscore_grade` filter and the app's A-to-E sort use the shown grade.

## 1. The idea in one paragraph

Nutri-Score turns a product's nutrition panel (per 100 g, or per 100 ml for drinks)
into one number and a letter A–E. It adds **"bad" points** for energy, sugars,
saturated fat and salt (plus non-nutritive sweeteners in drinks), subtracts **"good"
points** for protein, fibre and the share of fruit, vegetables and legumes, and cuts
the result into five bands. Lower is better. A few food groups use adjusted rules
(cheese, red meat, fats/oils/nuts/seeds, drinks, water) so that the letter ranks
products *within* a shelf in line with dietary guidelines. It does not look at
processing, additives or portion size.

## 2. The calculation, step by step

1. **Take the values per 100 g** (per 100 ml for drinks), as sold — or as prepared,
   for products that must be prepared (powders, mixes); see section 4.
2. **Negative points ("A points")**: look up each nutrient in its table; a value earns
   one point for every threshold it is *strictly greater than*.
3. **Positive points ("C points")**: same lookup for protein, fibre and % fruit/veg/legumes.
4. **Protein rule**: protein points only count if the negative points are below 11
   (below 7 for fats/oils/nuts/seeds). Cheese and drinks always count protein. Red
   meat products get at most 2 protein points.
5. **Score = negative points − positive points** (from about −17 to +55).
6. **Grade**: compare the score with the variant's band limits (section 2.4).

### 2.1 General foods (also cheese and red meat)

Negative points — a value strictly above the listed number earns that many points.

| Points | Energy (kJ) | Sugars (g) | Saturated fat (g) | Salt (g) |
|---:|---:|---:|---:|---:|
| 0 | ≤ 335 | ≤ 3.4 | ≤ 1 | ≤ 0.2 |
| 1 | > 335 | > 3.4 | > 1 | > 0.2 |
| 2 | > 670 | > 6.8 | > 2 | > 0.4 |
| 3 | > 1005 | > 10 | > 3 | > 0.6 |
| 4 | > 1340 | > 14 | > 4 | > 0.8 |
| 5 | > 1675 | > 17 | > 5 | > 1.0 |
| 6 | > 2010 | > 20 | > 6 | > 1.2 |
| 7 | > 2345 | > 24 | > 7 | > 1.4 |
| 8 | > 2680 | > 27 | > 8 | > 1.6 |
| 9 | > 3015 | > 31 | > 9 | > 1.8 |
| 10 | > 3350 | > 34 | > 10 | > 2.0 |
| 11 | | > 37 | | > 2.2 |
| 12 | | > 41 | | > 2.4 |
| 13 | | > 44 | | > 2.6 |
| 14 | | > 48 | | > 2.8 |
| 15 | | > 51 | | > 3.0 |
| 16–20 | | | | > 3.2, > 3.4, > 3.6, > 3.8, > 4.0 |

Maximum: 10 + 15 + 10 + 20 = 55 negative points.

Positive points:

| Points | Protein (g) | Fibre (g) | Fruit, vegetables, legumes (%) |
|---:|---:|---:|---:|
| 0 | ≤ 2.4 | ≤ 3.0 | ≤ 40 |
| 1 | > 2.4 | > 3.0 | > 40 |
| 2 | > 4.8 | > 4.1 | > 60 |
| 3 | > 7.2 | > 5.2 | |
| 4 | > 9.6 | > 6.3 | |
| 5 | > 12 | > 7.4 | > 80 |
| 6 | > 14 | | |
| 7 | > 17 | | |

Variants of the general rule:

- **Cheese**: same tables, but protein always counts (even with ≥ 11 negative points).
- **Red meat products** (beef, veal, pork, lamb, game, horse, goat…; OFF treats a
  product as red meat when red meat is > 10 % of its ingredients or its category is
  fresh red meat): same tables, protein capped at 2 points.

### 2.2 Fats, oils, nuts and seeds

Covers oils, butter, margarine, cream, and since 2023 nuts, peanuts, nut butters and
oil seeds (not chestnuts). Two negative components are replaced:

| Points | Energy from saturates (kJ) = sat. fat (g) × 37 | Saturated fat / total fat (%) |
|---:|---:|---:|
| 0 | ≤ 120 | < 10 |
| 1 | > 120 | ≥ 10 |
| 2 | > 240 | ≥ 16 |
| 3 | > 360 | ≥ 22 |
| 4 | > 480 | ≥ 28 |
| 5 | > 600 | ≥ 34 |
| 6 | > 720 | ≥ 40 |
| 7 | > 840 | ≥ 46 |
| 8 | > 960 | ≥ 52 |
| 9 | > 1080 | ≥ 58 |
| 10 | > 1200 | ≥ 64 |

Sugars, salt, protein and fibre use the general tables. Protein counts only if the
negative points are below **7**. Oils made from qualifying fruit/vegetables (olive,
avocado) count in the fruit/veg/legume share — in this category only.

### 2.3 Beverages

Drinks: water, soft drinks, juices and nectars, smoothies, tea/coffee drinks, and
since 2023 **milk, milk drinks, drinkable yogurts/kefir and plant-based drinks**.
Alcoholic drinks (> 1.2 % alcohol) are out of scope.

| Points | Energy (kJ/100 ml) | Sugars (g/100 ml) | Non-nutritive sweeteners |
|---:|---:|---:|---:|
| 0 | ≤ 30 | ≤ 0.5 | absent |
| 1 | > 30 | > 0.5 | |
| 2 | > 90 | > 2 | |
| 3 | > 150 | > 3.5 | |
| 4 | > 210 | > 5 | **present** |
| 5 | > 240 | > 6 | |
| 6 | > 270 | > 7 | |
| 7 | > 300 | > 8 | |
| 8 | > 330 | > 9 | |
| 9 | > 360 | > 10 | |
| 10 | > 390 | > 11 | |

Saturated fat and salt: general tables. Non-nutritive sweeteners (acesulfame K,
aspartame, cyclamate, saccharin, sucralose, thaumatin, neohesperidine DC, steviol
glycosides/stevia, neotame, advantame; **not** polyols such as erythritol) add a flat
4 points.

| Points | Protein (g/100 ml) | Fibre | Fruit, veg, legumes (%) |
|---:|---:|---|---:|
| 0 | ≤ 1.2 | general table | ≤ 40 |
| 1 | > 1.2 | | |
| 2 | > 1.5 | | > 40 |
| 3 | > 1.8 | | |
| 4 | > 2.1 | | > 60 |
| 5 | > 2.4 | | |
| 6 | > 2.7 | | > 80 |
| 7 | > 3.0 | | |

Protein always counts for drinks. **Water** (plain, unflavoured, nothing added) is
always A; no other drink can be A.

### 2.4 Score → grade

| Grade | General foods, cheese, red meat | Fats, oils, nuts, seeds | Beverages |
|---|---|---|---|
| A | ≤ 0 | ≤ −6 | water only |
| B | 1 – 2 | −5 – 2 | ≤ 2 |
| C | 3 – 10 | 3 – 10 | 3 – 6 |
| D | 11 – 18 | 11 – 18 | 7 – 9 |
| E | ≥ 19 | ≥ 19 | ≥ 10 |

### 2.5 Worked example (a real product)

"Mild Cheddar, Finely Shredded" (OFF 0011110501677), per 100 g: 1644 kJ, 0 g sugars,
17.9 g saturated fat, 1.61 g salt, 25 g protein, 0 g fibre, no fruit/veg.

- Energy 1644 kJ > 1340 → 4; sugars 0 → 0; saturated fat 17.9 > 10 → 10; salt 1.61 > 1.6 → 8.
  Negative = 22.
- Protein 25 > 17 → 7. As a **cheese**, protein counts despite 22 ≥ 11. Positive = 7.
- Score 22 − 7 = 15 → **D** (OFF publishes 15/D). Scored as a general food, the
  protein would be dropped: 22 → E. This is why the category variant matters.

## 3. What changed in 2023 (vs the original 2017 algorithm)

The update was proposed by the European Scientific Committee of the seven Nutri-Score
countries (Belgium, France, Germany, Luxembourg, the Netherlands, Spain, Switzerland):
foods in June 2022 (adopted July 2022), beverages in March 2023; countries adopted it
end of 2023 with a two-year transition for labels. OFF switched its published grade to
the 2023 version and still records both during the migration.

| | 2017 algorithm (OFF calls it "2021") | 2023 algorithm |
|---|---|---|
| Sugars | 0–10 pts, steps of 4.5 g | 0–15 pts, finer steps (3.4 g → 51 g) |
| Salt | sodium in mg, 0–10 pts (90 mg steps) | **salt** in g, 0–20 pts (0.2 g steps) |
| Fibre | 0–5 pts from 0.9 g | 0–5 pts from 3.0 g (aligned with the "source of fibre" claim) |
| Protein | 0–5 pts (1.6 g steps) | 0–7 pts (2.4 g steps) |
| Fruit/veg component | fruit, veg, legumes **and nuts and rapeseed/walnut/olive oil** | fruit, veg, legumes only; nuts and oils removed (olive/avocado oil only within the fats category) |
| Protein rule | not counted if negative ≥ 11 **unless fruit/veg = 5 pts**, or cheese | not counted if negative ≥ 11 (≥ 7 for fats); cheese and drinks always count; exception for fruit/veg removed |
| Red meat | — | protein capped at 2 points |
| Fats category | oils/fats, saturated-fat ratio | adds nuts and seeds; energy replaced by energy from saturates; own A/B limit (−6) |
| General A/B limit | A ≤ −1 | A ≤ 0 |
| Beverages | energy 0–270 kJ, sugars 0–13.5 g, fruit/veg up to 10 pts; milk and plant drinks scored as foods | new energy/sugar/protein tables, fruit/veg up to 6 pts, **+4 for sweeteners**, milk and plant drinks are beverages, B/C limit 2/3, C/D 6/7 |
| Beverage grades | B ≤ 1, C ≤ 5, D ≤ 9 | B ≤ 2, C ≤ 6, D ≤ 9 |

Net effect: sugary and salty foods score worse (cereals, sauces, ready meals); oily
fish, olive and rapeseed oil, nuts and wholegrain bread do better; sweetened drinks and
diet drinks do worse; milk and plant drinks are now judged against other drinks (plain
milk mostly B or C; sugary milk drinks lower).

## 4. How Open Food Facts computes it

Source: `openfoodfacts-server`, `lib/ProductOpener/Nutriscore.pm` (the algorithm),
`Food.pm` (data preparation and refusals), `Ingredients.pm` (estimates from
ingredients), `Config_off.pm` (category lists). Our point tables and comparison rules
are a line-for-line match (`>` everywhere, `>=` for the saturated-fat ratio, no rounding
of inputs; the ratio and the fruit/veg % kept to one decimal).

- **Inputs**: nutriments per 100 g/ml — energy (kJ), fat, saturated fat, sugars, salt,
  protein, fibre — plus `fruits-vegetables-legumes` (declared, or estimated), and the
  number of non-nutritive sweeteners found in the ingredient list.
- **Variant flags come from its category taxonomy**: beverage = `en:beverages` /
  `en:beverage-preparations` (plus milks, plant drinks, teas, coffees; minus soups and
  meal replacements); water = `en:spring-waters` not flavoured; cheese = `en:cheeses`;
  fats/oils/nuts/seeds = `en:fats`, `en:creams`, `en:seeds` or customs (HS) codes for
  nuts/seeds attached to the category; red meat = HS 02.01–02.06 category or red meat
  > 10 % of ingredients.
- **Fruit/veg/legume %**: (1) category overrides (e.g. crisps 0 %, olive oil 100 %);
  (2) a declared value; (3) an estimate from the ingredient list: each ingredient's
  declared or estimated % is summed when the ingredient is a fruit, vegetable or legume
  (Eurocode 2 groups 7.10, 8.x, 9.x, and herbs) and not "further processed" (flour,
  powder, freeze-dried, candied); a non-qualifying ingredient's sub-ingredients are
  searched; for canned vegetables the packing water is removed; (4) without ingredients,
  a category default (fruit juices 100, vegetables/fruits 90, jams 50, potatoes 0).
- **Refuses to compute** ("unknown") when there is no category, when energy, fat,
  saturated fat, sugars, sodium/salt or protein is missing (saturated fat may be taken
  as 0 when fat is 0, sugars as 0 when carbohydrates are 0), when a product that is
  eaten prepared (drink powders, dessert mixes, dried soups, syrups, cocoa powders,
  ground coffee) has no prepared values, or when nutrients mix estimated and declared
  sources. Missing fibre is allowed (0 points, with a warning).
- **Not applicable** for alcoholic drinks, baby foods/milks, chewing gum, food
  additives, dietary supplements, meal replacements, salt, spices, sugar substitutes,
  vinegars and non-food products (except tea/coffee-based drinks).
- The **export** (`data/food.parquet`) has `nutriscore_grade` and `nutriscore_score`
  for the current (2023) version only; there is no `nutriscore_data` column. Misc tags
  such as `en:nutriscore-2021-c-2023-d` keep the old/new grade pair.

## 5. Our implementation for USDA records

`nutriscore/algorithm.py` holds the tables and rules above. `nutriscore/usda.py`
decides the inputs from a product record:

| Input | From our record |
|---|---|
| Energy (kJ) | `nutrition.per_100g.energy_kcal × 4.184` (USDA reports kcal; kJ-only rows were converted to kcal at load) |
| Salt (g) | `per_100g.salt_g` (= sodium g × 2.5); falls back to `sodium_mg × 2.5 / 1000` |
| Sugars, saturated fat, fat, protein, fibre | `per_100g.sugars_g`, `saturated_fat_g`, `fat_g`, `protein_g`, `fiber_g` |
| Fruit/veg/legume % | estimated from `ingredients.items` (below) |
| Sweeteners (drinks) | `derived.groups.artificial_sweeteners` = "contains", or stevia/monk fruit/thaumatin in the ingredient text |
| Variant, scope | USDA `branded_food_category` (`record.main_category`) + product name + ingredients |

Values are per 100 g, or per 100 ml where the USDA serving is in ml (USDA does the
same per-100 normalisation either way).

### 5.1 Category variant mapping

Rules on the USDA category (first match), refined by the product name:

| Variant | USDA categories (examples) | Name/ingredient refinements |
|---|---|---|
| Out of scope | Alcohol, Baby/Infant foods, supplements/vitamins/meal replacements/weight control, Herbs & Spices, Vinegars | also: alcohol > 1.2 g/100 g, beer/wine/spirits/hard cider names in drink categories, chewing gum, products named "… vinegar", salt (≥ 50 g salt/100 g), sugar substitutes |
| Needs prepared values (not computed) | Powdered Drinks, Liquid Water Enhancer, "Not Ready to Drink" beverages, Cake/Cookie/Cupcake Mixes and Gravy Mix named "mix", Coffee (ground, pods, instant) | any drink/dessert/soup "… mix", "powder", "concentrate" (not "from concentrate") |
| Water | Water, Other Drinks, Non Alcoholic Beverages | unflavoured name, ≤ 1 kcal, no sugar, ingredients only water/minerals/CO₂ |
| Beverage | Soda, Juice/Nectars/Fruit Drinks, Iced & Bottle Tea, Sport Drinks, Energy/Protein Drinks, Other Drinks, Non Alcoholic Beverages – Ready to Drink, Plant Based Milk/Water, Milk, Milk/Milk Substitutes, Breakfast Drinks, Tea Bags, Water (flavoured), ready-to-drink Coffee | not powders/condensed/evaporated/frozen; in milk categories not yogurt, cheese, cream, creamer, pudding, mac & cheese; drinkable yogurt/kefir/smoothies from dairy categories |
| Cheese | Cheese, Cheese/Cheese Substitutes, Processed Cheese | not vegan/plant-based/dairy-free, cheesecake, cheese dip/sauce |
| Fats, oils, nuts, seeds | Vegetable & Cooking Oils, Oils/Fats Edible, Butter & Spread, Margarine, Cream, Nut & Seed Butters | not apple/cookie butter, chocolate/hazelnut spreads, dressings |
| … nuts and seeds by name | Popcorn, Peanuts, Seeds & Related Snacks; Other Grains & Seeds; Nuts/Seeds; Wholesome Snacks; Baking Decorations | name is a nut/seed and the first ingredient too; not popcorn, chocolate/candy-coated, trail mix, bars, clusters, chestnut, coconut |
| Red meat | any general food | red meat > 10 % of ingredients (beef, pork, veal, lamb, goat, venison, bison, ham, bacon, salami, pepperoni…, not broth/fat/flavour/gelatin or "turkey ham"), or a meat category with a red-meat product name |
| General | everything else | |

Limits: USDA categories are coarse and sometimes wrong (yogurts and mac & cheese
under "Milk/Milk Substitutes", broths under "Herbs/Spices/Extracts", ready gravy under
"Gravy Mix"), so the name rules are heuristics; a product in the wrong variant can move
1–2 grades. OFF's own variant depends on its (often missing) categories for US products.

### 5.2 Fruit/vegetable/legume estimate

USDA never declares the %. We reuse the per-ingredient percent estimates stored with
each record (label order: earlier ingredients weigh more; "2 % or less of" items get
half their cap) and:

1. correct one known weakness — salt near the end of a short list is estimated at
   10–25 %; we cap it at the product's salt content and give the freed share to the
   other ingredients of the same level, pro rata;
2. sum the ingredients that are fruit, vegetables or legumes (word lists, including
   herbs as OFF does) and not processed out of the category (powder, flour, starch,
   protein, fibre, oil, juice-for-colour, flavour, sugar/syrup, potato, corn other than
   sweet corn, peanuts, coconut, seeds); a non-matching ingredient's sub-ingredients
   are searched, as OFF does;
3. canned vegetables/beans/tomatoes: remove the packing water;
4. no ingredient list: OFF's category defaults (juices 100 %, fruit/vegetables 90 %,
   jams 50 %, fries/potatoes 0 %).

Our estimate falls in the same Nutri-Score band (≤ 40, 40–60, 60–80, > 80 %) as OFF's
own estimate for 88.6 % of the products where both exist.

### 5.3 Missing data and output

`compute()` never guesses a required value: with energy, fat, saturated fat, sugars,
salt or protein missing it returns `grade: None`, `status: "missing_data"` and the
list in `missing`. Fibre may be missing (0 points, listed in `missing`, as OFF does).
Out-of-scope products return `status: "not_applicable"` or `"needs_prepared_values"`
with the reason. `estimated` lists what was inferred (category variant, fruit/veg %).

## 6. Validation against Open Food Facts

`python -m nutriscore validate` joins our 425,531 USDA products to the OFF export by
barcode (digits with leading zeros stripped), scores every match, and compares with
OFF's published 2023 grade. It is read-only. Full run (no sampling), 2–4 minutes:

| | Products |
|---|---:|
| USDA products | 425,531 |
| Found in OFF by barcode | 364,156 |
| Graded by both | 273,823 |
| Graded only by us (OFF "unknown": mostly no category, or missing nutrients in OFF) | 62,638 |
| Graded only by OFF (we refuse: missing sugars/sat fat in USDA, prepared-only mixes, spices/vinegar/gum/salt that OFF has not categorised as such) | 10,359 |
| Neither | 17,336 |

**Agreement on the 273,823 products graded by both: 84.7 % exact grade, 95.1 % within
one grade.** For contrast, the same grades match OFF's old (2017/"2021") grade, kept
in its misc tags, only 57.9 % of the time — both sides are on the 2023 algorithm.

| Our variant | Products | Exact | Within one grade | Exact, when OFF's categories imply the same variant |
|---|---:|---:|---:|---:|
| General foods | 208,007 | 87.4 % | 96.8 % | 88.0 % (204,003) |
| Beverages | 26,795 | 79.4 % | 90.8 % | 87.2 % (24,319) |
| Fats, oils, nuts, seeds | 13,631 | 50.5 % | 74.3 % | 88.4 % (7,092) |
| Red meat | 13,382 | 86.7 % | 95.5 % | 86.8 % (13,347) |
| Cheese | 11,634 | 87.0 % | 98.5 % | 96.8 % (10,017) |
| Water | 374 | 22.7 % | 99.5 % | 100 % (18) |
| **All** | **273,823** | **84.7 %** | **95.1 %** | **88.2 % (258,796)** |

**Is the algorithm itself right?** Feeding OFF's *own* nutrient values, fruit/veg
estimate and category through our code reproduces OFF's grade for 99.2 % and its exact
score for 98.2 % of 273,238 products. The residual is OFF inputs the export does not
carry (category fruit/veg overrides, HS-code categories). So the disagreements are
about **inputs**, not arithmetic.

**Why we disagree** (for each disagreement, the first single OFF input that, swapped
into our computation, reproduces OFF's grade):

| Cause | Disagreements | What it means |
|---|---:|---|
| Nutrient values differ | 20,037 | OFF and USDA hold different label values (other product version, per-serving vs per-100 g entry, typos on either side). Not fixable on our side. |
| Fruit/veg/legume estimate | 8,371 | Our ingredient-based % lands in a different band than OFF's (both are estimates). |
| Several inputs differ | 7,213 | Mostly fats/nuts: OFF has no usable category for many US nuts, peanut butters and oils, so it scores them as general foods (e.g. pistachios E at OFF, A/B with the fats rule). |
| Category variant | 4,495 | OFF's categories put the product in another variant: uncategorised drinks scored as foods at OFF, sparkling/purified water (OFF only gives A to spring water), cheese without a cheese category, creams. |
| Non-nutritive sweetener detection | 878 | One side found a sweetener the other did not; e.g. OFF gives Diet Coke 0 points (B) where the rule adds 4 for aspartame (C). |
| Red meat protein cap | 314 | OFF did not flag the product as red meat (no HS code, ingredients not parsed). |
| Fiber missing in USDA | 48 | |
| Unexplained | 655 | Mostly obvious data errors on one side. |

Reading this as a product owner: where both sides agree on what kind of product it is,
we match OFF 88 % of the time (95 % within one grade overall); the rest is mostly
different label data and a different fruit/veg estimate. The fats/nuts and water gaps are cases where we follow the
official rule and OFF's US data cannot.

## 7. Takeaways for designing our own method

- The letter depends heavily on **which variant** a product falls in (cheese vs food,
  drink vs food, fats vs food): one product can move two grades. Any method we design
  needs a reliable product-type classification first.
- **Fruit/veg/legume %** is never on a US label and has to be estimated from
  ingredient order; a better percent estimator (using the nutrition panel as
  constraints, like OFF does) would improve any ingredient-based score.
- Nutri-Score ignores processing (NOVA), additives, sweetener type other than the
  drink penalty, whole grains and portion size; those are the obvious levers for our
  own method. Our records already carry ingredient groups (added sugars, seed oils,
  artificial colours…) and NOVA from OFF.
- Thresholds are per 100 g, which penalises dense foods eaten in small amounts
  (nuts, cheese, oils — hence their special rules) and favours watery foods.

## Sources

- Scientific Committee of the Nutri-Score, *Update of the Nutri-Score algorithm —
  Update report from the Scientific Committee of the Nutri-Score 2022* (foods; recap
  tables in the appendix):
  <https://mpc.gouvernement.lu/dam-assets/le-minist%C3%A8re/consodur/2022-main-algorithm-report-update-final.pdf>
  (also <https://www.rijksoverheid.nl/documenten/publicaties/2022/06/29/update-of-the-nutri-score-algorithm>)
- Scientific Committee of the Nutri-Score, *Update of the Nutri-Score algorithm for
  beverages — Second update report, 2023* (recap pp. 72–75):
  <https://www.santepubliquefrance.fr/determinants-de-sante/nutrition-et-activite-physique/documents/rapport-synthese/update-of-the-nutri-score-algorithm-for-beverages.-second-update-report-from-the-scientific-committee-of-the-nutri-score-v2-2023>
  (PDF copy <https://edepot.wur.nl/629571>)
- Santé publique France press releases:
  <https://www.santepubliquefrance.fr/en/press/transnational-governance-nutri-score-7-participating-countries-adopt-improved-algorithm-foods>,
  <https://www.santepubliquefrance.fr/en/press/nutri-score-calculation-algorithm-evolving-promote-healthier-food-choices>
- Open Food Facts server code: `lib/ProductOpener/Nutriscore.pm`, `Food.pm`,
  `Ingredients.pm`, `Config_off.pm`, and expected results in
  `tests/unit/expected_test_results/nutriscore/` —
  <https://github.com/openfoodfacts/openfoodfacts-server>
