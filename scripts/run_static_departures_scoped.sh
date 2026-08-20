#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_ROOT="${DATA_ROOT:-/srv/haltewecker/data}"
STATIC_ENV_FILE="${STATIC_DEPARTURES_ENV_FILE:-/etc/haltewecker-stop-data.env}"
FLOCK_BIN="${FLOCK_BIN:-flock}"
LOCK_PATH="${STATIC_DEPARTURES_LOCK:-/run/lock/haltewecker-static-departures.lock}"

if [[ -f "$STATIC_ENV_FILE" ]]; then
  set -a
  source "$STATIC_ENV_FILE"
  set +a
fi

export REPO DATA_ROOT

DRY_RUN=0
for argument in "$@"; do
  if [[ "$argument" == "--dry-run" ]]; then
    DRY_RUN=1
  fi
done

if [[ "$DRY_RUN" == "1" ]]; then
  exec python3 "$REPO/scripts/static_departures_scoped.py" "$@"
fi

STATIC_511_SECRET_FILE="${STATIC_511_SECRET_FILE:-/srv/haltewecker/secrets/usa_511/.env}"
if [[ -f "$STATIC_511_SECRET_FILE" ]]; then
  set -a
  source "$STATIC_511_SECRET_FILE"
  set +a
fi

WMATA_SECRET_FILE="${WMATA_SECRET_FILE:-/srv/haltewecker/secrets/wmata/.env}"
if [[ -f "$WMATA_SECRET_FILE" ]]; then
  set -a
  source "$WMATA_SECRET_FILE"
  set +a
fi

STM_SECRET_FILE="${STM_SECRET_FILE:-/srv/haltewecker/secrets/stm-montreal/.env}"
if [[ -f "$STM_SECRET_FILE" ]]; then
  STM_API_KEY="$(sed -n 's/^STM_API_KEY=//p' "$STM_SECRET_FILE" | head -n 1)"
  export STM_API_KEY
fi

mkdir -p "$(dirname "$LOCK_PATH")"
exec 9>"$LOCK_PATH"
if ! "$FLOCK_BIN" -n 9; then
  echo "[SCOPED PIPELINE] another static-departures pipeline is already running" >&2
  exit 1
fi

exec python3 "$REPO/scripts/static_departures_scoped.py" "$@"
