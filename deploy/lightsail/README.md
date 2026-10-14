# Deploy to AWS Lightsail

One Lightsail server runs the same Docker Compose stack as your PC, plus Caddy for HTTPS:

```
iPhone app ──HTTPS──▶ Caddy (80/443) ──▶ mobile gateway ──▶ Postgres + pgvector
                     api.example.com          │                 (17 GB food database)
                                              └─▶ OpenAI, Kroger, USDA
api, kroger, wholefoods: on the server only (reach them over an SSH tunnel)
```

Cost: the 16 GB Linux plan is $84/month (4 vCPU, 320 GB SSD, 6 TB transfer); snapshots about
$0.05/GB-month; a .com domain $15/year. OpenAI usage is billed separately.

Everything below runs from **Git Bash** in the `product-langflow` folder on your PC.

## 1. Create the server (Lightsail console, about 10 minutes)

1. https://lightsail.aws.amazon.com → **Create instance**.
   - Region: **Virginia (us-east-1)**. Platform: **Linux/Unix**. Blueprint: **OS Only → Ubuntu 24.04 LTS**.
   - Plan: **General purpose, $84 (16 GB)**, dual-stack (IPv4). 8 GB ($44) works but searches
     wait on disk: the vector index and product rows (~13 GB) don't fit in memory.
   - Name: `food-genie`. **Create instance**.
2. **Networking → Create static IP**, attach it to `food-genie`. Note the IP.
3. On the instance's **Networking** tab, IPv4 firewall: keep SSH (22) and HTTP (80), **add HTTPS (443)**.
   Optional: restrict SSH to your home IP.
4. **Account → SSH keys**: download the default key for us-east-1 and move it to
   `~/.ssh/LightsailDefaultKey-us-east-1.pem` (in Git Bash: `mv ~/Downloads/Lightsail*.pem ~/.ssh/`).
5. **Snapshots** tab: turn on **automatic snapshots** (daily, kept 7 days).

## 2. Point a domain at it

Buy a domain (Lightsail **Domains & DNS → Register domain**, or any registrar) and add an
**A record**: `api` → the static IP. Check from your PC:

```bash
nslookup api.example.com          # must show the static IP before step 4
```

## 3. Tell the script where the server is

```bash
cp deploy/lightsail/target.env.example deploy/lightsail/target.env
# edit target.env: LIGHTSAIL_HOST (static IP), LIGHTSAIL_KEY (the .pem path), API_DOMAIN
```

`target.env` is git-ignored.

## 4. Set up and deploy

```bash
deploy/lightsail/lightsail.sh setup      # Docker, 4 GB swap, firewall, nightly backups (~3 min)
deploy/lightsail/lightsail.sh env        # server .env: keys from your local .env + generated secrets
deploy/lightsail/lightsail.sh deploy     # ships the committed code, builds, starts, checks HTTPS
deploy/lightsail/lightsail.sh push-db    # copies your local food database (dump, upload, restore)
```

- `env` copies `OPENAI_API_KEY`, the Kroger keys and the other settings from your local `.env`
  and generates the database password, `MOBILE_APP_KEY` and `MOBILE_ADMIN_KEY` once. Nothing is
  printed. Run it again after changing a key locally; the generated secrets are kept.
- `deploy` ships what is **committed** on your current branch (`git archive`), so commit first.
  The server builds the images; first build takes a few minutes.
- `push-db` dumps the 17 GB `food` database (several GB compressed), uploads it and restores it.
  The upload is the slow part: roughly an hour per 8 GB at 20 Mbit/s upload. It refuses to
  overwrite an existing server database unless you add `--replace`.

**Check:** `https://api.example.com/api/health` shows `{"ok":true,"products":425531,...}`.

## 5. Point the app at the server

```bash
deploy/lightsail/lightsail.sh keys       # shows MOBILE_APP_KEY and MOBILE_ADMIN_KEY
```

In `product-mobile`, create `.env` (git-ignored):

```
EXPO_PUBLIC_API_URL=https://api.example.com
EXPO_PUBLIC_APP_KEY=<MOBILE_APP_KEY>
```

Restart `npx expo start`. The iPhone now talks to AWS over HTTPS, from anywhere, without the
PC, the Wi-Fi relay or the firewall rules.

## 6. Review scanned products

The review endpoints need the admin key, and the review UI stays on your PC:

```bash
deploy/lightsail/lightsail.sh tunnel     # leave running: server's gateway on 127.0.0.1:9003
```

In another window, start the Streamlit UI with `MOBILE_API_URL=http://127.0.0.1:9003`, and enter
the admin key in the Review tab.

## Day to day

| Command | Does |
|---|---|
| `lightsail.sh deploy` | ship the latest commit and restart what changed |
| `lightsail.sh status` | containers, deployed commit, health |
| `lightsail.sh logs [service]` | follow logs (`mobile` by default; also `caddy`, `db`, …) |
| `lightsail.sh backup` | database dump now (nightly at 03:30 UTC anyway; newest 3 kept in `/opt/food-backups`) |
| `lightsail.sh ssh` | a shell on the server |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `UNPROTECTED PRIVATE KEY FILE` or `Permission denied (publickey)` | Keep the .pem under `~/.ssh` and run `chmod 600 ~/.ssh/LightsailDefaultKey-us-east-1.pem`. |
| Health check times out | `nslookup` must show the static IP; the Lightsail firewall must allow 443; then `lightsail.sh logs caddy`. |
| `set POSTGRES_PASSWORD in .env` | Run `lightsail.sh env` before `deploy`. |
| App gets 401 | `EXPO_PUBLIC_APP_KEY` must equal the server's `MOBILE_APP_KEY` (`lightsail.sh keys`). |
| Slow answers after a restart | The vector index is loading into memory; the first few searches warm it up. |
| Upload broke off | Run `push-db` again (add `--replace` if the restore had started). |

## Growing later

Move the database to Amazon RDS for PostgreSQL (pgvector is included) when traffic grows: restore
a backup there, set `DATABASE_URL` for the services, and drop the `db` service. Or resize the
Lightsail instance from a snapshot to the 16 GB plan.

## Move to a bigger plan (e.g. 8 GB -> 16 GB)

Lightsail can't resize in place; you make a new instance from a snapshot and move the static
IP to it, so the app's URL doesn't change. About 30-45 minutes, a few minutes of downtime.

1. **Snapshots** tab of `food-genie` → **Create snapshot** (e.g. `food-genie-before-16gb`).
   Wait until it says *Available* (10-20 minutes for this disk).
2. On the snapshot: **⋮ → Create new instance**. Same region and zone, plan
   **General purpose, $84 (16 GB)**. Name it `food-genie-16gb`. Create, wait for *Running*.
3. New instance → **Networking** → IPv4 firewall: add **HTTPS (443)** next to SSH and HTTP
   (firewall rules are not copied from snapshots).
4. **Networking → Static IPs** → `food-genie`'s static IP → **Detach**, then **Attach** to
   `food-genie-16gb`. The IP (and `API_DOMAIN`) stay the same, so `target.env` and the app
   need no change. The same SSH key works.
5. From the PC: `deploy/lightsail/lightsail.sh deploy` (applies the 16 GB Postgres settings in
   `docker-compose.prod.yml`), then `lightsail.sh status`.
6. When the app works for a day, **stop** and then **delete** the old instance (a stopped
   instance still costs its disk). Keep the snapshot a week, then delete it.

## Rewriting many product rows (e.g. `pg-nutriscore` on all products)

Every rewritten row is also inserted into the HNSW vector index, which is very slow for
hundreds of thousands of rows. Drop the index first and rebuild it afterwards (search is slow
until the rebuild finishes, ~30 minutes on 16 GB):

```bash
lightsail.sh ssh
cd /opt/food && C="docker compose -f docker-compose.yml -f deploy/lightsail/docker-compose.prod.yml"
$C exec -T db psql -U postgres -d food -c "DROP INDEX IF EXISTS products_embedding"
$C exec -T mobile python -u -m off_products pg-nutriscore
$C exec -T db psql -U postgres -d food -c "SET maintenance_work_mem = '6GB'" \
  -c "SET max_parallel_maintenance_workers = 3" \
  -c "CREATE INDEX products_embedding ON products USING hnsw (embedding vector_cosine_ops)" \
  -c "VACUUM ANALYZE products"
```
