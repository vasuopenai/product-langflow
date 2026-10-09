# Catalog gap finder

Finds food products Kroger sells that our database doesn't have, and stores them for analysis
and review. It reads Kroger's catalog only through Kroger's official product API (no scraping).

```
Kroger product search, term by term (catalog_gaps/terms.txt, ~160 food terms, ~250 products each)
  └─ each product:  in our USDA database? ── yes ─▶ recorded, nothing to do
                    in Open Food Facts?     ── yes ─▶ recorded, nothing to do
                    non-food department?    ── yes ─▶ recorded, skipped
                    otherwise ─▶ staging.scanned_products, status "queued", found_via "kroger_catalog"
```

It runs as a slow, resumable background worker: one Kroger call every 6 seconds, at most 2,000
calls a day (Kroger allows 10,000 product calls a day, shared with the app's price lookups), then
it sleeps until the next day. Each term is crawled again after 30 days. Stopping and starting
loses nothing.

Research is **off** by default (`CATALOG_RESEARCH_PER_DAY=0`): gaps are stored only, for analysis.
Research one from the Review tab, or set a daily number to have the worker research that many
(USDA live API, Open Food Facts, Kroger, OpenAI web search; about $0.03–0.05 each).
A shopper scanning a queued barcode starts its research at once.

## Run

```bash
# On your PC, with the local Open Food Facts copy for the OFF check:
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/food \
OFF_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/off \
  python -m catalog_gaps run

# As a Docker service (local or on the server); without OFF_DATABASE_URL it uses the
# Open Food Facts API, at most one read a second:
COMPOSE_PROFILES=catalog docker compose up -d --build catalog
```

On Lightsail: add `COMPOSE_PROFILES=catalog` to your local `.env`, then
`deploy/lightsail/lightsail.sh env` and `deploy/lightsail/lightsail.sh deploy`.

## Look at the results

```bash
python -m catalog_gaps status              # terms done, products by outcome, today's usage
python -m catalog_gaps gaps --limit 50     # the staged products
python -m catalog_gaps add-terms FILE      # more search terms
```

SQL for analysis: `catalog.seen` has every product seen (brand, name, Kroger category, term,
outcome), `catalog.terms` the progress and counts per term, `catalog.usage` calls per day.

| Setting | Default | |
|---|---|---|
| `CATALOG_KROGER_CALLS_PER_DAY` | 2000 | Kroger calls a day for the crawl |
| `CATALOG_SECONDS_PER_CALL` | 6 | pause between calls |
| `CATALOG_RESEARCH_PER_DAY` | 0 | gaps researched a day (0 = store only) |
| `CATALOG_RECRAWL_DAYS` | 30 | crawl each term again after this many days |
| `OFF_DATABASE_URL` | unset | local Open Food Facts copy; unset = Open Food Facts API |
