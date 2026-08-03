#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_ROOT="${DATA_ROOT:-/srv/haltewecker/data}"
GTFS_URL="${GTFS_URL:?GTFS_URL is required}"
AUSTRIAN_GTFS_PATH="${AUSTRIAN_GTFS_PATH:-}"
AUSTRIAN_GTFS_DIR="${AUSTRIAN_GTFS_DIR:-$DATA_ROOT/austria}"
LOG_PREFIX="[StaticDepartures]"

if [[ -z "$AUSTRIAN_GTFS_PATH" && -d "$AUSTRIAN_GTFS_DIR" && ! -f "$AUSTRIAN_GTFS_DIR/.env" ]]; then
  while IFS= read -r candidate; do
    AUSTRIAN_GTFS_PATH="$candidate"
  done < <(find "$AUSTRIAN_GTFS_DIR" -maxdepth 1 -type f -name '*.zip' -print | sort)
fi

echo "$LOG_PREFIX import started at $(date -Is)"
IMPORT_ARGS=(
  --gtfs-url "$GTFS_URL"
  --stop-data "$DATA_ROOT/current"
  --next "$DATA_ROOT/staging/departures-next.sqlite"
)
if [[ -n "$AUSTRIAN_GTFS_PATH" ]]; then
  IMPORT_ARGS+=(--austrian-gtfs "$AUSTRIAN_GTFS_PATH")
elif [[ -d "$AUSTRIAN_GTFS_DIR" && -f "$AUSTRIAN_GTFS_DIR/.env" ]]; then
  IMPORT_ARGS+=(--austrian-gtfs-dir "$AUSTRIAN_GTFS_DIR" --austrian-sources "$REPO/config/austrian-sources.json")
fi
python3 "$REPO/scripts/import_static_departures_database.py" "${IMPORT_ARGS[@]}"

echo "$LOG_PREFIX atomic activation started at $(date -Is)"
VERSION="$(python3 "$REPO/scripts/swap_static_departures_database.py" --data-root "$DATA_ROOT")"
echo "$LOG_PREFIX activated databaseVersion=$VERSION"

echo "$LOG_PREFIX refreshing static-departures-api container"
docker compose -f "$REPO/deploy/static-departures.compose.yml" up -d --build

HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-45}"
HEALTH_INTERVAL_SECONDS="${HEALTH_INTERVAL_SECONDS:-2}"
deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
HEALTH=""
until HEALTH="$(docker exec static-departures-api python3 -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8080/static-departures/health", timeout=5).read().decode())' 2>/dev/null)"; do
  if (( SECONDS >= deadline )); then
    echo "$LOG_PREFIX health check timed out after ${HEALTH_TIMEOUT_SECONDS}s" >&2
    docker logs --tail 80 static-departures-api >&2 || true
    exit 1
  fi
  sleep "$HEALTH_INTERVAL_SECONDS"
done
echo "$LOG_PREFIX health=$HEALTH"
echo "$LOG_PREFIX completed at $(date -Is)"
