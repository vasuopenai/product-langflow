# Run everything locally on Windows

Everything runs on your laptop. The only paid service is the OpenAI API, used
to embed products and to read and answer questions. Postgres + pgvector runs
in Docker Desktop and the API runs locally, so there are no hosting costs.

All commands are **PowerShell**, run from the repository folder.

---

## Let Claude Code run these steps for you

The repo includes a Claude Code skill (`.claude/skills/local-setup`) that runs
the steps below. It checks each result, fixes local problems, and stops to ask
before anything that costs money or deletes data.

```powershell
irm https://claude.ai/install.ps1 | iex        # install Claude Code (once)
cd product-langflow
claude                                         # sign in on first run
```

Then, inside Claude Code:

```
/local-setup smoke      # install + tests + offline smoke test (free)
/local-setup small      # 5,000 real products + checks example answers (asks before spending)
/local-setup full       # full load (asks first, after reporting the small load's cost)
/local-setup api        # start the API and test it
/local-setup verify     # re-check answer quality on examples/questions.txt
```

You still create `.env` and paste your OpenAI key yourself (step 1). Claude is
blocked from reading `.env` by `.claude/settings.json`.

---

## 0. Install once

| Tool | Where | Notes |
|---|---|---|
| Python 3.12 | python.org/downloads | Tick **"Add python.exe to PATH"** in the installer. |
| Git for Windows | git-scm.com | Defaults are fine. |
| Docker Desktop | docker.com/products/docker-desktop | Use the **WSL 2 backend** when asked. Free for personal use and small businesses. Restart after installing. |

Laptop: 8 GB RAM works, 16 GB is comfortable. Disk: several GB for the
download, plus the database. A rough estimate for the database is ~20 KB per
product (embedding + full record), so ~10 GB for 500,000 products.

Check in a new PowerShell window:

```powershell
python --version          # 3.10 or newer
git --version
docker version            # shows both Client and Server
```

---

## 1. Get the code and install

```powershell
git clone https://github.com/vasuopenai/product-langflow.git
cd product-langflow
git checkout claude/vigilant-pascal-n9hsx5

python -m venv .venv
.\.venv\Scripts\Activate.ps1
# If that's blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   (then run it again)

pip install -r requirements.txt
python -m pytest -q tests
```

**Check:** `12 passed, 4 skipped`.

Create your settings file. The app reads `.env` automatically, so you never
need to set environment variables by hand:

```powershell
copy .env.example .env
notepad .env
```

Leave `DATABASE_URL` as it is. Paste your OpenAI key into `OPENAI_API_KEY`
(you need it from step 4). Save the file. `.env` is git-ignored, so the key
won't be committed.

> Revoke the old OpenAI key that is in `search_products11.ipynb` and use a
> new one.

---

## 2. Start the database

```powershell
docker compose up -d db
docker compose ps          # wait until db shows "healthy"
```

Run the Postgres tests against a scratch database:

```powershell
docker compose exec db psql -U postgres -c "CREATE DATABASE off_test"
$env:OFF_TEST_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/off_test"
python -m pytest -q tests
Remove-Item Env:OFF_TEST_DATABASE_URL
```

**Check:** `16 passed`.

---

## 3. Free smoke test (no OpenAI calls)

```powershell
python -m off_products pg-load tests\fixtures\sample_products.jsonl --embedder fake
# read 11 products, loaded 10

python -m off_products pg-search "@examples\protein_bar.json" --embedder fake
python -m off_products pg-search "@examples\avocado_oil_chips.json" --embedder fake
python -m off_products pg-search "@examples\dark_chocolate.json" --embedder fake
```

**Check:** you get `Chocolate Almond Protein Bar`, `Avocado Oil Sea Salt
Potato Chips` and `85% Dark Chocolate`, one per command.

`examples\*.json` are the structured queries the LLM normally produces for you.
Copy and edit them to try other filters.

Now delete the test products. The real data uses real embeddings, and the
two kinds must not be mixed:

```powershell
docker compose exec db psql -U postgres off -c "DROP TABLE products, product_tags, product_ingredients"
```

---

## 4. Download the Open Food Facts data

```powershell
mkdir data
curl.exe -L -o data\food.parquet https://huggingface.co/datasets/openfoodfacts/product-database/resolve/main/food.parquet
```

Use `curl.exe`, not `curl`, which in PowerShell is a different command. The
file is several GB. If the URL has moved, download `food.parquet` from the
"Files" tab of the `openfoodfacts/product-database` dataset on huggingface.co.

---

## 5. Small real load, to measure cost before going big

```powershell
python -m off_products pg-load data\food.parquet --country en:united-states --limit 5000
```

Then open your OpenAI usage page and see what those 5,000 embeddings cost.
Multiply to estimate a bigger load. Embeddings use `text-embedding-3-small`
and are cheap per product, but check rather than guess.

Ask questions. Each one makes two chat calls, one to read the question and
one to write the answer:

```powershell
python -m off_products ask "Suggest a protein bar with minimum 20 grams protein without seed oils"
python -m off_products ask "Suggest potato chips with very minimal ingredients and avocado oil"
python -m off_products ask "Dark chocolate with more than 70% cocoa" --json
```

More questions to try are in `examples\questions.txt`.

Read the `Filters:` line first. If an answer is off, the cause is almost
always a wrong filter, not the search. With only 5,000 products, "nothing
found" is normal for narrow questions.

---

## 6. Bigger load

```powershell
python -m off_products pg-load data\food.parquet --country en:united-states --hnsw
```

- This can take hours, mostly waiting on embedding calls. It's safe to stop
  with Ctrl+C and run again: products are upserted by barcode.
- Keep the laptop awake. In Windows power settings, set sleep to "Never" while
  plugged in.
- Products with no ingredient list or no nutrition facts are skipped. They
  can't answer filtered questions anyway.

---

## 7. Run the API locally and try it in the browser

```powershell
docker compose up -d --build api
```

Docker reads `OPENAI_API_KEY` from your `.env`. Then open
**http://localhost:8000/docs**. Choose `POST /ask` → "Try it out" and enter:

```json
{"question": "protein bar with at least 20 g protein and no seed oils"}
```

From PowerShell instead:

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod -Method Post -Uri http://localhost:8000/ask -ContentType "application/json" `
  -Body '{"question":"potato chips with very few ingredients and avocado oil"}' | Select-Object -ExpandProperty answer
```

The API is only reachable from your own machine, so it is not exposed to anyone else.

---

## 8. Optional: Langflow, also local

Run Langflow on the laptop (`pip install langflow` in a separate venv, then
`langflow run`), and in your flow use an **API Request** component:

- `POST http://localhost:8000/ask` with body `{"question": "<chat input>"}`.
  If Langflow itself runs in Docker, use `http://host.docker.internal:8000/ask`.
- Send the `answer` field to Chat Output.

---

## Stop, restart, clean up

```powershell
docker compose stop              # stop; the data is kept
docker compose start             # start again later
docker compose down              # remove containers; the data volume is kept
docker compose down -v           # remove everything, including loaded data
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker: error during connect` | Docker Desktop isn't running. Start it and wait for "Engine running". |
| `connection refused` on port 5432 | Run `docker compose up -d db`. If another Postgres is installed locally and using 5432, change `"127.0.0.1:5432:5432"` to `"127.0.0.1:5433:5432"` in `docker-compose.yml` and the port in `.env`. |
| `OpenAIError: Missing credentials` | `OPENAI_API_KEY` is missing from `.env`, or you're not running from the repo folder. |
| Activate.ps1 is blocked | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| JSON errors with `pg-search` | Put the query in a file and pass `"@file.json"`; PowerShell mangles inline quotes. |
| Answers are all "no products" | Run `ask ... --json` and look at `spec` and `notes`. Often the category id is wrong, or the basis should be per 100 g. |

When this works and you want it hosted, `docs/DEPLOY.md` section 8 covers
moving the database and API to the cloud.
