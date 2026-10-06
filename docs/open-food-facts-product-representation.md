# Representing Open Food Facts products for natural-language search

Goal: answer free-form questions such as

- "Suggest a protein bar with at least 20 g protein and no seed oils"
- "Potato chips with very few ingredients, made with avocado oil"
- "Dark chocolate with more than 70% cocoa and under 8 g sugar per serving"
- "Snacks with more than 10 g protein, no artificial sweeteners, gluten-free"

These questions share a shape: **a product type**, plus **hard constraints**
(nutrient thresholds, ingredient amounts, include or exclude an ingredient or a
whole ingredient group, ingredient count, labels, allergens), plus **soft
intent** (flavor, style). The representation below is built for that shape,
not for any one example.

> Sources: OFF's OpenAPI product schemas (`docs/api/ref/schemas/*.yaml`), the
> ingredients, categories and additives taxonomies in
> `openfoodfacts/openfoodfacts-server`, and the Parquet export code in
> `openfoodfacts/openfoodfacts-exports` (`exports/parquet/food.py`). All were
> read on 2026-10-06. world.openfoodfacts.org itself was blocked from the
> environment this was written in, so check file names and sizes against the
> live /data page.

---

## 1. Data formats available, and which to use

| Format | What it is | Strengths | Weaknesses | Use it for |
|---|---|---|---|---|
| **JSONL dump** (`openfoodfacts-products.jsonl.gz`) | One full product document per line, the same data as the MongoDB dump. Refreshed daily. | Every field, including the nested `ingredients` tree with per-ingredient `percent` / `percent_min` / `percent_max` / `percent_estimate`, the `nutriments` dict with `_100g` and `_serving` values, all language variants. | Very large; schema is loose (strings vs numbers, hundreds of rarely used keys). | **The source of truth** when you need full fidelity. |
| **Parquet** (Hugging Face `openfoodfacts/product-database`, `food.parquet`) | A curated subset of the JSONL (~110 typed columns), generated daily by `openfoodfacts-exports`. | Typed, columnar, streamable in batches. Can be queried directly with DuckDB or Polars. Keeps everything this app needs: `ingredients` (as a JSON string), `ingredients_tags`, `nutriments` (list of `{name, 100g, serving, unit, prepared_*}`), `categories_tags`, `labels_tags`, `allergens_tags`, `traces_tags`, `additives_tags`, `nova_group`, `nutriscore_grade`, `serving_quantity`, `countries_tags`, `unique_scans_n`, `completeness`, `images`. | Multilingual text fields become `[{lang, text}]` lists. A few JSONL-only fields are dropped (e.g. `image_front_url`; you build image URLs from `images`). | **Recommended bulk ingest.** |
| **CSV** (`en.openfoodfacts.org.products.csv(.gz)`) | Tab-separated, flattened, one column per nutrient `_100g`. | Easy to open in pandas or a spreadsheet. | No ingredient tree, so no per-ingredient percentages. Lossy flattening, many sparse columns. | Quick exploration only. |
| **MongoDB dump** | `mongorestore` archive of the production database. | Exactly what the OFF server sees. | Needs a MongoDB instance; same content as the JSONL. | Only if you want to run MongoDB. |
| **Delta exports** (last 14 days, daily files named by Unix timestamps) | Products changed since the previous export. | Cheap daily refresh without re-reading the full dump. | Must be applied in order; only covers 14 days. | **Incremental updates** after the first full load. |
| **RDF** | Linked-data export with a limited field set. | Semantic-web tooling. | Missing most of what this app needs. | Skip. |
| **Live API** (`/api/v2/product/{barcode}`, search API) | Per-product JSON, same shape as the JSONL. | Always current; good for barcode lookups and images. | Rate-limited; not for bulk or for analytical filtering. | Refresh a product at answer time; barcode scans. |
| **Taxonomies** (`ingredients`, `categories`, `labels`, `allergens`, `additives`, `nutrients`, as JSON or `.txt`) | The controlled vocabularies behind every `*_tags` field, with synonyms in many languages and parent/child links. | Synonym resolution ("canola" means `en:canola-oil`), hierarchy ("potato chips" is under "crisps"), multilingual. | Hierarchy is general-purpose: there is no "seed oil" or "artificial sweetener" node. | **Required.** Use them to map user words to tag ids and to define ingredient groups. |
| Images, Open Prices | Product photos (S3/CDN); crowdsourced price data. | Display; price questions. | Not needed for filtering. | Optional add-ons. |

**Recommendation:** load the Parquet export once (stream it with pyarrow or
DuckDB; don't load it all into memory), apply the delta exports daily, use the
taxonomies to build the vocabulary, and call the API only for single-product
freshness or images. The normalizer in this repo accepts both the JSONL and the
Parquet row shapes, so you can switch sources without changing code
downstream.

---

## 2. Why pure vector search is not enough

The current notebook (`search_products11.ipynb`) embeds a text blob, retrieves
the top 5 by cosine similarity, and asks an LLM to filter. That can't reliably
answer these questions:

- Embeddings don't do arithmetic. "≥ 20 g protein" is not a direction in
  embedding space, and the right bar may not be in the top 5 at all.
- "Without X" is the hardest case. The text "no seed oils" sits close to
  documents that mention oils.
- An LLM filtering 5 candidates can only drop wrong ones. It cannot find
  the right ones that were never retrieved.

So the representation has to support **exact filters** first and
**semantic ranking** second.

---

## 3. The canonical product record

Each OFF product is normalized into one record (`off_products/normalize.py`)
with four layers.

### 3.1 Identity and facets (from OFF tags)

```jsonc
{
  "code": "0000000000011",
  "name": "Chocolate Almond Protein Bar",
  "brand": "Trailhead Test Co",
  "quantity": "12 x 60 g",
  "categories": ["en:snacks", "en:sweet-snacks", "en:bars", "en:protein-bars"],  // includes ancestors
  "main_category": "en:protein-bars",
  "labels": ["en:gluten-free"],
  "allergens": ["en:milk", "en:nuts"], "traces": [],
  "countries": ["en:united-states"],
  "additives": [],
  "scores": {"nova_group": 3, "nutriscore_grade": "c", "environmental_score_grade": null}
}
```

Keep facets as **taxonomy ids** (`en:protein-bars`), not display strings. Ids
are language-independent and already include ancestors, so a filter on
`en:crisps` also matches `en:potato-crisps`.

### 3.2 Nutrition: both bases, standard units

```jsonc
"nutrition": {
  "basis_on_label": "serving",
  "serving_size": "1 bar (60 g)", "serving_g": 60,
  "per_100g":    {"energy_kcal": 380, "protein_g": 35, "sugars_g": 12, "fiber_g": 8, "sodium_mg": 300, ...},
  "per_serving": {"energy_kcal": 228, "protein_g": 21, "sugars_g": 7.2, "fiber_g": 4.8, "sodium_mg": 180, ...}
}
```

- People say "20 g protein" about **one bar or bag**, but compare densities
  per 100 g. Store both. When OFF lacks `_serving`, derive it from `_100g` ×
  `serving_quantity` / 100.
- Use OFF's normalized `<nutrient>_100g` / `_serving` values, never
  `_value` / `_unit`, which are whatever unit the contributor typed.
- Add derived ratios people ask about, e.g. `protein_kcal_pct` (share of
  calories from protein).

### 3.3 Ingredients: structure and amounts

```jsonc
"ingredients": {
  "text": "protein blend (whey protein isolate, milk protein isolate), almonds, dates, cocoa, sea salt",
  "count_top_level": 5,      // what the label shows at top level
  "count_total": 6,          // leaf ingredients including sub-ingredients
  "first": "en:milk-protein-blend",
  "tags": ["en:whey-protein-isolate", "en:milk-proteins", "en:dairy", "en:almond", ...],  // with ancestors
  "items": [
    {"id": "en:almond", "text": "almonds", "depth": 0, "rank": 2,
     "percent": null, "percent_min": 20, "percent_max": 35, "percent_estimate": 25}
  ],
  "vegan": false, "vegetarian": true, "palm_oil_free": true
}
```

This makes three kinds of question answerable:

- **Include or exclude an ingredient**: match on `tags`. Ancestors are
  included, so excluding `en:sunflower-oil` also excludes high-oleic
  sunflower oil.
- **Ingredient amount** ("> 70% cocoa", "≥ 5 g almonds per serving"): OFF
  gives a declared `percent` when the label prints one, and otherwise an
  estimated range (`percent_min`/`percent_max`) and point estimate. Store one
  row per ingredient with `percent`, `percent_min`, `percent_max`,
  `grams_per_100g`, `grams_per_serving` and a `declared` flag, so
  the app can choose strict ("label says ≥ 70%") or estimated matching.
- **Simplicity and ordering**: ingredient count ("minimal ingredients") and
  first ingredient ("nuts as the first ingredient").

### 3.4 Derived ingredient groups ("free of X")

Users ask about **groups** that OFF doesn't tag: seed oils, artificial
sweeteners, added sugars, gums, artificial colors, natural flavors. These are
defined as data in `off_products/concepts.py`, each a list of OFF taxonomy
ids. Adding a group means adding a list of ids, not writing new code:

| Group | Built from |
|---|---|
| `seed_oils` | canola, rapeseed, corn, cottonseed, soy, sunflower, safflower, grapeseed, rice-bran oil |
| `palm_oil` | palm, palm kernel oil |
| `hydrogenated_oils` | any id containing `hydrogenated` |
| `added_sugars` | sugar, cane/brown sugar, dextrose, honey, maple/glucose/corn/HFCS syrups, any `-syrup` |
| `artificial_sweeteners` | E950 acesulfame K, E951 aspartame, E954 saccharin, E955 sucralose, E961, E962 |
| `sugar_alcohols` | E420, E965–E968 |
| `gums_thickeners` | E407 carrageenan, E410, E412 guar, E414, E415 xanthan, E418, E466 |
| `artificial_colors` | E102, E110, E122, E124, E129, E133 |
| `artificial_flavors` / `natural_flavors` | `en:artificial-flavouring` / `en:natural-flavouring` |
| `synthetic_preservatives` | E202, E211, E249, E250, E320, E321 |

Each product gets a **four-state status** per group:

| Status | Meaning |
|---|---|
| `contains` | A matching ingredient or additive is listed. |
| `possible` | Something ambiguous could match: "vegetable oil" with no type, an oil OFF couldn't recognize, or "flavouring" with no qualifier. |
| `none` | Ingredients are known and nothing matched. |
| `unknown` | No ingredient data. |

The four states matter. A product with no ingredient list must not come back
for "free of X". About this the record has to be honest; a missing list is
not a clean one.

### 3.5 Quality and ranking signals

`completeness`, `unique_scans_n` (popularity), `data_quality_errors_tags`,
`last_modified_t`, `obsolete`. Use them to drop obsolete or broken records
and to rank well-known products first.

### 3.6 The embedding document

A short text built from name, brand, main category, labels, ingredient text
and oils (`search_text`). It exists to match **soft intent** like "chocolate
peanut butter" or "crunchy" or "kid-friendly". It is not meant to carry numbers.

---

## 4. Storage: three views of the same record

1. **Canonical JSON** (`record`): what the answering LLM reads and quotes.
2. **Flat filter row** (`to_flat`): scalar columns (`protein_g_serving`,
   `sugars_g_100g`, `ingredients_n`, `first_ingredient`, `nova_group`, …) plus
   tag lists (`category_tags`, `ingredient_tags`, `label_tags`,
   `allergen_tags`, `free_of`, `contains_groups`, `oil_tags`). This maps
   directly to SQL columns plus a tags table, or to **Pinecone metadata**
   (`to_pinecone_metadata`, which drops nulls because Pinecone rejects them).
3. **Ingredient amounts table** (`product_ingredients`): one row per product
   and ingredient. Flat vector-DB metadata can't express "≥ 30% almonds", so
   with Pinecone you over-fetch and post-filter with
   `matches_ingredient_amounts`. With SQL or DuckDB it is a join.

A relational store (DuckDB or Postgres with pgvector) can hold all three and do
filter and vector ranking in one query. Pinecone works for views 1 and 2 with
a post-filter for view 3. The reference code uses SQLite so it runs anywhere.

---

## 5. Query pipeline

```
question
  │ 1. LLM tool call with QUERY_SPEC_SCHEMA  (off_products/query.py)
  ▼
QuerySpec {semantic_query, categories_any, nutrients[], ingredient_amounts[],
           include/exclude_ingredients, exclude_groups, include_groups,
           first_ingredient_any, max_ingredients, labels_all, exclude_allergens,
           countries_any, max_nova_group, sort_by, limit}
  │ 2. hard filters: to_sql()  or  to_pinecone_filter() + matches_ingredient_amounts()
  ▼
candidates that satisfy every constraint
  │ 3. rank: embedding similarity to semantic_query, then popularity/completeness
  ▼
top N records (canonical JSON)
  │ 4. LLM writes the answer, citing the stored numbers, and states caveats
  ▼     (e.g. "estimated %", "per 60 g bar")
answer
```

Example mappings (see `tests/test_pipeline.py`):

| Question | QuerySpec |
|---|---|
| protein bar, ≥ 20 g protein, no seed oils | `categories_any=[en:protein-bars]`, `nutrients=[protein_g serving >= 20]`, `exclude_groups=[seed_oils]` |
| potato chips, very minimal ingredients, avocado oil | `categories_any=[en:potato-crisps]`, `include_ingredients_all=[en:avocado-oil]`, `max_ingredients=4`, `sort_by=ingredients_n` |
| more than 10 g protein | `nutrients=[protein_g serving > 10]` |
| dark chocolate > 70% cocoa | `ingredient_amounts=[en:cocoa-mass percent > 70]` |
| ≥ 4 g hazelnuts per serving, label-declared | `ingredient_amounts=[en:hazelnut grams_per_serving >= 4, declared_only]` |

**Mapping words to ids.** Give the parsing LLM a lookup tool over the
taxonomies (synonym → id, e.g. "canola" → `en:canola-oil`, "potato chips" →
`en:potato-crisps`) instead of trusting it to guess ids. If a hard filter
returns nothing, relax it in a fixed order (category first, then a soft
constraint) and tell the user what was relaxed.

---

## 6. Caveats to design around

- **Coverage varies.** Many products lack ingredient lists, serving sizes or
  categories. Filter on `completeness` or country, and never treat missing as
  "free of".
- **Percentages are mostly estimates.** Only some labels declare amounts.
  Expose `declared` and say "estimated" in answers.
- **Category tagging is incomplete.** If a category filter returns too
  little, fall back to a semantic match on the product type.
- **Group definitions are opinions.** Whether peanut or sesame oil is a
  "seed oil", or whether E471 counts, depends on the user. Keep the
  groups in config and mention the definition in answers.
- **Per-serving values depend on `serving_quantity`**, which is sometimes
  missing or wrong. Fall back to per-100 g and say so.

---

## 7. Code in this repo

| File | Purpose |
|---|---|
| `off_products/normalize.py` | Raw OFF (JSONL or Parquet row) → canonical record, flat filter row, Pinecone metadata, ingredient amounts |
| `off_products/concepts.py` | Ingredient-group definitions and four-state classification |
| `off_products/query.py` | `QuerySpec`, LLM tool schema, compilers to SQL and Pinecone filters, amount post-filter |
| `off_products/store.py` | Streaming reader for `.jsonl`, `.jsonl.gz` and `.parquet`; SQLite store |
| `tests/` | Synthetic OFF-shaped fixtures and tests covering the example questions |

```bash
python -m off_products build food.parquet products.db --country en:united-states
python -m off_products query products.db '{"semantic_query":"protein bar",
  "categories_any":["en:protein-bars"],
  "nutrients":[{"nutrient":"protein_g","basis":"serving","op":">=","value":20}],
  "exclude_groups":["seed_oils"]}'
```
