# Commercial-grade food label data for Whole Foods, Kroger and Costco

Research date: 2026-10-07. Prices are from vendor pages or third-party
listings found by web search. Several vendor and retailer sites were not
reachable from the research environment, so treat every price as a starting
point and confirm it with the vendor. Where a figure is an estimate or
anecdote, it is marked as one.

## How label data actually flows

Retailers don't own or sell nutrition-label data. Brands create it and
publish it through **product-content syndication networks**: GS1's GDSN, run
in the US mainly by **Syndigo** (which acquired **1WorldSync** in Sept 2025,
forming a ~$3.5B company that says it powers 90% of the top 20 US retailers),
and **NIQ** (NielsenIQ, which owns Label Insight and Brandbank). Retailers
such as Kroger receive the data from those networks and show it on their
websites.

That gives two separate problems:

1. **Label data**: nutrition, ingredients, allergens, serving size, keyed by
   UPC/GTIN. You get this from the syndication networks or from datasets fed
   by them.
2. **Retailer assortment**: which UPCs Whole Foods, Kroger or Costco
   actually sell, and where. This comes from the retailers (APIs, websites)
   or from scraped datasets.

Join the two on UPC/GTIN.

## Per retailer

| Retailer | Official data access | Notes |
|---|---|---|
| **Kroger** | **Kroger Developer API**, Products API: free public tier with OAuth client credentials and **10,000 calls/day** on `/products`. Third-party clients report that Products API 1.3.0 returns `allergens`, `nutrition` (ingredient statement, serving size, nutrients) and `warnings`. A **Partner API** with more access requires a contract with Kroger. | Best official option of the three. Confirm the nutrition fields and the terms on caching and commercial use on developer.kroger.com, which wasn't reachable from here. |
| **Whole Foods** | **No public API** found. Product pages on wholefoodsmarket.com show ingredients and nutrition facts, with a disclaimer that packaging may differ. Owned by Amazon. Amazon's affiliate product API is not a nutrition source. | Use label data from syndication networks or FDC for 365-brand and national brands; assortment only via scraping or a partnership. |
| **Costco** | **No public API** found. Kirkland Signature items appear in Open Food Facts (crowdsourced). | Same as Whole Foods. Costco's online catalog differs from warehouse assortment. |

## Options by cost

### Tier 0: free

| Source | What you get | Cost | Notes |
|---|---|---|---|
| **USDA FoodData Central, Branded Foods** | About 2M US branded products from manufacturers via GDSN partners (1WorldSync, Label Insight, GS1 US): UPC, brand owner, ingredients text, label nutrients, serving size. | **$0.** Public domain (CC0); attribution requested. API key from data.gov. | Same upstream source as the paid networks. API data refreshed monthly, bulk downloads twice a year. Ingredients are raw text: no parsed ingredient tree or taxonomy tags, so this repo would need an ingredient parser. Coverage of store brands (365, Kirkland, Kroger) not verified. |
| **Open Food Facts** (current source) | Crowdsourced labels with a parsed ingredient tree, taxonomies and photos. | $0 (ODbL: share-alike on the database) | Uneven coverage and quality, but the richest structure. |
| **Kroger public API** | Kroger assortment, store availability and (reportedly) nutrition. | $0, within 10,000 calls/day | Fills the Kroger assortment gap. Check the terms before storing data in bulk. |

### Tier 1: low-cost nutrition APIs (about $15–$300/month)

| API | Listed price | Notes |
|---|---|---|
| **Edamam** Food & Grocery DB | $14 / $69 / $299 per month (100k / 750k / 5M calls) | Per-call API; check whether caching or storing is allowed. |
| **Spoonacular** | $0 / $29 / $99 / $179 per month (3k–300k requests) | About 86k grocery products; UPC lookup. Small catalog. |
| **FatSecret Platform** | Basic free; **Premier Free for startups/non-profits** (US data, barcode, allergens; attribution required); paid Premier by quote | 2.3M+ foods, 90% global barcode coverage (their claim). |
| **Chomp** | Not found; their site has a calculator | 875k+ products. |

**Caveat for this project:** the app filters across the *whole* catalog (">= 20 g
protein AND no seed oils"). That requires the data stored in your own database.
Per-call APIs whose terms forbid bulk storage or caching suit barcode lookups,
not catalog-wide filtering. Check storage rights before paying.

### Tier 2: scraped retailer datasets (assortment and prices)

| Vendor | Price found | Coverage |
|---|---|---|
| **Bright Data** Kroger dataset | from **$0.0025 per record, $250 minimum** | Kroger; Whole Foods datasets also listed (price not retrieved) |
| **Apify** Costco scraper | from **about $1 per 1,000 results** | Costco.com prices, specs, availability |
| **Apify** Instacart scraper | from **about $5 per 1,000 products** | 70+ retailers on Instacart, including Costco, Kroger and Whole Foods; dietary tags (nutrition coverage unclear) |

Legal risk: US case law (hiQ v. LinkedIn; Meta v. Bright Data, 2024) has
mostly found that scraping *public* pages isn't "unauthorized access" under
the CFAA. Retailer terms of service can still support breach-of-contract
claims, and login-walled data is riskier. Get legal advice before building a
product on scraped retailer data. Website nutrition can also lag behind
packaging.

### Tier 3: enterprise syndication (true commercial grade)

| Vendor | What | Cost |
|---|---|---|
| **Syndigo** (now including 1WorldSync, Nutritionix, ItemMaster) | Brand-submitted product content, the same data retailers receive; Nutritionix API for nutrition and UPC lookup | Quote only. Third-party estimate (Spendhound) for Syndigo platform contracts: **about $46k/yr SMB, about $252k/yr enterprise** (not specific to a data license). A developer report puts Nutritionix at **$449/month** (anecdotal). |
| **NIQ Label Insight / Brandbank** | Label transcriptions plus derived attributes (claims, diets, allergens) | Quote only |
| **Kroger Partner API** | Deeper Kroger catalog and commerce access | Contract with Kroger |

## Recommendation

1. **Now ($0):** add USDA FDC Branded Foods as a second label source next to
   Open Food Facts. Prefer FDC's manufacturer-submitted nutrition when both
   exist, and keep Open Food Facts' parsed ingredient tree and taxonomies.
   Join on UPC. Add the Kroger public API for Kroger assortment.
2. **Measure coverage:** for a sample of what Whole Foods, Kroger and Costco
   sell in your target categories (bars, chips, chocolate…), check how many
   UPCs have complete labels in FDC + OFF. That number shows whether paid data is
   worth it.
3. **Then decide:** if Whole Foods and Costco assortment is the gap, budget a
   one-time scraped dataset (hundreds of dollars) *after* legal review, or
   approach the retailers about a partnership. If label quality is the gap,
   request quotes from Syndigo/Nutritionix and NIQ with your required storage
   rights spelled out.

## Sources

- [USDA FoodData Central — inventory and update log](https://fdc.nal.usda.gov/log)
- [USDA FoodData Central — data documentation](https://fdc.nal.usda.gov/data-documentation)
- [USDA FoodData Central — API guide](https://fdc.nal.usda.gov/api-guide)
- [FoodData Central on catalog.data.gov (CC0)](https://catalog.data.gov/dataset/fooddata-central)
- [Kroger API overview (publicapi.dev)](https://publicapi.dev/kroger-api)
- [kroger-mcp (Products API 1.3.0 nutrition/allergen fields)](https://github.com/CupOfOwls/kroger-mcp)
- [Kroger API rate limits (kroger-api client)](https://github.com/CupOfOwls/kroger-api)
- [Whole Foods Market product page example](https://www.wholefoodsmarket.com/product/whole-foods-kitchens-coconut-curry-chicken-24-oz-b092rxxgg9)
- [Whole Foods customer service — product information](https://wfm.amazon.com/customer-service/topics/products)
- [Open Food Facts — Kirkland Signature example](https://world.openfoodfacts.org/product/0096619846016/almonds-kirkland-signature)
- [Edamam Food Database API](https://developer.edamam.com/food-database-api) and [Edamam plans (apis.io)](https://apis.io/plans/edamam/edamam-plans-pricing/)
- [Spoonacular plans (apis.io)](https://apis.io/plans/spoonacular/spoonacular-plans-pricing/)
- [FatSecret Platform editions](https://platform.fatsecret.com/api-editions)
- [Chomp API (public-api.org)](https://public-api.org/api/529/chomp)
- [Nutritionix API guide (Syndigo docs)](https://docx.syndigo.com/developers/docs/nutritionix-api-guide)
- [Nutritionix alternatives (publicapis.io)](https://publicapis.io/alternatives/nutritionix-api)
- [Syndigo acquires 1WorldSync (Built In Chicago)](https://www.builtinchicago.org/articles/syndigo-acquires-1worldsync-20250903)
- [Syndigo pricing estimates (Spendhound)](https://www.spendhound.com/marketplace/syndigo-pricing)
- [Bright Data Kroger dataset](https://brightdata.com/products/datasets/kroger) and [Whole Foods datasets](https://brightdata.com/products/datasets/whole-foods)
- [Apify Costco scraper](https://apify.com/datascrapers/costco-scraper) and [Apify Instacart scraper](https://apify.com/piotrv1001/instacart-scraper)
- [ZwillGen on scraping and the CFAA](https://www.zwillgen.com/alternative-data/dc-court-ruling-reduces-webscraping-risk/)
- [Web scraping legality overview, 2026 (cloro.dev)](https://cloro.dev/blog/website-scraping-legal/)
