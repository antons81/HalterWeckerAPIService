#!/usr/bin/env bash
set -euo pipefail
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STOP_DATA_ENV_FILE="${STOP_DATA_ENV_FILE:-/etc/haltewecker-stop-data.env}"
if [[ -f "$STOP_DATA_ENV_FILE" ]]; then
  set -a
  source "$STOP_DATA_ENV_FILE"
  set +a
fi

WMATA_ENV_FILE="${WMATA_ENV_FILE:-/srv/haltewecker/secrets/wmata/.env}"
if [[ -f "$WMATA_ENV_FILE" ]]; then
  set -a
  source "$WMATA_ENV_FILE"
  set +a
fi
DATA_ROOT="${DATA_ROOT:-/srv/haltewecker/data}"
GTFS_URL="${GTFS_URL:?GTFS_URL is required}"
STOP_DATA_PATH="${STOP_DATA_PATH:-$DATA_ROOT/current}"
NEXT_DATABASE_PATH="${NEXT_DATABASE_PATH:-$DATA_ROOT/staging/departures-next.sqlite}"
RELEASE_ID="${RELEASE_ID:-}"
AUSTRIAN_GTFS_PATH="${AUSTRIAN_GTFS_PATH:-}"
AUSTRIAN_GTFS_DIR="${AUSTRIAN_GTFS_DIR:-$DATA_ROOT/austria}"
LOG_PREFIX="[StaticDepartures]"
CONTAINER_NAME="${STATIC_DEPARTURES_CONTAINER_NAME:-static-departures-api}"
ACTIVE_RELEASE_DIR=""
if [[ -z "$RELEASE_ID" && -L "$DATA_ROOT/current-release" ]]; then
  ACTIVE_RELEASE_DIR="$DATA_ROOT/current-release"
  RELEASE_ID="$(python3 - "$ACTIVE_RELEASE_DIR/release-metadata.json" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["releaseID"])
PY
)"
  STOP_DATA_PATH="$ACTIVE_RELEASE_DIR/stop-data"
  NEXT_DATABASE_PATH="$ACTIVE_RELEASE_DIR/departures-next.sqlite"
fi

if [[ "${READINESS_ONLY:-0}" == "1" ]]; then
  echo "$LOG_PREFIX release=${RELEASE_ID:-legacy} stage=readiness started"
else

if [[ -z "$AUSTRIAN_GTFS_PATH" && -d "$AUSTRIAN_GTFS_DIR" && ! -f "$AUSTRIAN_GTFS_DIR/.env" ]]; then
  while IFS= read -r candidate; do
    AUSTRIAN_GTFS_PATH="$candidate"
  done < <(find "$AUSTRIAN_GTFS_DIR" -maxdepth 1 -type f -name '*.zip' -print | sort)
fi

stage_started=$SECONDS
echo "$LOG_PREFIX release=${RELEASE_ID:-legacy} stage=import started at $(date -Is)"
IMPORT_ARGS=(
  --gtfs-url "$GTFS_URL"
  --stop-data "$STOP_DATA_PATH"
  --next "$NEXT_DATABASE_PATH"
  --external-sources "$REPO/config/external-gtfs-sources.json"
)
if [[ -n "$RELEASE_ID" ]]; then
  IMPORT_ARGS+=(--release-id "$RELEASE_ID")
fi
if [[ -n "$AUSTRIAN_GTFS_PATH" ]]; then
  IMPORT_ARGS+=(--austrian-gtfs "$AUSTRIAN_GTFS_PATH")
elif [[ -d "$AUSTRIAN_GTFS_DIR" && -f "$AUSTRIAN_GTFS_DIR/.env" ]]; then
  IMPORT_ARGS+=(--austrian-gtfs-dir "$AUSTRIAN_GTFS_DIR" --austrian-sources "$REPO/config/austrian-sources.json")
fi
EXTERNAL_GTFS_IMPORT_ARGS=()
while IFS= read -r external_mapping; do
  [[ -n "$external_mapping" ]] || continue
  EXTERNAL_GTFS_IMPORT_ARGS+=(--external-gtfs-url "$external_mapping")
done < <(python3 - "${EXTERNAL_GTFS_ARTIFACTS_JSON:-}" "$REPO/config/external-gtfs-sources.json" <<'PY'
import json
import sys
from pathlib import Path

artifact_path = sys.argv[1].strip()
sources_path = Path(sys.argv[2])
sources = json.loads(sources_path.read_text(encoding="utf-8"))
enabled = {
    str(source["id"])
    for source in sources
    if source.get("importIntoStaticDepartures") is True
}

if artifact_path:
    payload = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    for source_id, entry in sorted(payload.get("external", {}).items()):
        if source_id in enabled and isinstance(entry, dict) and entry.get("path"):
            print(f"{source_id}={entry['path']}")
else:
    for source in sources:
        source_id = str(source.get("id", ""))
        if source_id not in enabled:
            continue
        value = str(source.get("localPath") or source.get("url") or "").strip()
        if value:
            print(f"{source_id}={value}")
PY
)
IMPORT_ARGS+=("${EXTERNAL_GTFS_IMPORT_ARGS[@]}")

# Reload the operator secret immediately before the importer so intermediate
# environment setup cannot replace the credential inherited by the child.
if [[ -f "$WMATA_ENV_FILE" ]]; then
  set -a
  source "$WMATA_ENV_FILE"
  set +a
fi
: "${WMATA_API_KEY:?WMATA_API_KEY is required before static departures importer}"
python3 -u "$REPO/scripts/import_static_departures_database.py" "${IMPORT_ARGS[@]}"
echo "$LOG_PREFIX release=${RELEASE_ID:-legacy} stage=import duration=$((SECONDS - stage_started))s"

if [[ "${SKIP_ACTIVATION:-0}" == "1" ]]; then
  echo "$LOG_PREFIX release=${RELEASE_ID:-legacy} stage=validation duration=0s (database validation completed by importer)"
  exit 0
fi

echo "$LOG_PREFIX atomic activation started at $(date -Is)"
if [[ -n "$ACTIVE_RELEASE_DIR" ]]; then
  VERSION="$(python3 - "$NEXT_DATABASE_PATH" "$ACTIVE_RELEASE_DIR/departures.sqlite" <<'PY'
import os
import sys
os.replace(sys.argv[1], sys.argv[2])
print("release-updated")
PY
)"
else
  VERSION="$(python3 "$REPO/scripts/swap_static_departures_database.py" --data-root "$DATA_ROOT")"
fi
echo "$LOG_PREFIX activated databaseVersion=$VERSION"
fi

echo "$LOG_PREFIX refreshing static-departures-api container"
docker compose -f "$REPO/deploy/static-departures.compose.yml" up -d --build

HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-45}"
HEALTH_INTERVAL_SECONDS="${HEALTH_INTERVAL_SECONDS:-2}"
deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
HEALTH=""
readiness_started=$SECONDS
until HEALTH="$(docker exec "$CONTAINER_NAME" python3 -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8080/static-departures/health", timeout=5).read().decode())' 2>/dev/null)"; do
  if (( SECONDS >= deadline )); then
    echo "$LOG_PREFIX health check timed out after ${HEALTH_TIMEOUT_SECONDS}s" >&2
    docker logs --tail 80 "$CONTAINER_NAME" >&2 || true
    exit 1
  fi
  sleep "$HEALTH_INTERVAL_SECONDS"
done
echo "$LOG_PREFIX health=$HEALTH"
if [[ -n "$RELEASE_ID" ]]; then
  python3 - "$HEALTH" "$RELEASE_ID" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
actual = payload.get("database", {}).get("releaseID", "")
if actual != sys.argv[2]:
    raise SystemExit(f"runtime release mismatch: expected {sys.argv[2]}, got {actual or '<missing>'}")
PY
fi
echo "$LOG_PREFIX release=${RELEASE_ID:-legacy} stage=readiness duration=$((SECONDS - readiness_started))s"
echo "$LOG_PREFIX completed at $(date -Is)"
