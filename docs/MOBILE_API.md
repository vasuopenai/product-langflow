# Mobile app gateway (mobile_api)

One API for the phone app, on port 8003 (`docker compose up -d --build mobile`,
interactive docs at http://127.0.0.1:8003/docs). It reuses the product store,
the Q&A pipeline and the Kroger client; API keys stay on the server.

| Endpoint | What it does |
|---|---|
| `GET /api/health` | product count, Kroger keys present |
| `GET /api/nearby?lat=&lng=` | Kroger-family stores nearby with distances; `at_store` when the nearest is within `AT_STORE_METERS` (200 m) |
| `POST /api/ask {question, location_id?}` | answer, filters, notes and product cards, each with Kroger price/aisle/stock at that store |
| `POST /api/web {query}` | products from the web (OpenAI web search), cached 24 h |
| `POST /api/scan {barcode, lat?, lng?, location_id?}` | in the store: the product. Not yet: research starts, `status: researching` |
| `GET /api/scan/{barcode}` | poll: `researching`, `pending` (with what was found), `not_found`, `found` once approved |
| `GET /api/products/{barcode}?location_id=` | product detail |

Headers: `X-App-Key` (required when `MOBILE_APP_KEY` is set — set it when hosted),
`X-Client-Id` (an install id, for rate limits and scan logs). Paid calls (questions,
web searches, barcode research) are limited to `MOBILE_HOURLY_LIMIT` (60) per client.

## Scanned barcodes we don't have

1. Research, best source first: USDA FoodData Central live API (`USDA_API_KEY`,
   default `DEMO_KEY`; get a free key at https://api.data.gov/signup/), Open Food
   Facts, Kroger, then OpenAI web search (only when USDA doesn't have it; it is told
   what the other sources think the product is, and asked for evidence and links).
2. Merged field by field (USDA > Open Food Facts > web > Kroger) into
   `staging.scanned_products`, with each field's source, the sources' raw answers and
   links, a confidence, and the scan count. Every scan is logged in `staging.scan_events`.
3. Review (`/admin/review...`, the Streamlit "Review" tab; `X-Admin-Key` when
   `MOBILE_ADMIN_KEY` is set): edit, research again, approve or reject.
4. Approve converts the draft to a USDA-style row and adds it through the normal
   loader path (same parsing, categories, nutrition checks, embedding). The product
   is searchable at once, keeps `origin: scan` and its source links, and survives
   `pg-rederive`.

About a quarter of popular US products in Open Food Facts aren't in USDA's branded
data (for example much of Kirkland Signature), which is what this flow fills in.
