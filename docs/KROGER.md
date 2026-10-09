# Kroger: what Kroger sells, matched to your label data

This step answers two questions:

1. **Coverage:** of the products a Kroger store sells, how many already have
   label data (in your loaded store, in your USDA dump, or from Kroger itself)?
2. **Availability:** which searchable products are sold at Kroger? Questions
   like "protein bar at Kroger with 20 g protein" then filter on it, and
   answers show price and aisle.

It uses Kroger's free public Developer API, which allows about 10,000 product
calls per day.

```
Kroger API ──kroger-crawl──▶ data/kroger/products.jsonl ─┐
USDA dump ──usda-index───▶ data/usda_index.db ───────────┼─kroger-match─▶ coverage.csv
loaded products (Postgres or SQLite) ────────────────────┘                + "sold at Kroger" tags
```

Matching is by barcode. Kroger's API reportedly drops the last (check) digit of
each UPC, so the code adds it back before comparing (`off_products/gtin.py`).

---

## 1. Get Kroger API keys (free)

1. Create an account at https://developer.kroger.com.
2. Register an application. Choose the **Products** API (Locations is
   included). The redirect URL isn't used by this tool; any valid URL works.
3. Copy the **Client ID** and **Client Secret** into `.env`:

   ```
   KROGER_CLIENT_ID=your-client-id
   KROGER_CLIENT_SECRET=your-client-secret
   ```

> Read Kroger's API terms while you're there, especially anything on storing
> or caching product data and on commercial use. This tool stores the
> results. The public API is meant for app developers; bulk or commercial use
> may need Kroger's Partner API, which requires a contract.

## 2. Pick a store

Prices and aisle locations are per store.

```powershell
python -m off_products kroger-locations --zip 45202
# 01400943  KROGER     Kroger - 1014 Vine St, Cincinnati, OH, 45202
```

The list includes Kroger-family banners (Ralphs, Fred Meyer, King Soopers…),
which share the same catalog.

## 3. Crawl products

```powershell
python -m off_products kroger-crawl --location 01400943
```

- Searches each line of `examples/kroger_terms.txt` (protein bar, potato
  chips, dark chocolate…) and saves every product's full API response to
  `data/kroger/products.jsonl`.
- Each search term returns at most about 250 products, so **more specific terms give
  more coverage**. Edit the file, for example adding "keto bar", "rice
  cakes" or brand names, and run the command again. Only new terms are fetched.
- **Resumable:** progress is saved in `data/kroger/state.json`. It stops at
  `--max-calls` per day (default 9,000, under the public limit). Run it
  again the next day to continue.
- Each search page is one call, and each page returns up to 50 products.

## 4. Index your USDA dump

Point it at the folder of the **CSV** download (the one containing
`branded_food.csv`), or at the branded **JSON** file. The JSON needs
`pip install ijson`.

```powershell
python -m off_products usda-index "C:\data\FoodData_Central_branded_food_csv_2025-04-24"
# read <N> USDA records, <M> unique barcodes -> data/usda_index.db
```

This builds a small barcode index (`data/usda_index.db`) and keeps the newest record
when a barcode appears more than once. You only need to run it again for a new USDA dump.

## 5. Match and report

```powershell
python -m off_products kroger-match
```

This uses `DATABASE_URL` from `.env` to tag your Postgres product store. Use
`--sqlite products.db` for the SQLite store instead, or neither for the report only.

Example layout (the numbers are illustrative, not real results):

```
category                                  total  in store  USDA ingr  Kroger ingr  any label
ALL                                        6210      2890       4710         3020       5480
Snacks                                     1800       910       1450          880       1650
...
```

| Column | Meaning |
|---|---|
| `total` | Kroger products crawled in that Kroger category |
| `in store` | already in your loaded product store, so searchable now |
| `USDA ingr` | the USDA dump has an ingredient list for it |
| `Kroger ingr` | Kroger's own API returned an ingredient list |
| `any label` | label data exists somewhere (store, USDA or Kroger) |

`data/kroger/coverage.csv` has one row per Kroger product, with its
`label_source` (`store`, `usda`, `kroger` or `none`). Filter it to see exactly
which products are missing.

The match step also:
- tags matched products with `retailer = kroger`, so `"retailers_any": ["kroger"]`
  works in searches and the `ask` command maps "at Kroger" to it;
- fills a `retailer_items` table with price, aisle, size and fetch date, which
  answers quote.

Running it again replaces the previous Kroger tags and prices, so it is safe to repeat.

## Try it

```powershell
python -m off_products ask "protein bar at Kroger with at least 20 g protein and no seed oils"
```

## Reading the numbers

- **`in store` low, `USDA ingr` high:** the products exist in USDA but not in
  your loaded Open Food Facts data. The next step would be loading USDA
  records for those barcodes into the store.
- **`any label` low:** a real data gap. Check `Kroger ingr`: Kroger's own
  labels can fill it, within their terms.
- **Prices go stale.** Re-crawl periodically (delete `data/kroger/state.json`
  to start over), then run `kroger-match` again.

## Known limits

- The Kroger API response fields were taken from Kroger's published
  conventions and third-party clients. They could not be checked against the live API
  when this was written. The full raw response is always saved in
  `products.jsonl`, so if nutrition comes back under a different field name,
  only `parse_product` in `off_products/kroger.py` needs adjusting. Re-crawling isn't needed.
- Search-based crawling finds what your terms find. It is not a full catalog dump.
- The Pinecone filter path doesn't support the retailer filter; Postgres and SQLite do.
