#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-/srv/haltewecker/pipeline/HalterWeckerAPIService}"
DATA_ROOT="${DATA_ROOT:-/srv/haltewecker/data}"
STAGING="$DATA_ROOT/temp/stop-data"
BUILD_DIR="$STAGING/data"
CURRENT="$DATA_ROOT/current"
PREVIOUS="$DATA_ROOT/previous/stop-data"
ROLLBACK="$DATA_ROOT/temp/current-rollback"
STOP_DATA_LOCK="${STOP_DATA_LOCK:-/run/lock/haltewecker-stop-data.lock}"
STOP_DATA_ENV_FILE="${STOP_DATA_ENV_FILE:-/etc/haltewecker-stop-data.env}"

source "$STOP_DATA_ENV_FILE"

SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SUDO_BIN="${SUDO_BIN:-sudo}"
STATIC_DEPARTURES_SERVICE="${STATIC_DEPARTURES_SERVICE:-haltewecker-static-departures.service}"
FLOCK_BIN="${FLOCK_BIN:-flock}"

mkdir -p "$(dirname "$STOP_DATA_LOCK")"
exec 9>"$STOP_DATA_LOCK"
if ! "$FLOCK_BIN" -n 9; then
  echo "[StopData] another stop-data publication is already running" >&2
  exit 1
fi

run_systemctl() {
  "$SUDO_BIN" -n "$SYSTEMCTL_BIN" "$@"
}

static_departures_supports_wait() {
  "$SYSTEMCTL_BIN" start --help 2>&1 | grep -q -- '--wait'
}

published_release_version() {
  python3 - "$CURRENT/manifest.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
version = manifest.get("version")
if not isinstance(version, str) or not version:
    raise SystemExit("Published manifest does not contain a version.")
print(version)
PY
}

synchronize_static_departures() {
  local start_status=0
  local service_state

  echo "[StopData] starting static departures rebuild via $STATIC_DEPARTURES_SERVICE"
  if static_departures_supports_wait; then
    if run_systemctl start --wait "$STATIC_DEPARTURES_SERVICE"; then
      :
    else
      start_status=$?
    fi
  else
    echo "[StopData] systemctl --wait is unavailable; waiting for the Type=oneshot start job"
    if run_systemctl start "$STATIC_DEPARTURES_SERVICE"; then
      :
    else
      start_status=$?
    fi
  fi

  service_state="$(run_systemctl show "$STATIC_DEPARTURES_SERVICE" -p Result -p ExecMainStatus)"
  printf '[StopData] static departures service state:\n%s\n' "$service_state"

  if [[ "$start_status" -ne 0 || "$service_state" != *"Result=success"* || "$service_state" != *"ExecMainStatus=0"* ]]; then
    echo "[StopData] ERROR: stop packages are published, but static departures are not synchronized" >&2
    return 1
  fi

  echo "[StopData] static departures synchronized"
}

cd "$REPO"

rm -rf "$STAGING" "$ROLLBACK"
mkdir -p "$STAGING"

# Map provider env vars to repeatable --external-gtfs-url providerID=URL args.
# Add future countries here without changing build_stop_packages.py.
run_build_stop_packages() {
  local nl_url="${1-}"
  local -a cmd
  cmd=(
    python3 "$REPO/scripts/build_stop_packages.py"
    --gtfs-url "$GTFS_URL"
    --swiss-gtfs-url "$SWISS_GTFS_URL"
    --austrian-gtfs-url "${AUSTRIAN_GTFS_URL:-}"
  )
  if [ -n "$nl_url" ]; then
    cmd+=(--nl-gtfs-url "$nl_url")
  fi
  if [ -n "${SWEDEN_GTFS_URL:-}" ]; then
    cmd+=(--external-gtfs-url "sweden=${SWEDEN_GTFS_URL}")
  fi
  cmd+=(--output "$BUILD_DIR")
  "${cmd[@]}"
}

if [ "${FORCE_PRESERVE_NL:-0}" = "1" ]; then
  BUILD_WITHOUT_NL=1
elif ! run_build_stop_packages "$NL_GTFS_URL"; then
  BUILD_WITHOUT_NL=1
else
  BUILD_WITHOUT_NL=0
fi

if [ "$BUILD_WITHOUT_NL" = "1" ]; then
  test -d "$CURRENT"
  echo "Preserving the last published Dutch assets."
  rm -rf "$BUILD_DIR"
  run_build_stop_packages ""
  python3 "$REPO/scripts/preserve_nl_assets.py" \
    --current "$CURRENT" \
    --output "$BUILD_DIR" \
    --cities "$REPO/config/cities.json"
fi

python3 "$REPO/scripts/build_swiss_departure_index.py" \
  --gtfs-url "$SWISS_GTFS_URL" \
  --output "$BUILD_DIR/swiss-static"

test -f "$BUILD_DIR/manifest.json"
test -f "$BUILD_DIR/transit-radar-cities.json"
test -f "$BUILD_DIR/swiss-static/manifest.json"

if [ -n "${SWEDEN_GTFS_URL:-}" ]; then
  test -f "$BUILD_DIR/stops/stockholm.json"
  test -f "$BUILD_DIR/departures/stockholm.json"
  python3 - "$BUILD_DIR/manifest.json" "$BUILD_DIR/transit-radar-cities.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
radar = json.load(open(sys.argv[2], encoding="utf-8"))
manifest_ids = [city.get("id") for city in manifest.get("cities", [])]
if manifest_ids.count("stockholm") != 1:
    raise SystemExit(
        f"manifest must contain stockholm exactly once, found {manifest_ids.count('stockholm')}"
    )
radar_ids = [city.get("appCityID") for city in radar.get("cities", [])]
if radar_ids.count("stockholm") != 1:
    raise SystemExit(
        f"transit-radar-cities must contain stockholm exactly once, found {radar_ids.count('stockholm')}"
    )
print("[StopData] Sweden external packages validated")
PY
fi

# Do not replace or remove the last working published dataset before the complete
# staged build has passed all builders and manifest validation.
if [ -d "$CURRENT" ]; then
  mv "$CURRENT" "$ROLLBACK"
fi

if ! mv "$BUILD_DIR" "$CURRENT"; then
  if [ -d "$ROLLBACK" ]; then
    mv "$ROLLBACK" "$CURRENT"
  fi
  exit 1
fi

if [ -d "$ROLLBACK" ]; then
  mkdir -p "$(dirname "$PREVIOUS")"
  rm -rf "$PREVIOUS"
  mv "$ROLLBACK" "$PREVIOUS"
fi

mkdir -p "$STAGING"
RELEASE_VERSION="$(published_release_version)"
echo "[StopData] published stop release version=$RELEASE_VERSION path=$CURRENT"

if ! synchronize_static_departures; then
  echo "[StopData] ERROR: published stop release version=$RELEASE_VERSION is not synchronized with static departures" >&2
  exit 1
fi
