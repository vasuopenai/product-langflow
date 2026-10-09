---
name: local-setup
description: Set up, load and test the Open Food Facts product search locally (Docker Postgres + pgvector, Python venv, data load, API, answer-quality checks). Use when the user asks to set up, load data, run or test this project on their machine.
disable-model-invocation: true
arguments: [stage]
---

# Local setup and test run

Drive the local pipeline end to end on the user's machine, following
`docs/WINDOWS_LOCAL.md` (Windows) or `docs/DEPLOY.md` steps 1–7 (macOS/Linux).
Requested stage: **$ARGUMENTS** (empty means `smoke`).

| Stage | Does | Costs money? |
|---|---|---|
| `smoke` | check tools, install, unit + Postgres tests, offline smoke test | no |
| `small` | download data if missing, load 5,000 real products, ask the example questions, verify | yes, small |
| `full` | load the full country with `--hnsw` | yes, largest |
| `api` | start the API container and test `/health`, `/search`, `/ask` | yes, small |
| `verify` | re-run the example questions and check answers against filters | yes, small |
| `kroger` | find a store, crawl Kroger products, match to USDA and the store, report coverage | no (Kroger API is free; uses daily quota) |

Run every stage before the requested one that hasn't passed yet. Stop at the
first failure, diagnose it, fix what is clearly local (missing install, stopped
container, wrong path), and ask the user about anything else.

## Ground rules

- **Spending:** before any command that calls OpenAI (`pg-load` without
  `--embedder fake`, `ask`, `/ask`, `/search` with the openai embedder), state
  what it will do and how many products or questions are involved, then wait for
  the user's go-ahead. For `full`, first report the cost of the `small` load
  (ask the user to read it from their OpenAI usage page) and an extrapolation.
- **Secrets:** never print, echo or commit `.env` or the API key. Check that a
  key is set with `.venv/Scripts/python -c "import os; from off_products._env import load_dotenv; load_dotenv(); print(bool(os.getenv('OPENAI_API_KEY')))"`.
  If it's missing, ask the user to put it in `.env` themselves. Don't ask them to
  paste it into the chat.
- **Shell:** on Windows, commands run in Git Bash (or PowerShell when Git
  isn't installed). Call the venv's interpreter directly so no activation is
  needed: `.venv/Scripts/python` on Windows, `.venv/bin/python` elsewhere.
  Written as `PY` below.
- **Destructive commands:** `DROP TABLE` and `docker compose down -v` delete
  loaded data. Only use them where a stage below says so, and say what they
  remove.
- Never mix fake and real embeddings in one database: the `smoke` stage wipes
  its test rows before finishing.

## Stage: smoke

1. Tools: `python --version` (3.10+), `docker version` (the Server section must be
   present; if not, ask the user to start Docker Desktop), `git --version`.
2. venv and deps: create `.venv` if missing (`python -m venv .venv`), then
   `PY -m pip install -r requirements.txt`.
3. `.env`: if missing, copy `.env.example` to `.env` and tell the user to add
   `OPENAI_API_KEY` before the `small` stage.
4. Unit tests: `PY -m pytest -q tests` → expect `12 passed, 4 skipped`.
5. Database: `docker compose up -d db`. Poll `docker compose exec -T db pg_isready -U postgres`
   until ready (up to about 60 s).
6. Postgres tests: create `off_test` if missing
   (`docker compose exec -T db psql -U postgres -c "CREATE DATABASE off_test"`, ignore
   "already exists"), then run pytest with
   `OFF_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/off_test` → expect `16 passed`.
7. Offline smoke test:
   `PY -m off_products pg-load tests/fixtures/usda --source usda --embedder fake`
   (expect `loaded 4`), then for each `examples/*.json`:
   `PY -m off_products pg-search @examples/<file> --embedder fake`. Expected top hits:
   protein_bar → Chocolate Almond Protein Bar, avocado_oil_chips → Avocado Oil Sea Salt
   Kettle Chips, dark_chocolate → 85% Dark Chocolate.
8. Wipe the test rows:
   `docker compose exec -T db psql -U postgres off -c "DROP TABLE IF EXISTS products, product_tags, product_ingredients"`.

## Stage: small

1. Confirm `OPENAI_API_KEY` is set (see Secrets).
2. Data: if `data/food.parquet` is missing, download it with
   `curl -L -o data/food.parquet https://huggingface.co/datasets/openfoodfacts/product-database/resolve/main/food.parquet`
   (several GB; `curl.exe` in PowerShell). If the URL fails, tell the user to fetch
   `food.parquet` from the `openfoodfacts/product-database` dataset page.
3. Ask for go-ahead, then
   `PY -m off_products pg-load data/food.parquet --country en:united-states --limit 5000`.
   Use another `--country` if the user named one. Run it in the background and
   report progress lines.
4. Then do the `verify` stage on `examples/questions.txt`.
5. Ask the user to check their OpenAI usage page and report the cost of this run.

## Stage: full

Only after `small` passed and the user has agreed to the extrapolated cost:
`PY -m off_products pg-load data/food.parquet --country <same> --hnsw` in the background.
It is safe to re-run if interrupted (upserts by barcode). Report progress every
few minutes. When it finishes, run `verify`.

## Stage: api

1. `docker compose up -d --build api`. The compose file reads `OPENAI_API_KEY` from `.env`.
2. `curl -s localhost:8000/health` → `ok: true` with the product count.
3. After go-ahead, POST one question from `examples/questions.txt` to `/ask` and
   show the answer.

## Stage: kroger

Follow `docs/KROGER.md`. Needs `KROGER_CLIENT_ID` and `KROGER_CLIENT_SECRET`
in `.env` (check presence the same way as the OpenAI key; never print them).

1. Ask the user for a ZIP code, run `PY -m off_products kroger-locations --zip <zip>`,
   and let them pick a store.
2. After go-ahead (it runs for a while and uses the day's Kroger quota):
   `PY -m off_products kroger-crawl --location <id>` in the background. It is
   resumable; if it stops at the daily budget, say so and stop.
3. Ask the user for the path of their USDA dump, then
   `PY -m off_products usda-index "<path>"`.
4. `PY -m off_products kroger-match` (tags products in the Postgres store from
   `DATABASE_URL`). Show the coverage table and explain it: share of Kroger
   products already searchable ("in store"), share with labels only in USDA or
   Kroger, and share with no label anywhere. Point to `data/kroger/coverage.csv`.

## Stage: verify (answer-quality check)

For each line in `examples/questions.txt` (after go-ahead; each question is two
small chat calls plus one embedding):

1. `PY -m off_products ask "<question>" --json`
2. Check that the `spec` faithfully reflects the question: right nutrient and
   basis (per serving vs per 100 g), right category and ingredient ids, the
   right `exclude_groups`, and no invented constraints.
3. Check every returned product against the spec using its own fields:
   `per_serving` / `per_100g`, `ingredient_count`, `ingredient_amounts`,
   `groups` (any listed group has status contains, possible or unknown), and
   `ingredients` text.
4. Check the answer only names returned products and quotes their numbers
   correctly, and says "estimated" for non-declared percentages.

Finish with a table: question | parsed filters OK? | products returned | all
satisfy filters? | answer faithful? | notes. For each failure, name the likely
cause (parse prompt, missing taxonomy id, data coverage, group definition) and
propose a fix, but don't change code unless the user asks.
