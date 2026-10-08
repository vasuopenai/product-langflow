# Whole Foods brand products (wholefoods)

A read-only service listing the products Whole Foods Market reports to USDA under
its own brands (365 Whole Foods Market, 365 Everyday Value, Whole Foods Market,
Engine 2, Whole Catch, ...): about 3,000 products with barcodes, ingredients and
nutrition, already in the product store.

Why not scrape the website: Whole Foods has no public product API, and its
Conditions of Use include the Amazon.com Conditions of Use, which exclude
collecting product listings and prices and using robots or data-extraction tools.

- A product counts as Whole Foods brand when its USDA brand owner is Whole Foods
  Market in any spelling (`wholefoods.store.OWNER_PATTERN`); other companies with
  "whole foods" in their name are excluded.
- The set is computed at startup into `wholefoods.products` (and on `POST /refresh`,
  e.g. after a product reload). Only the product store's tables are read.
- It doesn't cover national brands sold at Whole Foods.

```
docker compose up -d --build wholefoods      # http://127.0.0.1:8002
GET  /products?q=&category=cat:snacks&brand=&limit=50&offset=0
GET  /facets       # brands and categories with counts
POST /refresh
```

The UI's **Whole Foods** tab uses it.
