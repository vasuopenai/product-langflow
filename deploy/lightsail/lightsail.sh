#!/usr/bin/env bash
# Deploy the food services to one AWS Lightsail server, from your PC (Git Bash on Windows,
# macOS or Linux). Settings come from deploy/lightsail/target.env (git-ignored).
# First time, in order (README.md walks through it):
#   lightsail.sh setup         Docker, swap, firewall and nightly backups on the server
#   lightsail.sh env           server .env: keys from your local .env plus generated secrets
#   lightsail.sh deploy        ship the committed code, build, start, check https://API_DOMAIN
#   lightsail.sh push-db       copy your local `food` database to the server (--replace to overwrite)
#   lightsail.sh push-refs     copy the catalog gap finder's USDA/OFF barcode lists (docs/CATALOG_GAPS.md)
# Day to day:
#   lightsail.sh deploy | status | logs [service] | backup | keys | tunnel | ssh
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
if [ ! -f "$HERE/target.env" ]; then
  echo "Copy deploy/lightsail/target.env.example to deploy/lightsail/target.env and fill it in." >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$HERE/target.env"
: "${LIGHTSAIL_HOST:?set in target.env}" "${LIGHTSAIL_KEY:?set in target.env}" "${API_DOMAIN:?set in target.env}"

APP=/opt/food
COMPOSE="docker compose -f docker-compose.yml -f deploy/lightsail/docker-compose.prod.yml"
SSH_OPTS=(-i "$LIGHTSAIL_KEY" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)
remote() { ssh "${SSH_OPTS[@]}" "ubuntu@$LIGHTSAIL_HOST" "$@"; }
in_app() { remote "cd $APP && $*"; }

# A key's value from your local .env, without printing it.
local_value() { grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
# A key's value from the server's .env (empty if none).
server_value() { remote "grep -E '^$1=' $APP/.env 2>/dev/null | tail -1 | cut -d= -f2-" || true; }

health() {
  echo "== waiting for https://$API_DOMAIN/api/health (the first HTTPS certificate takes ~30 s)"
  for _ in $(seq 1 30); do
    if curl -fsS -m 10 "https://$API_DOMAIN/api/health"; then echo; return 0; fi
    sleep 5
  done
  echo "No answer. Check the DNS A record, the Lightsail firewall (HTTPS 443) and: lightsail.sh logs caddy" >&2
  return 1
}

cmd=${1:-help}
shift || true
case "$cmd" in
  setup)
    remote 'sudo bash -s' < "$HERE/server-setup.sh"
    ;;

  env)
    # Secrets are generated once and kept on later runs: a new database password would not
    # match the existing database, and a new app key would lock out the published app.
    pg=$(server_value POSTGRES_PASSWORD); app=$(server_value MOBILE_APP_KEY); admin=$(server_value MOBILE_ADMIN_KEY)
    [ -n "$pg" ] || pg=$(remote 'openssl rand -hex 24')
    [ -n "$app" ] || app=$(remote 'openssl rand -hex 16')
    [ -n "$admin" ] || admin=$(remote 'openssl rand -hex 24')
    # A key missing from the local .env keeps the server's current value (never silently dropped);
    # a required key in neither place stops here, before anything is written.
    current=$(remote "cat $APP/.env 2>/dev/null" || true)
    value() {
      local v
      v=$(local_value "$1")
      if [ -z "$v" ]; then
        v=$(printf '%s\n' "$current" | grep -E "^$1=" | tail -1 | cut -d= -f2-)
        if [ -n "$v" ]; then echo "Note: $1 is not in your local .env; keeping the server's value." >&2; fi
      fi
      printf '%s' "$v"
    }
    for k in OPENAI_API_KEY KROGER_CLIENT_ID KROGER_CLIENT_SECRET; do
      if [ -z "$(value "$k" 2>/dev/null)" ]; then
        echo "Error: $k is in neither your local .env nor the server's; add it to .env. Nothing written." >&2
        exit 1
      fi
    done
    remote "cp -p $APP/.env $APP/.env.bak 2>/dev/null || true"  # the previous settings, just in case
    {
      echo "# Written by deploy/lightsail/lightsail.sh env on $(date -u +%F). Change keys in your local"
      echo "# .env and run it again; generated secrets below are kept."
      echo "API_DOMAIN=$API_DOMAIN"
      echo "APP_DB=food"
      echo "MOBILE_BIND=127.0.0.1"
      echo "POSTGRES_PASSWORD=$pg"
      echo "MOBILE_APP_KEY=$app"
      echo "MOBILE_ADMIN_KEY=$admin"
      for k in OPENAI_API_KEY OFF_CHAT_MODEL KROGER_CLIENT_ID KROGER_CLIENT_SECRET KROGER_API_BASE \
               KROGER_MAX_CALLS_PER_DAY USDA_API_KEY WEB_SEARCH_MODEL MOBILE_HOURLY_LIMIT SUPPORT_EMAIL \
               COMPOSE_PROFILES CATALOG_KROGER_CALLS_PER_DAY CATALOG_SECONDS_PER_CALL CATALOG_RESEARCH_PER_DAY \
               CATALOG_RECRAWL_DAYS; do
        v=$(value "$k")
        if [ -n "$v" ]; then echo "$k=$v"; fi
      done
    } | remote "mkdir -p $APP && umask 077 && cat > $APP/.env"
    echo "Wrote $APP/.env on the server (values not shown; the previous one is .env.bak)."
    echo "The app and admin keys: lightsail.sh keys"
    ;;

  deploy)
    git -C "$ROOT" diff --quiet HEAD || echo "Note: uncommitted changes are not deployed; commit them first."
    if [ -z "$(server_value POSTGRES_PASSWORD)" ]; then echo "No .env on the server yet: run lightsail.sh env first." >&2; exit 1; fi
    sha=$(git -C "$ROOT" rev-parse HEAD)
    echo "== shipping ${sha:0:7} ($(git -C "$ROOT" rev-parse --abbrev-ref HEAD))"
    git -C "$ROOT" archive --format=tar HEAD | gzip | remote "mkdir -p $APP && tar -xzf - -C $APP && echo $sha > $APP/DEPLOYED_COMMIT"
    echo "== building and starting"
    in_app "$COMPOSE up -d --build --remove-orphans && docker image prune -f > /dev/null"
    health
    ;;

  push-db)
    replace=${1:-}
    if [ "$(server_value POSTGRES_PASSWORD)" = "" ]; then echo "Run lightsail.sh env and deploy first." >&2; exit 1; fi
    exists=$(in_app "$COMPOSE up -d db > /dev/null 2>&1; for i in \$(seq 1 30); do $COMPOSE exec -T db pg_isready -U postgres -q && break; sleep 2; done; $COMPOSE exec -T db psql -U postgres -tAc \"select 1 from pg_database where datname='food'\"" | tr -d '[:space:]')
    if [ "$exists" = 1 ] && [ "$replace" != "--replace" ]; then
      echo "The server already has a food database. To overwrite it (including products approved" >&2
      echo "on the server since), run: lightsail.sh push-db --replace" >&2
      exit 1
    fi
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    echo "== dumping your local food database (several minutes)"
    (cd "$ROOT" && docker compose exec -T db pg_dump -U postgres -Fc -Z 6 food) > "$tmp/food.dump"
    echo "   dump is $(du -h "$tmp/food.dump" | cut -f1)"
    echo "== uploading (the slow part: about 1 hour per 8 GB at 20 Mbit/s upload)"
    scp "${SSH_OPTS[@]}" "$tmp/food.dump" "ubuntu@$LIGHTSAIL_HOST:/opt/food-backups/upload-food.dump"
    echo "== restoring on the server (rebuilds the vector index; 10-30 minutes)"
    # The app services create their own (empty) tables on startup; keep them stopped until the
    # restore is done so it starts from an empty database.
    in_app "$COMPOSE stop api kroger wholefoods mobile"
    if [ "$exists" = 1 ]; then
      in_app "$COMPOSE exec -T db dropdb -U postgres food"
    fi
    in_app "$COMPOSE exec -T db createdb -U postgres food \
      && $COMPOSE cp /opt/food-backups/upload-food.dump db:/tmp/food.dump \
      && { $COMPOSE exec -T db pg_restore -U postgres -d food --no-owner -j 2 /tmp/food.dump \
           || echo 'pg_restore reported warnings; check the product count below'; } \
      && $COMPOSE exec -T db rm -f /tmp/food.dump"
    count=$(in_app "$COMPOSE exec -T db psql -U postgres food -tAc 'select count(*) from products'" | tr -d '[:space:]')
    echo "products on the server: $count"
    # Keep the upload until the restore is known good, so a retry needs no new upload.
    if [ "${count:-0}" -gt 0 ]; then in_app "rm -f /opt/food-backups/upload-food.dump"; fi
    in_app "$COMPOSE up -d"
    health
    ;;

  push-refs)
    # The catalog gap finder's barcode reference lists (full USDA and Open Food Facts), built
    # locally with `python -m catalog_gaps build-refs`; a small upload instead of 9 GB of sources.
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    echo "== dumping catalog.usda_codes, catalog.off_codes, catalog.refs"
    (cd "$ROOT" && docker compose exec -T db pg_dump -U postgres -Fc -Z 6 \
       -t catalog.usda_codes -t catalog.off_codes -t catalog.refs food) > "$tmp/refs.dump"
    echo "   dump is $(du -h "$tmp/refs.dump" | cut -f1)"
    scp "${SSH_OPTS[@]}" "$tmp/refs.dump" "ubuntu@$LIGHTSAIL_HOST:/opt/food-backups/upload-refs.dump"
    in_app "$COMPOSE exec -T db psql -U postgres food -qc 'CREATE SCHEMA IF NOT EXISTS catalog' \
      && $COMPOSE cp /opt/food-backups/upload-refs.dump db:/tmp/refs.dump \
      && $COMPOSE exec -T db pg_restore -U postgres -d food --clean --if-exists --no-owner /tmp/refs.dump \
      && $COMPOSE exec -T db rm -f /tmp/refs.dump && rm -f /opt/food-backups/upload-refs.dump"
    in_app "$COMPOSE exec -T db psql -U postgres food -c 'SELECT name, rows, built_at FROM catalog.refs'"
    ;;

  status)
    in_app "$COMPOSE ps --format 'table {{.Service}}\t{{.Status}}'"
    echo "deployed commit: $(remote "cut -c1-7 $APP/DEPLOYED_COMMIT 2>/dev/null")"
    curl -fsS -m 10 "https://$API_DOMAIN/api/health" && echo
    ;;

  logs)
    in_app "$COMPOSE logs --tail 200 -f ${1:-mobile}"
    ;;

  backup)
    remote "bash $APP/deploy/lightsail/backup.sh && ls -lh /opt/food-backups"
    ;;

  keys)
    # For the app build (EXPO_PUBLIC_APP_KEY) and the review UI (admin key). Shown on request only.
    remote "grep -E '^(MOBILE_APP_KEY|MOBILE_ADMIN_KEY)=' $APP/.env"
    ;;

  tunnel)
    echo "Review UI: leave this running, then start Streamlit with MOBILE_API_URL=http://127.0.0.1:9003"
    echo "and the admin key from 'lightsail.sh keys'. Ctrl+C closes the tunnel."
    ssh "${SSH_OPTS[@]}" -N -L 9003:127.0.0.1:8003 "ubuntu@$LIGHTSAIL_HOST"
    ;;

  ssh)
    ssh "${SSH_OPTS[@]}" "ubuntu@$LIGHTSAIL_HOST"
    ;;

  *)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
