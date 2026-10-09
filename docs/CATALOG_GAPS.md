# Catalog gap finder

Answers "how many of Kroger's food products are missing from USDA, from Open Food Facts, from
our app?", lists them, and adds any group to the review queue. Kroger's catalog is read only
through Kroger's official product API (no scraping).

```
1. crawl    Kroger product search, term by term (catalog_gaps/terms.txt, ~160 food terms,
            ~250 products each). Every product is stored with its barcode readings, food or
            not, and Kroger's full record (catalog.seen).
2. compare  against full barcode lists: every USDA Branded Foods barcode (catalog.usda_codes),
            every Open Food Facts barcode with its US flag (catalog.off_codes), and our app's
            products. Computed when you ask, so new lists or a reload change the numbers
            without crawling again.
3. act      report / export a CSV / stage a group for research and review.
```

While crawling, products in neither our app nor Open Food Facts are staged automatically
(status `queued`, found_via `kroger_catalog`). Any other group can be staged later from the
stored records with `stage`, with no Kroger calls.

## Setup

```bash
# Reference lists, from the downloads on your PC (about 30 s; again after new downloads):
python -m catalog_gaps build-refs --usda data/usda/branded_food.csv --off data/food.parquet
deploy/lightsail/lightsail.sh push-refs          # copy them to the server
```

Without the Open Food Facts list the crawler falls back to Open Food Facts' API (one read a second).

## Crawl

```bash
python -m catalog_gaps run                        # on your PC (DATABASE_URL from .env)
COMPOSE_PROFILES=catalog docker compose up -d --build catalog     # as a Docker service
```

On Lightsail: add `COMPOSE_PROFILES=catalog` to your local `.env`, then `lightsail.sh env` and
`lightsail.sh deploy`. It makes one Kroger call every 6 seconds, at most 2,000 a day (Kroger
allows 10,000, shared with the app's price lookups), then sleeps until the next day. Each term is
crawled again after 30 days. Stopping and starting loses nothing.

## Gaps

```bash
python -m catalog_gaps report                    # counts, USDA x OFF table, categories, brands
python -m catalog_gaps export gaps.csv --group not_in_usda [--category dairy] [--brand "simple truth"]
python -m catalog_gaps stage not_in_usda --category snacks --dry-run     # how many would be added
python -m catalog_gaps stage not_in_usda --category snacks --limit 200   # add them to the review queue
python -m catalog_gaps gaps                      # what the catalog put in staging
```

| Group | Kroger food products that are … |
|---|---|
| `not_in_usda` | not in the USDA Branded Foods database |
| `not_in_off` | not in Open Food Facts |
| `not_in_off_us` | not listed by Open Food Facts as sold in the US |
| `neither` | in neither USDA nor Open Food Facts |
| `off_not_usda` | in Open Food Facts but not USDA |
| `not_in_app` | not in our app's database |
| `usda_not_app` | in USDA but not our app (the loader skipped incomplete labels) |

Counts exclude non-food departments and in-store codes (produce PLUs, deli and meat-counter
labels), which no outside database can match; the report lists how many were set aside.

Staged products are researched from the Review tab (or set `CATALOG_RESEARCH_PER_DAY` to have the
worker research that many a day: USDA live API, Open Food Facts, Kroger, OpenAI web search; about
$0.03–0.05 each). A shopper scanning a queued barcode starts its research at once.

| Setting | Default | |
|---|---|---|
| `CATALOG_KROGER_CALLS_PER_DAY` | 2000 | Kroger calls a day for the crawl |
| `CATALOG_SECONDS_PER_CALL` | 6 | pause between calls |
| `CATALOG_RESEARCH_PER_DAY` | 0 | queued products researched a day (0 = store only) |
| `CATALOG_RECRAWL_DAYS` | 30 | crawl each term again after this many days |

Tables for your own SQL: `catalog.seen` (every Kroger product: brand, name, category, term,
barcode readings, raw record), `catalog.terms` (progress per term), `catalog.usage` (calls per day),
`catalog.usda_codes`, `catalog.off_codes`, `catalog.refs` (when each list was built).
