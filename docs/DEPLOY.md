# Deploy and test: step by step

What you end up with:

```
Open Food Facts Parquet ──pg-load──▶ Postgres + pgvector ◀── API (/ask, /search) ◀── Langflow or any client
                         (normalize, embed)                     (OpenAI: parse question, write answer)
```

**On Windows, or to keep everything on your laptop:** follow
[WINDOWS_LOCAL.md](WINDOWS_LOCAL.md) instead of steps 1–7. It is the same flow in PowerShell.

Run the steps in order. Each one ends with a check, so you know it worked
before moving on. Steps 1–3 cost nothing and need no API key.

---

## 0. Prerequisites

- Python 3.10 or newer
- Docker Desktop (or Docker Engine with the compose plugin)
- An OpenAI API key, from step 4 on
- About 10 GB of free disk for the Open Food Facts download

> **Security first:** `search_products11.ipynb` contains an OpenAI key in a
> comment, and it is in git history. Revoke it in the OpenAI dashboard and
> create a new one. Keep keys in `.env`, which is git-ignored.

---

## 1. Get the code and run the unit tests

```bash
git clone https://github.com/vasuopenai/product-langflow.git
cd product-langflow
git checkout claude/vigilant-pascal-n9hsx5     # until it is merged

python3 -m venv .venv
source .venv/bin/activate                      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m pytest -q tests
```

**Check:** `12 passed, 4 skipped`. The 4 skipped tests need Postgres (step 2).

---

## 2. Start Postgres with pgvector locally

```bash
docker compose up -d db
docker compose ps                               # db should say "healthy"
```

Run the Postgres tests against a separate scratch database:

```bash
docker compose exec db psql -U postgres -c "CREATE DATABASE off_test"
OFF_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/off_test \
  python -m pytest -q tests
```

**Check:** `16 passed`. This covers loading, every filter type, semantic
ranking, and the full `ask` flow with a mocked OpenAI client.

---

## 3. Smoke test the whole stack offline

This loads the 11 synthetic test products using `fake` embeddings (no
OpenAI calls, no cost) and starts the API.

```bash
cp .env.example .env                            # then edit .env
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/off

python -m off_products pg-load tests/fixtures/sample_products.jsonl --embedder fake
# -> read 11 products, loaded 10   (the one without an ingredient list is skipped)

python -m off_products pg-search --embedder fake \
  '{"semantic_query":"protein bar","categories_any":["en:protein-bars"],
    "nutrients":[{"nutrient":"protein_g","basis":"serving","op":">=","value":20}],
    "exclude_groups":["seed_oils"]}'
# -> 0000000000011  Chocolate Almond Protein Bar ...  protein/serving=21.0
```

Now the API, in Docker:

```bash
OFF_EMBEDDER=fake docker compose up -d --build api
curl localhost:8000/health
# -> {"ok":true,"products":10}

curl -s -X POST localhost:8000/search -H 'content-type: application/json' \
  -d '{"semantic_query":"chocolate","ingredient_amounts":[{"ingredient":"en:cocoa-mass","basis":"percent","op":">","value":70}]}'
# -> 85% Dark Chocolate
```

Interactive API docs: http://localhost:8000/docs

**Check:** both commands return the products shown. The pipeline works end to
end. Now wipe the test rows:

```bash
docker compose exec db psql -U postgres off -c "DROP TABLE products, product_tags, product_ingredients"
```

---

## 4. Download the real data

Use the Parquet export that Open Food Facts publishes on Hugging Face (dataset
`openfoodfacts/product-database`, file `food.parquet`, refreshed daily). It is
several GB.

```bash
mkdir -p data
curl -L -o data/food.parquet \
  https://huggingface.co/datasets/openfoodfacts/product-database/resolve/main/food.parquet
```

If that URL has moved, open the dataset page on huggingface.co and download
`food.parquet` from the "Files" tab. `pg-load` also accepts the JSONL dump
(`openfoodfacts-products.jsonl.gz` from https://world.openfoodfacts.org/data).

**Check:** `ls -lh data/food.parquet` shows a multi-GB file.

---

## 5. Small real load (about 5,000 products) and first real questions

Put your OpenAI key in `.env` (`OPENAI_API_KEY=sk-...`), then:

```bash
set -a; source .env; set +a                     # loads DATABASE_URL and OPENAI_API_KEY

python -m off_products pg-load data/food.parquet \
  --country en:united-states --limit 5000
```

By default the loader skips products with no ingredient list or no nutrition
facts, because they can't satisfy any filter. Pass `--keep-incomplete` to load
them anyway.

Ask questions:

```bash
python -m off_products ask "Suggest a protein bar with minimum 20 grams protein without seed oils"
python -m off_products ask "Suggest potato chips with very minimal ingredients and avocado oil"
python -m off_products ask "snacks with more than 10 g protein and no artificial sweeteners"
python -m off_products ask "dark chocolate with more than 70% cocoa" --json   # shows the parsed filters too
```

Every answer prints the `Filters:` the LLM extracted. Check those first: if
an answer is wrong, the cause is almost always a wrong filter (wrong category
id, wrong per-serving vs per-100 g basis), not the search.

**Check:** the filters match what you asked, and every product in the
answer actually meets them. 5,000 products is a thin sample, so "no results"
is normal for narrow questions here.

---

## 6. Full load

```bash
python -m off_products pg-load data/food.parquet --country en:united-states --hnsw
```

- Repeat `--country` to add countries, or leave it out to load everything.
- `--hnsw` builds an approximate vector index at the end. Use it when you
  load hundreds of thousands of products. It needs pgvector 0.8 or newer,
  which the Docker image has.
- Cost and time are dominated by OpenAI embeddings: each product is one short
  text of roughly 100–200 tokens, using `text-embedding-3-small`. Check
  current OpenAI pricing and multiply by the product count the small load
  reported. Expect hours, not minutes, for a full country.
- Re-running the load is safe. Products are upserted by barcode, so an
  interrupted load can be restarted.

**Check:** `curl localhost:8000/health` shows the product count, and the
step 5 questions now return several good matches.

---

## 7. Test plan

Run these through `ask --json` and check the filters and results by hand. Each
line covers one capability.

| Question | What must be true |
|---|---|
| protein bar ≥ 20 g protein, no seed oils | every result: `protein_g` per serving ≥ 20; `groups` has no `seed_oils` entry |
| potato chips, very minimal ingredients, avocado oil | `ingredient_count` ≤ 4; ingredients include avocado oil |
| more than 10 g protein per 100 g | `per_100g.protein_g` > 10 (basis must be `100g`) |
| dark chocolate > 70% cocoa | top-level cocoa ingredient `percent` > 70; `declared` true, or the answer says "estimated" |
| cereal with no added sugar and at least 5 g fiber | `exclude_groups` includes `added_sugars`; fiber ≥ 5 |
| crackers without gums or artificial colors | both groups excluded |
| gluten-free granola where nuts are the first ingredient | label `en:gluten-free`; `first_ingredient_any` set |
| a made-up product type ("moon cheese bars") | notes say what was relaxed or ignored; no invented products |

For a repeatable check, save questions with their expected filters in a file and
compare `ask --json` output to it after any prompt or model change.

---

## 8. Deploy to the cloud

### 8a. Database: Supabase or Neon (both offer pgvector 0.8+)

1. Create a project.
2. Enable the `vector` extension. Supabase: Database → Extensions. Neon:
   `CREATE EXTENSION vector;` in the SQL editor. `pg-load` also tries to create
   it.
3. Copy the connection string, for example
   `postgresql://USER:PASSWORD@HOST:5432/postgres?sslmode=require`.
   On Supabase use the **session** pooler or direct connection, not the
   transaction pooler.
4. Load from your machine into the cloud database. Same command, different URL:

   ```bash
   DATABASE_URL='postgresql://...cloud...' \
     python -m off_products pg-load data/food.parquet --country en:united-states --hnsw
   ```

   Run step 5's small load against the cloud database first to confirm the connection works.

### 8b. API: any container host (Render, Railway, Fly.io, Google Cloud Run…)

The `Dockerfile` serves the API on `$PORT` (default 8000). On Render, for example:

1. New → Web Service → connect this GitHub repo and branch → Runtime: Docker.
2. Environment variables: `DATABASE_URL` (the cloud database), `OPENAI_API_KEY`.
   Optional: `OFF_CHAT_MODEL`, `OFF_EMBED_MODEL`.
3. Deploy, then:

   ```bash
   curl https://YOUR-SERVICE/health
   curl -X POST https://YOUR-SERVICE/ask -H 'content-type: application/json' \
     -d '{"question":"protein bar with at least 20 g protein and no seed oils"}'
   ```

> The API has **no authentication**, and every `/ask` call spends your OpenAI
> credits. Before sharing the URL, keep the service private, put it behind
> your host's auth or an API gateway, or add an API-key check in
> `off_products/api.py`.

`OFF_EMBED_MODEL` must be the same at load time and query time. If you change
it, reload the data.

### 8c. Connect Langflow

In your flow, replace the Pinecone, embeddings and parser chain with one
**API Request** component:

- Method `POST`, URL `https://YOUR-SERVICE/ask`
- Body `{"question": "<Chat Input text>"}`
- Send the response's `answer` field to Chat Output. `products` holds the
  structured matches if you want cards or links.

---

## 9. Keep data fresh

- **Simple:** re-download `food.parquet` weekly and re-run `pg-load`. Only
  products whose text changed need new embeddings, but the current loader
  re-embeds everything it loads, so filter with `--country` to keep cost
  down.
- **Incremental:** Open Food Facts publishes daily delta files for the last 14
  days on its /data page. They are product JSON, so
  `pg-load delta-file.json.gz` should accept them. Test one file before
  automating this.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `type "vector" does not exist` | Enable the `vector` extension on the database (8a step 2). |
| `OpenAIError: Missing credentials` | `OPENAI_API_KEY` is not set in the shell or the container. |
| `/health` OK but every answer is "no products" | Run `ask --json` and look at `spec`. Usually the category id is wrong (the notes list ignored ids) or the basis should be `100g`. |
| Good products missing from results | They may lack ingredients or nutrition (skipped at load), or OFF has no category on them. Try the question without the category. |
| Fewer results than `limit` with `--hnsw` on old pgvector | Upgrade to pgvector 0.8+, or drop the index: `DROP INDEX products_embedding`. |
| Load is slow | Embedding calls dominate. Raise `--batch-size`, narrow with `--country`, or test with `--limit`. |
