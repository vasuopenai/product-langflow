# Kroger price, aisle and availability (kroger_sync)

A separate service that adds "sold at Kroger" information to products the app
already has. It looks products up **by barcode** in Kroger's public API (no
catalog crawl) and keeps the answers in its own `kroger` database schema.

- The product API, search, loads and `pg-rederive` are unchanged and never touch it.
- It only reads the product store (`products`, `product_tags`) to get barcodes.
- Answers are cached for `KROGER_MAX_AGE_DAYS` (default 7), including "not sold
  at Kroger", so the same barcode isn't looked up again until it is stale.

```
UI ── /ask, /search ──▶ product API (port 8000)
 └──── /items, /runs ──▶ Kroger service (port 8001) ──▶ Kroger API (by barcode)
                              └─ kroger.items cache in the same Postgres
```

## Set up once

1. Create an app at https://developer.kroger.com with the **Products** scope, and
   put the keys in `.env`:
   ```
   KROGER_CLIENT_ID=...
   KROGER_CLIENT_SECRET=...
   ```
   Read Kroger's terms on storing data and commercial use; bulk or commercial use
   may need their Partner API.
2. Start the service: `docker compose up -d --build kroger` (http://127.0.0.1:8001).
3. In the UI's **Kroger** tab: search a ZIP code, choose the store (prices and
   aisles are per store).

## Use

- **On demand (default):** after each answer or search, the UI asks the service
  about the products shown. Unknown or stale barcodes are looked up live, 50 per
  API call, and cached. The UI shows Kroger price, sale, aisle and stock, and a
  "sold at Kroger only" toggle.
- **Load (optional, one time):** look up every product in chosen categories (or
  all) ahead of time. Stops at the daily budget (`KROGER_MAX_CALLS_PER_DAY`,
  default 9000; the public API allows about 10,000) and continues on the next run.
  425k products at 50 per call is about 8,500 calls.
- **Refresh:** look the chosen categories up again for current prices.

Same from the command line: `python -m kroger_sync locations 45202`,
`store <id>`, `load --category cat:snacks`, `refresh`, `lookup <barcode>...`,
`status`, `coverage`.

## How barcodes map

Kroger's product id is the UPC **without its check digit**, zero-padded to 13
digits (`036000291452` → `0003600029145`). USDA barcodes include the check digit,
so it is dropped; a code whose last digit isn't a valid check digit is used as is.

## Not verified yet

The request formats come from Kroger's documentation and third-party clients and
haven't been tried against the live API from this project:
- looking up up to 50 ids per call with `filter.productId=a,b,c` (if the API
  rejects lists, the client switches to one `GET /products/{id}` per barcode);
- the response fields read by `parse_product` (price, aisle, image). The full
  raw response is stored in `kroger.items.raw`, so a field can be fixed and
  re-read without calling the API again.
