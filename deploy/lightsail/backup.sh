#!/usr/bin/env bash
# Dump the food database to /opt/food-backups and keep the newest KEEP dumps (default 3).
# Runs nightly from cron on the server (see server-setup.sh); `lightsail.sh backup` runs it now.
set -euo pipefail
APP=/opt/food
OUT=/opt/food-backups
KEEP=${KEEP:-3}
cd "$APP"
COMPOSE="docker compose -f docker-compose.yml -f deploy/lightsail/docker-compose.prod.yml"

file="$OUT/food-$(date -u +%Y%m%d-%H%M).dump"
echo "$(date -u +%FT%TZ) backing up to $file"
$COMPOSE exec -T db pg_dump -U postgres -Fc -Z 6 food > "$file.part"
mv "$file.part" "$file"
ls -1t "$OUT"/food-*.dump | tail -n +$((KEEP + 1)) | xargs -r rm --
echo "$(date -u +%FT%TZ) done: $(du -h "$file" | cut -f1)"
