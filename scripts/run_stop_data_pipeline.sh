#!/usr/bin/env bash
set -euo pipefail
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

RUN_MODE="normal"
RESUME_RELEASE_ID=""
NO_ACTIVATE="${HALTEWECKER_NIGHTLY_NO_ACTIVATE:-0}"
EXPLICIT_NO_ACTIVATE=0
INCREMENTAL_NO_ACTIVATE=0
INCREMENTAL_PROOF_OVERRIDE="${HALTEWECKER_INCREMENTAL_PROOF_OVERRIDE:-0}"
REUSE_STOP_DATA=0
STOP_DATA_ONLY=0
REUSE_STOP_DATA_REFERENCE=""
REUSED_STOP_DATA_SOURCE=""
REUSED_STOP_DATA_RELEASE_ID=""
REUSED_STOP_DATA_MANIFEST_SHA256=""
REUSED_STOP_DATA_METADATA_SHA256=""

cleanup_failed_no_activate() {
  local status="$?"
  if [[ "$status" -ne 0 && "$NO_ACTIVATE" == "1" && -n "${RELEASE_DIR:-}" ]]; then
    rm -rf -- "$RELEASE_DIR"
    DIAGNOSTICS_CLEANUP_ACTIONS="removed release_dir=$RELEASE_DIR"
    if [[ -n "${INCREMENTAL_RELEASE_DIR:-}" && -d "$INCREMENTAL_RELEASE_DIR" ]]; then
      if [[ -f "$INCREMENTAL_RELEASE_DIR/release.json" ]]; then
        DIAGNOSTICS_CLEANUP_ACTIONS+=";preserved published_incremental_release=$INCREMENTAL_RELEASE_DIR"
      else
        rm -rf -- "$INCREMENTAL_RELEASE_DIR"
        DIAGNOSTICS_CLEANUP_ACTIONS+=";removed incremental_release_dir=$INCREMENTAL_RELEASE_DIR"
      fi
    fi
    echo "[Nightly] stage=cleanup status=PASS release=$RELEASE_ID reason=failure" >&2
  fi
  if [[ "$NO_ACTIVATE" == "1" ]] && type log_disk_state >/dev/null 2>&1; then
    log_disk_state "after"
    log_disk_peak
  fi
  if [[ "$NO_ACTIVATE" == "1" ]]; then
    diagnostics_write_report "$status"
  fi
  exit "$status"
}
if [[ "${1:-}" == "--resume" ]]; then
  if [[ "$#" -ne 2 ]]; then
    echo "usage: $0 [--resume RELEASE_ID]" >&2
    exit 64
  fi
  RUN_MODE="resume"
  RESUME_RELEASE_ID="$2"
elif [[ "${1:-}" == "--no-activate" && "$#" -eq 1 ]]; then
  NO_ACTIVATE=1
  EXPLICIT_NO_ACTIVATE=1
elif [[ "${1:-}" == "--no-activate" && "${2:-}" == "--reuse-stop-data" && "$#" -eq 2 ]]; then
  NO_ACTIVATE=1
  EXPLICIT_NO_ACTIVATE=1
  REUSE_STOP_DATA=1
elif [[ "${1:-}" == "--no-activate" && "${2:-}" == "--reuse-stop-data" && "$#" -eq 3 ]]; then
  NO_ACTIVATE=1
  EXPLICIT_NO_ACTIVATE=1
  REUSE_STOP_DATA=1
  REUSE_STOP_DATA_REFERENCE="$3"
elif [[ "${1:-}" == "--incremental-no-activate" && "$#" -eq 1 ]]; then
  NO_ACTIVATE=1
  EXPLICIT_NO_ACTIVATE=1
  INCREMENTAL_NO_ACTIVATE=1
elif [[ "${1:-}" == "--stop-data-only" && "$#" -eq 1 ]]; then
  NO_ACTIVATE=1
  EXPLICIT_NO_ACTIVATE=1
  STOP_DATA_ONLY=1
elif [[ "$#" -ne 0 ]]; then
  echo "usage: $0 [--resume RELEASE_ID|--stop-data-only|--incremental-no-activate|--no-activate [--reuse-stop-data [RELEASE_ID]]]" >&2
  exit 64
fi

if [[ "$REUSE_STOP_DATA" == "1" && "$EXPLICIT_NO_ACTIVATE" != "1" ]]; then
  echo "[StopData] ERROR: --reuse-stop-data requires explicit --no-activate" >&2
  exit 64
fi
if [[ "$INCREMENTAL_PROOF_OVERRIDE" != "0" && "$INCREMENTAL_PROOF_OVERRIDE" != "1" ]]; then
  echo "[StopData] ERROR: HALTEWECKER_INCREMENTAL_PROOF_OVERRIDE must be 0 or 1" >&2
  exit 64
fi
if [[ "$INCREMENTAL_PROOF_OVERRIDE" == "1" && "$INCREMENTAL_NO_ACTIVATE" != "1" ]]; then
  echo "[StopData] ERROR: incremental proof override requires --incremental-no-activate" >&2
  exit 64
fi

REPO="${REPO:-/srv/haltewecker/pipeline/HalterWeckerAPIService}"
DATA_ROOT="${DATA_ROOT:-/srv/haltewecker/data}"
RELEASES="$DATA_ROOT/releases"
if [[ "$RUN_MODE" == "resume" ]]; then
  if ! [[ "$RESUME_RELEASE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "[StopData] ERROR: invalid release ID for resume: $RESUME_RELEASE_ID" >&2
    exit 64
  fi
  RELEASE_ID="$RESUME_RELEASE_ID"
else
  RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
fi
RELEASE_DIR="$RELEASES/$RELEASE_ID"
INCREMENTAL_RELEASES_ROOT="${HALTEWECKER_INCREMENTAL_RELEASES_ROOT:-$DATA_ROOT/releases/incremental}"
INCREMENTAL_RELEASE_DIR="$INCREMENTAL_RELEASES_ROOT/$RELEASE_ID"
BUILD_DIR="$RELEASE_DIR/stop-data"
ARTIFACTS_JSON="$RELEASE_DIR/gtfs-artifacts.json"
CURRENT="$DATA_ROOT/current"
PREVIOUS="$DATA_ROOT/previous/stop-data"
ROLLBACK="$DATA_ROOT/temp/current-rollback/$RELEASE_ID"
CURRENT_RELEASE="$DATA_ROOT/current-release"
STATIC_DEPARTURES_RELEASE="$DATA_ROOT/static-departures-release"
DEPARTURES_CURRENT="$DATA_ROOT/departures-current.sqlite"
STOP_DATA_LOCK="${STOP_DATA_LOCK:-/run/lock/haltewecker-stop-data.lock}"
STATIC_DEPARTURES_LOCK="${STATIC_DEPARTURES_LOCK:-/run/lock/haltewecker-static-departures.lock}"
STOP_DATA_ENV_FILE="${STOP_DATA_ENV_FILE:-/etc/haltewecker-stop-data.env}"

set -a
source "$STOP_DATA_ENV_FILE"
set +a

WMATA_ENV_FILE="${WMATA_ENV_FILE:-/srv/haltewecker/secrets/wmata/.env}"
if [[ -f "$WMATA_ENV_FILE" ]]; then
  set -a
  source "$WMATA_ENV_FILE"
  set +a
fi

API_511_ENV_FILE="${API_511_ENV_FILE:-/srv/haltewecker/secrets/usa_511/.env}"
if [[ -f "$API_511_ENV_FILE" ]]; then
  set -a
  source "$API_511_ENV_FILE"
  set +a
fi

FINLAND_ENV_FILE="${FINLAND_ENV_FILE:-/srv/haltewecker/secrets/finland/.env}"
if [[ -f "$FINLAND_ENV_FILE" ]]; then
  set -a
  source "$FINLAND_ENV_FILE"
  set +a
fi

AUSTRALIA_ENV_FILE="${AUSTRALIA_ENV_FILE:-/srv/haltewecker/secrets/australia/.env}"
if [[ -f "$AUSTRALIA_ENV_FILE" ]]; then
  set -a
  source "$AUSTRALIA_ENV_FILE"
  set +a
fi

SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SUDO_BIN="${SUDO_BIN:-sudo}"
STATIC_DEPARTURES_SERVICE="${STATIC_DEPARTURES_SERVICE:-haltewecker-static-departures.service}"
FLOCK_BIN="${FLOCK_BIN:-flock}"
AUSTRIAN_DATA_ROOT="${AUSTRIAN_DATA_ROOT:-$DATA_ROOT/austria}"
MVO_ENV_FILE="${MVO_ENV_FILE:-$AUSTRIAN_DATA_ROOT/.env}"
STATIC_DEPARTURES_PIPELINE="${STATIC_DEPARTURES_PIPELINE:-$REPO/scripts/run_static_departures_pipeline.sh}"
RELEASE_STATE_SCRIPT="$REPO/scripts/release_state.py"
CUSTOM_ARTIFACTS_JSON="$RELEASE_DIR/custom-gtfs-artifacts.json"

DIAGNOSTICS_ROOT="${HALTEWECKER_PIPELINE_DIAGNOSTICS_ROOT:-$DATA_ROOT/pipeline-diagnostics}"
if [[ -n "${HALTEWECKER_DIAGNOSTICS_RUN_KIND:-}" ]]; then
  DIAGNOSTICS_RUN_KIND="$HALTEWECKER_DIAGNOSTICS_RUN_KIND"
elif [[ "$STOP_DATA_ONLY" == "1" ]]; then
  DIAGNOSTICS_RUN_KIND="stop-data-only"
elif [[ "$REUSE_STOP_DATA" == "1" ]]; then
  DIAGNOSTICS_RUN_KIND="cold-reuse"
elif [[ "$INCREMENTAL_NO_ACTIVATE" == "1" ]]; then
  DIAGNOSTICS_RUN_KIND="incremental-no-activate"
elif [[ "$NO_ACTIVATE" == "1" ]]; then
  DIAGNOSTICS_RUN_KIND="no-activate"
else
  DIAGNOSTICS_RUN_KIND="production"
fi
DIAGNOSTICS_INVOCATION_TOKEN="$(date -u +%Y%m%dT%H%M%SZ)-$$"
DIAGNOSTICS_RUN_ID="${HALTEWECKER_RUN_ID:-${DIAGNOSTICS_RUN_KIND}-${DIAGNOSTICS_INVOCATION_TOKEN}}"
DIAGNOSTICS_LOG="$DIAGNOSTICS_ROOT/$DIAGNOSTICS_RUN_ID.log"
DIAGNOSTICS_STDERR_LOG="$DIAGNOSTICS_ROOT/$DIAGNOSTICS_RUN_ID.stderr.log"
DIAGNOSTICS_REPORT="$DIAGNOSTICS_ROOT/$DIAGNOSTICS_RUN_ID.report"
DIAGNOSTICS_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DIAGNOSTICS_SIGNAL=""
DIAGNOSTICS_CURRENT_STAGE="initialization"
DIAGNOSTICS_CLEANUP_ACTIONS="none"
DIAGNOSTICS_BUILD_FINGERPRINT=""
DIAGNOSTICS_FINGERPRINT_VERSION="unknown"
DIAGNOSTICS_DISK_BEFORE_KB=""
DIAGNOSTICS_DISK_AFTER_KB=""
DIAGNOSTICS_MIN_FREE_KB=""
DIAGNOSTICS_STAGING_PATH=""
DIAGNOSTICS_PUBLISHED_PATH=""
DIAGNOSTICS_PUBLICATION_TRANSITION_MS=""
BUILD_FINGERPRINT=""

if [[ "$NO_ACTIVATE" == "1" ]]; then
  mkdir -p "$DIAGNOSTICS_ROOT"
  exec > >(tee -a "$DIAGNOSTICS_LOG")
  exec 2> >(tee -a "$DIAGNOSTICS_STDERR_LOG" >&2)
fi

diagnostics_set_stage() {
  DIAGNOSTICS_CURRENT_STAGE="$1"
}

diagnostics_refresh_fingerprint() {
  DIAGNOSTICS_FINGERPRINT_VERSION="$(sed -n 's/^STOP_DATA_BUILD_FINGERPRINT_VERSION = //p' "$REPO/scripts/build_fingerprint.py" | tr -d '[:space:]' || true)"
  DIAGNOSTICS_FINGERPRINT_VERSION="${DIAGNOSTICS_FINGERPRINT_VERSION:-unknown}"
  DIAGNOSTICS_BUILD_FINGERPRINT="$(python3 "$REPO/scripts/build_fingerprint.py" --repository "$REPO" 2>/dev/null || true)"
  BUILD_FINGERPRINT="$DIAGNOSTICS_BUILD_FINGERPRINT"
}

diagnostics_write_report() {
  local status="$1"
  local result="PASS"
  local ended_at
  local disk_after_kb
  local generation_size_kb
  local last_stage_line
  local last_provider
  local last_provider_stage
  local publication_line

  if [[ "$status" -ne 0 ]]; then
    result="FAIL"
  fi
  ended_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  disk_after_kb="$(df -Pk "$DATA_ROOT" 2>/dev/null | awk 'NR == 2 { print $4; exit }' || true)"
  DIAGNOSTICS_DISK_AFTER_KB="${disk_after_kb:-}"
  generation_size_kb="$(du -skL "${RELEASE_DIR:-}" 2>/dev/null | awk '{ print $1; exit }' || true)"
  generation_size_kb="${generation_size_kb:-0}"
  last_stage_line="$(grep -E 'stage=[^ ]+' "$DIAGNOSTICS_LOG" 2>/dev/null | tail -n 1 || true)"
  last_provider="$(printf '%s\n' "$last_stage_line" | sed -n 's/.*source=\([^ ]*\).*/\1/p')"
  last_provider_stage="$(printf '%s\n' "$last_stage_line" | sed -n 's/.*stage=\([^ ]*\).*/\1/p')"
  publication_line="$(grep -E 'stage=release-publication status=PASS' "$DIAGNOSTICS_LOG" 2>/dev/null | tail -n 1 || true)"
  DIAGNOSTICS_STAGING_PATH="$(printf '%s\n' "$publication_line" | sed -n 's/.*staging_path=\([^ ]*\).*/\1/p')"
  DIAGNOSTICS_PUBLISHED_PATH="$(printf '%s\n' "$publication_line" | sed -n 's/.*published_path=\([^ ]*\) transition_ms=.*/\1/p')"
  DIAGNOSTICS_PUBLICATION_TRANSITION_MS="$(printf '%s\n' "$publication_line" | sed -n 's/.*transition_ms=\([^ ]*\).*/\1/p')"

  {
    printf 'status=%s\n' "$result"
    printf 'exit_code=%s\n' "$status"
    printf 'signal=%s\n' "${DIAGNOSTICS_SIGNAL:-none}"
    printf 'run_id=%s\n' "$DIAGNOSTICS_RUN_ID"
    printf 'run_kind=%s\n' "$DIAGNOSTICS_RUN_KIND"
    printf 'release_id=%s\n' "${RELEASE_ID:-unknown}"
    printf 'git_sha=%s\n' "$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
    printf 'fingerprint_version=%s\n' "$DIAGNOSTICS_FINGERPRINT_VERSION"
    printf 'build_fingerprint=%s\n' "${DIAGNOSTICS_BUILD_FINGERPRINT:-unknown}"
    printf 'started_at=%s\n' "$DIAGNOSTICS_STARTED_AT"
    printf 'ended_at=%s\n' "$ended_at"
    printf 'current_stage=%s\n' "$DIAGNOSTICS_CURRENT_STAGE"
    printf 'last_provider=%s\n' "${last_provider:-unknown}"
    printf 'last_provider_stage=%s\n' "${last_provider_stage:-unknown}"
    printf 'staging_path=%s\n' "${DIAGNOSTICS_STAGING_PATH:-unknown}"
    printf 'published_path=%s\n' "${DIAGNOSTICS_PUBLISHED_PATH:-unknown}"
    printf 'publication_transition_ms=%s\n' "${DIAGNOSTICS_PUBLICATION_TRANSITION_MS:-unknown}"
    printf 'disk_before_free_kb=%s\n' "${DIAGNOSTICS_DISK_BEFORE_KB:-unknown}"
    printf 'disk_min_free_kb=%s\n' "${DIAGNOSTICS_MIN_FREE_KB:-unknown}"
    printf 'disk_after_free_kb=%s\n' "${DIAGNOSTICS_DISK_AFTER_KB:-unknown}"
    printf 'generation_size_at_exit_kb=%s\n' "$generation_size_kb"
    printf 'cleanup_actions=%s\n' "$DIAGNOSTICS_CLEANUP_ACTIONS"
    printf 'stdout_log=%s\n' "$DIAGNOSTICS_LOG"
    printf 'stderr_log=%s\n' "$DIAGNOSTICS_STDERR_LOG"
    printf 'last_stage_line=%s\n' "$last_stage_line"
  } > "$DIAGNOSTICS_REPORT"
}

if [[ "$NO_ACTIVATE" == "1" ]]; then
  trap cleanup_failed_no_activate EXIT
  trap 'DIAGNOSTICS_SIGNAL=SIGINT; exit 130' INT
  trap 'DIAGNOSTICS_SIGNAL=SIGTERM; exit 143' TERM
  trap 'DIAGNOSTICS_SIGNAL=SIGHUP; exit 129' HUP
fi

resolve_reused_stop_data() {
  local resolved_source
  local resolved_releases_root
  local relative_source
  local source_release_dir
  local source_metadata

  if [[ "$RUN_MODE" != "normal" ]]; then
    echo "[StopData] ERROR: --reuse-stop-data is only supported for a new no-activate run" >&2
    return 1
  fi
  if [[ -n "$REUSE_STOP_DATA_REFERENCE" ]]; then
    if ! [[ "$REUSE_STOP_DATA_REFERENCE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
      echo "[StopData] ERROR: invalid reuse generation ID: $REUSE_STOP_DATA_REFERENCE" >&2
      return 1
    fi
    source_release_dir="$RELEASES/$REUSE_STOP_DATA_REFERENCE"
    resolved_source="$(readlink -f "$source_release_dir/stop-data" || true)"
    if [[ ! -d "$resolved_source" ]]; then
      echo "[StopData] ERROR: reuse generation is missing or broken: $REUSE_STOP_DATA_REFERENCE" >&2
      return 1
    fi
  else
    if [[ ! -L "$CURRENT" ]]; then
      echo "[StopData] ERROR: reuse source must be an explicit current release symlink: $CURRENT" >&2
      return 1
    fi
    resolved_source="$(readlink -f "$CURRENT" || true)"
    if [[ ! -d "$resolved_source" ]]; then
      echo "[StopData] ERROR: reuse source is missing or broken: $CURRENT" >&2
      return 1
    fi
  fi
  resolved_releases_root="$(readlink -f "$RELEASES")"
  relative_source="${resolved_source#"$resolved_releases_root/"}"
  if [[ "$relative_source" == "$resolved_source" || "$relative_source" != */stop-data || "${relative_source%/stop-data}" == */* ]]; then
    echo "[StopData] ERROR: reuse source is not a direct published release stop-data path: $resolved_source" >&2
    return 1
  fi
  REUSED_STOP_DATA_RELEASE_ID="${relative_source%/stop-data}"
  source_release_dir="$RELEASES/$REUSED_STOP_DATA_RELEASE_ID"
  if [[ "$(readlink -f "$source_release_dir/stop-data")" != "$resolved_source" ]]; then
    echo "[StopData] ERROR: reuse source does not match its published release path" >&2
    return 1
  fi
  source_metadata="$source_release_dir/release-metadata.json"
  if [[ ! -f "$source_metadata" ]]; then
    echo "[StopData] ERROR: reuse source metadata is missing: $source_metadata" >&2
    return 1
  fi

  BUILD_FINGERPRINT="$(python3 "$REPO/scripts/build_fingerprint.py" --repository "$REPO")"
  if ! python3 - "$resolved_source" "$REUSED_STOP_DATA_RELEASE_ID" "$source_metadata" "$BUILD_FINGERPRINT" <<'PY'
import json
import sys
from pathlib import Path

stop_data = Path(sys.argv[1])
release_id = sys.argv[2]
metadata_path = Path(sys.argv[3])
build_fingerprint = sys.argv[4]
required_directories = (
    "stops", "routes", "departures", "trips", "transit", "radar",
    "swiss-static", "provenance",
)
required_files = (
    "manifest.json", "transit-radar-cities.json",
    "swiss-static/manifest.json", "provenance/input-artifacts.json",
)
for relative in required_directories:
    if not (stop_data / relative).is_dir():
        raise SystemExit(f"reuse stop-data directory is missing: {stop_data / relative}")
for relative in required_files:
    path = stop_data / relative
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"reuse stop-data artifact is missing or empty: {path}")

def read_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"reuse stop-data JSON is invalid: {path}: {type(error).__name__}") from error
    if not isinstance(payload, dict):
        raise SystemExit(f"reuse stop-data JSON is not an object: {path}")
    return payload

manifest = read_object(stop_data / "manifest.json")
if manifest.get("releaseID") != release_id:
    raise SystemExit(
        f"reuse stop-data release mismatch: expected {release_id}, got {manifest.get('releaseID', '<missing>')}"
    )
cities = manifest.get("cities")
if not isinstance(cities, list) or not cities:
    raise SystemExit("reuse stop-data manifest has no cities")
for city in cities:
    if not isinstance(city, dict) or not isinstance(city.get("url"), str):
        raise SystemExit("reuse stop-data manifest contains an invalid city entry")
    package = stop_data / str(city["url"])
    if not package.is_file() or package.stat().st_size == 0:
        raise SystemExit(f"reuse stop-data city package is missing or empty: {package}")

metadata = read_object(metadata_path)
if metadata.get("releaseID") != release_id:
    raise SystemExit("reuse stop-data release metadata ID does not match source generation")
if metadata.get("buildFingerprint") != build_fingerprint:
    raise SystemExit("reuse stop-data build fingerprint is incompatible with current pipeline")
if not isinstance(manifest.get("sourceArtifacts"), dict):
    raise SystemExit("reuse stop-data manifest has no sourceArtifacts provenance")
PY
  then
    return 1
  fi

  REUSED_STOP_DATA_SOURCE="$source_release_dir/stop-data"
  REUSED_STOP_DATA_MANIFEST_SHA256="$(sha256sum "$REUSED_STOP_DATA_SOURCE/manifest.json" | awk '{print $1}')"
  REUSED_STOP_DATA_METADATA_SHA256="$(sha256sum "$source_metadata" | awk '{print $1}')"

  RELEASE_ID="$REUSED_STOP_DATA_RELEASE_ID"
  RELEASE_DIR="$RELEASES/.no-activate-${RELEASE_ID}-$$"
  INCREMENTAL_RELEASES_ROOT="${HALTEWECKER_INCREMENTAL_RELEASES_ROOT:-$DATA_ROOT/releases/incremental}/reuse-$$"
  INCREMENTAL_RELEASE_DIR="$INCREMENTAL_RELEASES_ROOT/$RELEASE_ID"
  BUILD_DIR="$REUSED_STOP_DATA_SOURCE"
  ARTIFACTS_JSON="$RELEASE_DIR/gtfs-artifacts.json"
  CUSTOM_ARTIFACTS_JSON="$RELEASE_DIR/custom-gtfs-artifacts.json"
  mkdir -p "$RELEASE_DIR"
  cp -p "$source_release_dir/gtfs-artifacts.json" "$ARTIFACTS_JSON"
  cp -p "$source_release_dir/custom-gtfs-artifacts.json" "$CUSTOM_ARTIFACTS_JSON"
  if [[ -f "$source_release_dir/austrian-artifacts.json" ]]; then
    cp -p "$source_release_dir/austrian-artifacts.json" "$RELEASE_DIR/austrian-artifacts.json"
  fi
  echo "[Nightly] stage=stop-data-reuse status=PASS release=$RELEASE_ID source=$REUSED_STOP_DATA_SOURCE manifest_sha256=$REUSED_STOP_DATA_MANIFEST_SHA256"
}

verify_reused_stop_data_unchanged() {
  local current_source
  local current_manifest_sha256
  local current_metadata_sha256
  current_manifest_sha256="$(sha256sum "$REUSED_STOP_DATA_SOURCE/manifest.json" | awk '{print $1}')"
  current_metadata_sha256="$(sha256sum "${REUSED_STOP_DATA_SOURCE%/stop-data}/release-metadata.json" | awk '{print $1}')"
  if [[ -z "$REUSE_STOP_DATA_REFERENCE" ]]; then
    current_source="$(readlink -f "$CURRENT")"
  else
    current_source="$(readlink -f "$REUSED_STOP_DATA_SOURCE")"
  fi
  if [[ "$current_source" != "$(readlink -f "$REUSED_STOP_DATA_SOURCE")" || "$current_manifest_sha256" != "$REUSED_STOP_DATA_MANIFEST_SHA256" || "$current_metadata_sha256" != "$REUSED_STOP_DATA_METADATA_SHA256" ]]; then
    echo "[StopData] ERROR: reused stop-data generation changed during run" >&2
    return 1
  fi
  echo "[Nightly] stage=stop-data-reuse status=UNCHANGED release=$RELEASE_ID source=$REUSED_STOP_DATA_SOURCE"
}

if [[ "$REUSE_STOP_DATA" == "1" ]]; then
  resolve_reused_stop_data
fi

mkdir -p "$(dirname "$STOP_DATA_LOCK")"
exec 9>"$STOP_DATA_LOCK"
if ! "$FLOCK_BIN" -n 9; then
  echo "[StopData] another stop-data publication is already running" >&2
  exit 1
fi
mkdir -p "$(dirname "$STATIC_DEPARTURES_LOCK")"
exec 10>"$STATIC_DEPARTURES_LOCK"
if ! "$FLOCK_BIN" -n 10; then
  echo "[StopData] static-departures lock is held by another job" >&2
  exit 1
fi

# Invalidate the standalone static-departures handoff before starting a new
# stop-data build. A failed build must never leave the previous release eligible
# for the downstream nightly static-departures timer. Resume preserves the
# existing handoff until activation-state inspection has completed.
if [[ "$RUN_MODE" == "normal" && "$NO_ACTIVATE" != "1" ]]; then
  rm -f "$STATIC_DEPARTURES_RELEASE"
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

elapsed_seconds() {
  echo "$((SECONDS - $1))s"
}

replace_link() {
  local link="$1"
  local target="$2"
  local temporary="${link}.next"
  rm -f "$temporary"
  ln -s "$target" "$temporary"
  python3 - "$temporary" "$link" <<'PY'
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
}

persist_release_stage() {
  local completed_stage="$1"
  python3 "$RELEASE_STATE_SCRIPT" write-state \
    --release-dir "$RELEASE_DIR" \
    --release-id "$RELEASE_ID" \
    --completed-stage "$completed_stage" \
    --build-fingerprint "$BUILD_FINGERPRINT"
  echo "[StopData] release=$RELEASE_ID state completedStage=$completed_stage persisted"
}

inspect_resume() {
  local resume_info
  BUILD_FINGERPRINT="$(python3 "$REPO/scripts/build_fingerprint.py" --repository "$REPO")"
  if ! resume_info="$(python3 "$RELEASE_STATE_SCRIPT" inspect-resume \
    --releases-root "$RELEASES" \
    --release-id "$RELEASE_ID" \
    --repository "$REPO" \
    --current-fingerprint "$BUILD_FINGERPRINT" \
    --current "$CURRENT" \
    --current-release "$CURRENT_RELEASE" \
    --static-departures-release "$STATIC_DEPARTURES_RELEASE" \
    --departures-current "$DEPARTURES_CURRENT" \
    --previous "$PREVIOUS" \
    --format tsv)"; then
    return 1
  fi
  IFS=$'\t' read -r RESUME_STATUS RESUME_COMPLETED_STAGE RESUME_NEXT_STAGE <<<"$resume_info"
  echo "[StopData] release=$RELEASE_ID resume=true completedStage=$RESUME_COMPLETED_STAGE nextStage=$RESUME_NEXT_STAGE status=$RESUME_STATUS"
}

prepare_runtime() {
  echo "[StopData] release=$RELEASE_ID preparing candidate runtime readiness"
  if ! READINESS_ONLY=1 \
    RELEASE_ID="$RELEASE_ID" \
    EXTERNAL_GTFS_ARTIFACTS_JSON="$ARTIFACTS_JSON" \
    STATIC_DEPARTURES_CONTAINER_NAME="static-departures-api-$RELEASE_ID" \
    DEPARTURES_DATABASE="/data/releases/$RELEASE_ID/departures.sqlite" \
    STATIC_DATA_ROOT="/data/releases/$RELEASE_ID/stop-data" \
    "$STATIC_DEPARTURES_PIPELINE"; then
    echo "[StopData] ERROR: release=$RELEASE_ID runtime readiness failed" >&2
    return 1
  fi
}

activate_runtime() {
  echo "[StopData] release=$RELEASE_ID activating canonical runtime"
  if ! READINESS_ONLY=1 RELEASE_ID="$RELEASE_ID" EXTERNAL_GTFS_ARTIFACTS_JSON="$ARTIFACTS_JSON" "$STATIC_DEPARTURES_PIPELINE"; then
    echo "[StopData] ERROR: release=$RELEASE_ID canonical runtime readiness failed" >&2
    return 1
  fi
  echo "[StopData] release=$RELEASE_ID static departures synchronized"
}

cd "$REPO"
TOTAL_STARTED=$SECONDS
PEAK_USED_KB=0
PEAK_FREE_KB=0
if [[ "$NO_ACTIVATE" == "1" ]]; then
  diagnostics_refresh_fingerprint
fi

disk_free_kb() {
  df -Pk "$DATA_ROOT" | awk 'NR == 2 { print $4; exit }'
}

disk_used_kb() {
  df -Pk "$DATA_ROOT" | awk 'NR == 2 { print $3; exit }'
}

log_disk_state() {
  local phase="$1"
  local free_kb
  local used_kb
  free_kb="$(disk_free_kb)"
  used_kb="$(disk_used_kb)"
  if (( used_kb > PEAK_USED_KB )); then
    PEAK_USED_KB="$used_kb"
    PEAK_FREE_KB="$free_kb"
  fi
  if [[ -z "$DIAGNOSTICS_DISK_BEFORE_KB" ]]; then
    DIAGNOSTICS_DISK_BEFORE_KB="$free_kb"
  fi
  if [[ -z "$DIAGNOSTICS_MIN_FREE_KB" || "$free_kb" -lt "$DIAGNOSTICS_MIN_FREE_KB" ]]; then
    DIAGNOSTICS_MIN_FREE_KB="$free_kb"
  fi
  echo "[Nightly] disk phase=$phase used_kb=$used_kb free_kb=$free_kb free_gb=$((free_kb / 1024 / 1024))"
}

log_disk_peak() {
  echo "[Nightly] disk phase=peak used_kb=$PEAK_USED_KB free_kb=$PEAK_FREE_KB free_gb=$((PEAK_FREE_KB / 1024 / 1024))"
}

proof_disk_preflight() {
  diagnostics_set_stage "disk-preflight"
  local free_kb
  local current_stop_data_kb=0
  local margin_kb
  local estimated_additional_kb
  local estimated_free_kb
  local minimum_free_kb

  free_kb="$(disk_free_kb)"
  if [[ "$REUSE_STOP_DATA" != "1" && ( -d "$CURRENT" || -L "$CURRENT" ) ]]; then
    current_stop_data_kb="$(du -skL "$CURRENT" 2>/dev/null | awk '{ print $1; exit }' || true)"
    current_stop_data_kb="${current_stop_data_kb:-0}"
  fi
  margin_kb=$(( ${HALTEWECKER_PROOF_ESTIMATE_MARGIN_GB:-5} * 1024 * 1024 ))
  estimated_additional_kb=$((current_stop_data_kb + margin_kb))
  estimated_free_kb=$((free_kb - estimated_additional_kb))
  if [[ "$INCREMENTAL_NO_ACTIVATE" == "1" ]]; then
    if [[ "$INCREMENTAL_PROOF_OVERRIDE" == "1" ]]; then
      minimum_free_kb=$((35 * 1024 * 1024))
      warning_free_kb=$((40 * 1024 * 1024))
      disk_mode="manual-production-shaped-proof"
    else
      minimum_free_kb=$((45 * 1024 * 1024))
      warning_free_kb=$((45 * 1024 * 1024))
      disk_mode="production-shaped-no-activate"
    fi
  else
    minimum_free_kb=$(( ${HALTEWECKER_MIN_FREE_GB:-45} * 1024 * 1024 ))
    warning_free_kb="$minimum_free_kb"
    disk_mode="proof-or-legacy"
  fi
  log_disk_state "before"
  echo "[Nightly] disk estimated_additional_gb=$((estimated_additional_kb / 1024 / 1024)) estimated_peak_free_gb=$((estimated_free_kb / 1024 / 1024)) warning_free_gb=$((warning_free_kb / 1024 / 1024)) minimum_free_gb=$((minimum_free_kb / 1024 / 1024)) mode=$disk_mode reuse_stop_data=$REUSE_STOP_DATA"
  if (( free_kb <= warning_free_kb )); then
    echo "[Nightly] WARNING: disk free is at or below warning threshold for $disk_mode" >&2
  fi
  if (( free_kb < minimum_free_kb || estimated_free_kb < minimum_free_kb )); then
    echo "[Nightly] ERROR: insufficient disk for $disk_mode" >&2
    return 1
  fi
}

if [[ "$NO_ACTIVATE" == "1" && "$RUN_MODE" == "normal" ]]; then
  proof_disk_preflight
fi

run_build_stage() {
  diagnostics_set_stage "stop-data-build"
  echo "[StopData] release=$RELEASE_ID stage=build started"

mkdir -p "$BUILD_DIR" "$RELEASES"
if [[ -f "$MVO_ENV_FILE" ]]; then
  echo "[StopData] refreshing Austrian MVO GTFS sources"
  python3 "$REPO/scripts/download_austrian_gtfs.py" \
    --registry "$REPO/config/austrian-sources.json" \
    --env-file "$MVO_ENV_FILE" \
    --output "$AUSTRIAN_DATA_ROOT" \
    --output-json "$RELEASE_DIR/austrian-artifacts.json"
fi

EXTERNAL_URL_OVERRIDES=()
if [[ -n "${SWEDEN_GTFS_URL:-}" ]]; then
  EXTERNAL_URL_OVERRIDES+=(--external-gtfs-url "sweden=$SWEDEN_GTFS_URL")
fi
PREPARE_ARGS=(
  --cache-root "${GTFS_CACHE_ROOT:-/srv/haltewecker/cache/gtfs}"
  --gtfs-url "$GTFS_URL"
  --swiss-gtfs-url "$SWISS_GTFS_URL"
  --nl-gtfs-url "${NL_GTFS_URL:-}"
  --external-sources "$REPO/config/external-gtfs-sources.json"
)
if [[ ${#EXTERNAL_URL_OVERRIDES[@]} -gt 0 ]]; then
  PREPARE_ARGS+=("${EXTERNAL_URL_OVERRIDES[@]}")
fi
PREPARE_ARGS+=(--output "$ARTIFACTS_JSON")
PREPARE_ARGS+=(--release-root "$RELEASE_DIR")
python3 "$REPO/scripts/prepare_gtfs_artifacts.py" "${PREPARE_ARGS[@]}"
VBB_INPUT_URL="${VBB_GTFS_URL:-https://unternehmen.vbb.de/fileadmin/user_upload/VBB/Dokumente/API-Datensaetze/gtfs-mastscharf/GTFS.zip}"
RNV_INPUT_URL="${RNV_GTFS_URL:-https://gtfs-sandbox-dds.rnv-online.de/latest/gtfs.zip}"
python3 "$REPO/scripts/prepare_custom_gtfs_artifacts.py" \
  --cache-root "${GTFS_CACHE_ROOT:-/srv/haltewecker/cache/gtfs}" \
  --vbb-url "$VBB_INPUT_URL" \
  --rnv-url "$RNV_INPUT_URL" \
  --output "$CUSTOM_ARTIFACTS_JSON"
BUILD_FINGERPRINT="$(python3 "$REPO/scripts/build_fingerprint.py" --repository "$REPO")"
echo "[StopData] release=$RELEASE_ID buildFingerprint=$BUILD_FINGERPRINT"

ARTIFACT_VALUES=()
while IFS= read -r artifact_value; do
  ARTIFACT_VALUES+=("$artifact_value")
done < <(python3 - "$ARTIFACTS_JSON" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
sources = payload["sources"]
print(sources["germany"]["path"])
print(sources["swiss"]["path"])
print(sources.get("netherlands", {}).get("path", ""))
print(payload.get("nlFailure") or "")
for source_id, entry in sorted(payload.get("external", {}).items()):
    if isinstance(entry, dict) and entry.get("path"):
        print(f"external:{source_id}={entry['path']}")
PY
)
GTFS_URL="${ARTIFACT_VALUES[0]}"
SWISS_GTFS_URL="${ARTIFACT_VALUES[1]}"
NL_GTFS_URL="${ARTIFACT_VALUES[2]}"
NL_SOURCE_FAILED="${ARTIFACT_VALUES[3]}"
EXTERNAL_GTFS_ARGS=()
for value in "${ARTIFACT_VALUES[@]:4}"; do
  EXTERNAL_GTFS_ARGS+=(--external-gtfs-url "${value#external:}")
done

CUSTOM_ARTIFACT_VALUES=()
while IFS= read -r artifact_value; do
  CUSTOM_ARTIFACT_VALUES+=("$artifact_value")
done < <(python3 - "$CUSTOM_ARTIFACTS_JSON" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
for source_id in ("vbb", "rnv"):
    print(payload["sources"][source_id]["path"])
PY
)
VBB_GTFS_ARTIFACT="${CUSTOM_ARTIFACT_VALUES[0]}"
RNV_GTFS_ARTIFACT="${CUSTOM_ARTIFACT_VALUES[1]}"

mkdir -p "$BUILD_DIR" "$RELEASES"

# Map provider env vars to repeatable --external-gtfs-url providerID=URL args.
# Add future countries here without changing build_stop_packages.py.
run_build_stop_packages() {
  local nl_url="${1-}"
  local -a cmd
  cmd=(
    python3 "$REPO/scripts/build_stop_packages.py"
    --gtfs-url "$GTFS_URL"
    --swiss-gtfs-url "$SWISS_GTFS_URL"
    --austrian-sources "$REPO/config/austrian-sources.json"
    --external-gtfs-sources "$REPO/config/external-gtfs-sources.json"
  )
  if [[ -d "$AUSTRIAN_DATA_ROOT" && -f "$AUSTRIAN_DATA_ROOT/.env" ]]; then
    cmd+=(--austrian-gtfs-dir "$AUSTRIAN_DATA_ROOT")
  else
    cmd+=(--austrian-gtfs-url "${AUSTRIAN_GTFS_URL:-}")
  fi
  cmd+=(--vbb-gtfs-url "$VBB_GTFS_ARTIFACT")
  cmd+=(--rnv-gtfs-url "$RNV_GTFS_ARTIFACT")
  cmd+=(--kyiv-cache-root "${KYIV_OPEN_DATA_CACHE_ROOT:-$DATA_ROOT/kyiv-open-data-cache}")
  cmd+=(--gtfs-cache-root "${GTFS_CACHE_ROOT:-/srv/haltewecker/cache/gtfs}")
  cmd+=(--previous-stop-data "$CURRENT")
  if [ -n "$nl_url" ]; then
    cmd+=(--nl-gtfs-url "$nl_url")
  fi
  if [[ ${#EXTERNAL_GTFS_ARGS[@]} -gt 0 ]]; then
    cmd+=("${EXTERNAL_GTFS_ARGS[@]}")
  fi
  cmd+=(--output "$BUILD_DIR")
  if [[ -n "${NL_GTFS_URL:-}" ]]; then
    cmd+=(--allow-nl-failure)
  fi
  "${cmd[@]}"
}

run_build_stop_packages "${NL_GTFS_URL:-}"

if [[ "${FORCE_PRESERVE_NL:-0}" = "1" || -n "$NL_SOURCE_FAILED" || -f "$BUILD_DIR/.nl-failure" ]]; then
  test -d "$CURRENT"
  echo "[StopData] release=$RELEASE_ID preserving last validated Dutch assets"
  python3 "$REPO/scripts/preserve_nl_assets.py" \
    --current "$CURRENT" \
    --output "$BUILD_DIR" \
    --cities "$REPO/config/cities.json"
  rm -f "$BUILD_DIR/.nl-failure"
fi

SWISS_INDEX_STARTED=$SECONDS
python3 "$REPO/scripts/build_swiss_departure_index.py" \
  --gtfs-url "$SWISS_GTFS_URL" \
  --output "$BUILD_DIR/swiss-static"
echo "[StopData] source=swiss stage=departure-index duration=$(elapsed_seconds "$SWISS_INDEX_STARTED")"

test -f "$BUILD_DIR/manifest.json"
test -f "$BUILD_DIR/transit-radar-cities.json"
test -f "$BUILD_DIR/swiss-static/manifest.json"
test -f "$BUILD_DIR/provenance/input-artifacts.json"
if [[ -f "$MVO_ENV_FILE" ]]; then
  python3 "$REPO/scripts/validate_austrian_stop_packages.py" \
    --stop-data "$BUILD_DIR" \
    --registry "$REPO/config/austrian-sources.json"
fi

echo "[StopData] release=$RELEASE_ID stage=build duration=$(elapsed_seconds "$TOTAL_STARTED")"
}
run_candidate_validation() {
  diagnostics_set_stage "stop-data-validation"
  VALIDATION_STARTED=$SECONDS
  test -f "$CUSTOM_ARTIFACTS_JSON"
if [[ -f "$MVO_ENV_FILE" ]]; then
  test -f "$RELEASE_DIR/austrian-artifacts.json"
fi
python3 - "$BUILD_DIR/manifest.json" "$RELEASE_ID" "$RELEASE_DIR/release-metadata.json" "$BUILD_FINGERPRINT" "$ARTIFACTS_JSON" "$CURRENT_RELEASE" "$REPO" "$REUSE_STOP_DATA" <<'PY'
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
release_id = sys.argv[2]
metadata_path = Path(sys.argv[3])
build_fingerprint = sys.argv[4]
artifacts_path = Path(sys.argv[5])
previous_release_link = Path(sys.argv[6])
repository_root = Path(sys.argv[7])
reuse_stop_data = sys.argv[8] == "1"
candidate_root = manifest_path.parent.parent
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if not isinstance(manifest.get("cities"), list) or not manifest["cities"]:
    raise SystemExit("staged stop manifest has no cities")
manifest["releaseID"] = release_id
artifacts = json.loads(artifacts_path.read_text(encoding="utf-8"))
source_artifacts = {}
for group in ("sources", "external"):
    for source_id, entry in (artifacts.get(group) or {}).items():
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        if not isinstance(entry.get("sha256"), str) or not entry["sha256"]:
            raise SystemExit(f"GTFS artifact provenance is missing for {source_id}")
        if not isinstance(entry.get("size"), int) or entry["size"] <= 0:
            raise SystemExit(f"GTFS artifact size provenance is missing for {source_id}")
        artifact_path = Path(str(entry["path"]))
        sys.path.insert(0, str(repository_root / "scripts"))
        if source_id == "ireland":
            from ireland_artifact_snapshot import validate_ireland_release_snapshot

            try:
                validate_ireland_release_snapshot(entry, candidate_root)
            except ValueError as error:
                raise SystemExit(str(error)) from error
        if not artifact_path.exists():
            raise SystemExit(f"GTFS artifact path is missing for {source_id}: {artifact_path}")
        from artifact_provenance import artifact_provenance
        actual_digest, actual_size = artifact_provenance(artifact_path)
        if actual_digest != entry["sha256"] or actual_size != entry["size"]:
            raise SystemExit(f"GTFS artifact checksum/path mismatch for {source_id}")
        source_artifacts[str(source_id)] = {
            "sha256": entry["sha256"],
            "size": entry.get("size"),
        }
if not source_artifacts:
    raise SystemExit("No GTFS artifact provenance was produced for stop-data")
registry_path = repository_root / "config" / "external-gtfs-sources.json"
registry = json.loads(registry_path.read_text(encoding="utf-8"))
candidate_external = artifacts.get("external") or {}
manifest_city_ids = {
    str(city.get("id"))
    for city in manifest.get("cities", [])
    if isinstance(city, dict) and city.get("id")
}
sys.path.insert(0, str(repository_root / "scripts"))
from release_integrity import validate_candidate_sources
from release_integrity import (
    validate_previous_release_city_retirements,
    validate_previous_release_cities,
    validate_previous_release_sources,
)
from release_integrity import validate_artifact_entry
try:
    validate_candidate_sources(
        registry, candidate_external, manifest_city_ids, repository_root
    )
except ValueError as error:
    raise SystemExit(str(error)) from error
for source in registry:
    source_id = str(source["id"])
    classification = str(source.get("classification", "required"))
    active = classification == "required"
    if classification == "conditional":
        activation_env = str(source.get("activationEnv", ""))
        active = bool(
            activation_env and __import__("os").environ.get(activation_env, "").strip()
        )
    entry = candidate_external.get(source_id)
    if active and (not isinstance(entry, dict) or not entry.get("path")):
        raise SystemExit(f"Expected external source is missing from candidate: {source_id}")
    if source.get("importIntoStaticDepartures") is True:
        if not isinstance(entry, dict) or not entry.get("path"):
            raise SystemExit(f"Static-enabled source is missing from import plan: {source_id}")
    if source_id == "511-bay-area":
        city_file = repository_root / str(source["cities"])
        expected_cities = {
            str(city["id"])
            for city in json.loads(city_file.read_text(encoding="utf-8"))
        }
        missing = sorted(expected_cities - manifest_city_ids)
        if missing:
            raise SystemExit(
                "511 candidate city coverage is incomplete: " + ", ".join(missing)
            )

supplemental = {}
custom_path = candidate_root / "custom-gtfs-artifacts.json"
if custom_path.is_file():
    custom = json.loads(custom_path.read_text(encoding="utf-8"))
    for source_id, entry in (custom.get("sources") or {}).items():
        if not isinstance(entry, dict) or not entry.get("path"):
            raise SystemExit(f"Custom artifact provenance is missing for {source_id}")
        supplemental[str(source_id)] = entry
        try:
            validate_artifact_entry(
                str(source_id), entry, base_dir=candidate_root
            )
        except ValueError as error:
            raise SystemExit(str(error)) from error
    if set(custom.get("sources") or {}) != {"vbb", "rnv"}:
        raise SystemExit("Custom GTFS provenance must contain exactly vbb and rnv")
austria_path = candidate_root / "austrian-artifacts.json"
if austria_path.is_file():
    austria = json.loads(austria_path.read_text(encoding="utf-8"))
    expected_austria = {
        "vor", "steiermark", "salzburg", "kaernten",
        "ooevv", "tirol", "vorarlberg", "linz-ag",
    }
    actual_austria = {}
    for entry in austria.get("sources", []):
        source_id = str(entry.get("source", ""))
        if source_id in expected_austria:
            if not entry.get("sha256") or not isinstance(entry.get("size"), int):
                raise SystemExit(f"Austrian provenance is incomplete for {source_id}")
            try:
                validate_artifact_entry(
                    source_id, entry, base_dir=candidate_root
                )
            except ValueError as error:
                raise SystemExit(str(error)) from error
            actual_austria[source_id] = entry
    if set(actual_austria) != expected_austria:
        raise SystemExit("Austrian provenance does not contain all eight configured sources")
    supplemental.update(actual_austria)
input_path = candidate_root / "stop-data" / "provenance" / "input-artifacts.json"
if input_path.is_file():
    inputs = json.loads(input_path.read_text(encoding="utf-8"))
    for source_id, entry in (inputs.get("sources") or {}).items():
        if (
            not isinstance(entry, dict)
            or not entry.get("sha256")
            or not isinstance(entry.get("size"), int)
        ):
            raise SystemExit(f"Input provenance is incomplete for {source_id}")
        supplemental[str(source_id)] = entry
systems_path = candidate_root / "stop-data" / "transit" / "kyiv-systems.json"
if systems_path.is_file():
    systems = json.loads(systems_path.read_text(encoding="utf-8"))
    systems_source = systems.get("source") or {}
    if systems_source.get("contentDigest") and isinstance(systems_source.get("contentSize"), int):
        supplemental["kyiv-systems"] = {
            "sourceID": "kyiv-systems",
            "path": "transit/kyiv-systems.json",
            "sha256": systems_source["contentDigest"],
            "size": systems_source["contentSize"],
            "origin": "Kyiv Open Data Portal systems resources",
            "status": systems_source.get("provenanceStatus", "used"),
        }
    else:
        raise SystemExit("Kyiv systems provenance is incomplete")
artifacts["supplemental"] = supplemental
artifacts_path.write_text(
    json.dumps(artifacts, ensure_ascii=False, indent=2), encoding="utf-8"
)
old_root = (
    previous_release_link.resolve()
    if previous_release_link.is_symlink()
    else previous_release_link
)
old_artifacts_path = old_root / "gtfs-artifacts.json"
old_artifacts = None
if old_artifacts_path.is_file():
    old_artifacts = json.loads(old_artifacts_path.read_text(encoding="utf-8"))
    old_ids = {
        str(source_id)
        for group in ("sources", "external")
        for source_id, entry in (old_artifacts.get(group) or {}).items()
        if isinstance(entry, dict) and entry.get("path")
    }
    try:
        validate_previous_release_sources(old_ids, set(source_artifacts))
    except ValueError as error:
        raise SystemExit(str(error)) from error
    missing_old = sorted(
        source_id for source_id in old_ids if source_id not in source_artifacts
    )
    if missing_old:
        raise SystemExit(
            "Candidate lost sources from active release: " + ", ".join(missing_old)
        )
old_manifest_path = old_root / "stop-data" / "manifest.json"
if old_manifest_path.is_file():
    old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    old_city_ids = {
        str(city.get("id"))
        for city in old_manifest.get("cities", [])
        if isinstance(city, dict) and city.get("id")
    }
    try:
        if isinstance(old_artifacts, dict):
            retirements = validate_previous_release_city_retirements(
                old_manifest=old_manifest,
                candidate_manifest=manifest,
                active_stop_data=old_root / "stop-data",
                candidate_stop_data=manifest_path.parent,
                active_artifacts=old_artifacts,
                candidate_artifacts=artifacts,
                registry=registry,
                repository_root=repository_root,
                candidate_artifacts_root=candidate_root,
            )
            if retirements:
                print(
                    "[StopData] legitimate source retirements="
                    + json.dumps(retirements, ensure_ascii=False, sort_keys=True)
                )
        else:
            validate_previous_release_cities(old_city_ids, manifest_city_ids)
    except ValueError as error:
        raise SystemExit(str(error)) from error
if reuse_stop_data:
    if manifest.get("sourceArtifacts") != source_artifacts:
        raise SystemExit("reused stop-data source provenance differs from candidate artifacts")
    if not isinstance(manifest.get("inputProvenance"), dict):
        raise SystemExit("reused stop-data source has no inputProvenance")
else:
    manifest["sourceArtifacts"] = source_artifacts
    manifest["inputProvenance"] = supplemental
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
metadata_path.write_text(
    json.dumps(
        {
            "releaseID": release_id,
            "buildFingerprint": build_fingerprint,
            "stopManifestVersion": manifest.get("version"),
            "sourceArtifacts": source_artifacts,
            "inputProvenance": supplemental,
        },
        indent=2,
    ),
    encoding="utf-8",
)
PY
  echo "[StopData] release=$RELEASE_ID stage=validation duration=$(elapsed_seconds "$VALIDATION_STARTED")"
}

run_static_departures_stage() {
  diagnostics_set_stage "legacy-import"
  STATIC_STARTED=$SECONDS
  EXTERNAL_GTFS_ARTIFACTS_JSON="$ARTIFACTS_JSON" \
STOP_DATA_PATH="$BUILD_DIR" \
NEXT_DATABASE_PATH="$RELEASE_DIR/departures.sqlite" \
RELEASE_ID="$RELEASE_ID" \
SKIP_ACTIVATION=1 \
  "$STATIC_DEPARTURES_PIPELINE"
  python3 "$REPO/scripts/validate_release_consistency.py" --release-dir "$RELEASE_DIR"

  echo "[StaticDepartures] release=$RELEASE_ID stage=import duration=$(elapsed_seconds "$STATIC_STARTED")"

if [ -n "${SWEDEN_GTFS_URL:-}" ]; then
  for sweden_city in stockholm malmo goteborg uppsala vaxjo helsingborg linkoping jonkoping orebro vasteras; do
    test -f "$BUILD_DIR/stops/$sweden_city.json"
    test -f "$BUILD_DIR/routes/$sweden_city.json"
    test -f "$BUILD_DIR/departures/$sweden_city.json"
  done
  python3 - "$BUILD_DIR/manifest.json" "$BUILD_DIR/transit-radar-cities.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
radar = json.load(open(sys.argv[2], encoding="utf-8"))
expected = {
    "stockholm", "malmo", "goteborg", "uppsala", "vaxjo",
    "helsingborg", "linkoping", "jonkoping", "orebro", "vasteras",
}
manifest_ids = [city.get("id") for city in manifest.get("cities", [])]
radar_ids = [city.get("appCityID") for city in radar.get("cities", [])]
if any(manifest_ids.count(city_id) != 1 for city_id in expected):
    raise SystemExit("manifest must contain every Swedish city exactly once")
if not expected.issubset(manifest_ids):
    raise SystemExit("manifest is missing a Swedish city")
if any(radar_ids.count(city_id) != 1 for city_id in expected):
    raise SystemExit("transit-radar-cities must contain every Swedish city exactly once")
if not expected.issubset(radar_ids):
    raise SystemExit("transit-radar-cities is missing a Swedish city")
print("[StopData] Sweden external packages validated")
PY
fi

if [[ -d "$RELEASE_DIR/external-artifacts/ireland" ]]; then
  for ireland_city in dublin cork galway limerick waterford; do
    test -f "$BUILD_DIR/stops/$ireland_city.json"
    test -f "$BUILD_DIR/routes/$ireland_city.json"
    test -f "$BUILD_DIR/departures/$ireland_city.json"
  done
  python3 - "$BUILD_DIR/manifest.json" "$BUILD_DIR/transit-radar-cities.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
radar = json.load(open(sys.argv[2], encoding="utf-8"))
expected = {"dublin", "cork", "galway", "limerick", "waterford"}
manifest_ids = {city.get("id") for city in manifest.get("cities", [])}
radar_ids = {city.get("appCityID") for city in radar.get("cities", [])}
if not expected.issubset(manifest_ids) or not expected.issubset(radar_ids):
    raise SystemExit("Ireland stop or radar manifest is incomplete")
print("[StopData] Ireland external packages validated")
PY
fi
}

if [[ "$RUN_MODE" == "normal" ]]; then
  if [[ "$REUSE_STOP_DATA" == "1" ]]; then
    echo "[Nightly] stage=stop-data-build status=SKIPPED release=$RELEASE_ID reason=reuse-stop-data"
  else
    echo "[Nightly] stage=stop-data-build status=started release=$RELEASE_ID"
    run_build_stage
    echo "[Nightly] stage=stop-data-build status=PASS release=$RELEASE_ID"
  fi
  if [[ "$REUSE_STOP_DATA" != "1" ]]; then
    persist_release_stage "build"
  else
    echo "[StopData] release=$RELEASE_ID state persistence skipped for read-only reused stop-data"
  fi
  echo "[Nightly] stage=validation status=started release=$RELEASE_ID"
  run_candidate_validation
  echo "[Nightly] stage=validation status=PASS release=$RELEASE_ID"
  if [[ "$REUSE_STOP_DATA" != "1" ]]; then
    persist_release_stage "candidate-validation"
  fi
  log_disk_state "after-stop-data"
  if [[ "$STOP_DATA_ONLY" == "1" ]]; then
    diagnostics_set_stage "stop-data-only"
    echo "[Nightly] stage=stop-data-validation status=PASS release=$RELEASE_ID"
    echo "[Nightly] stage=legacy-import status=SKIPPED release=$RELEASE_ID reason=stop-data-only"
    STOP_DATA_SIZE_BYTES="$(du -skL "$BUILD_DIR" | awk '{print $1 * 1024; exit}')"
    echo "[Nightly] stage=stop-data-only status=PASS release=$RELEASE_ID path=$BUILD_DIR size_bytes=$STOP_DATA_SIZE_BYTES buildFingerprint=$BUILD_FINGERPRINT"
    log_disk_peak
    exit 0
  fi
  if [[ "$NO_ACTIVATE" == "1" ]]; then
    if [[ "$INCREMENTAL_NO_ACTIVATE" == "1" ]]; then
      echo "[Nightly] stage=legacy-import status=SKIPPED release=$RELEASE_ID reason=production-shaped-no-activate"
    else
      echo "[Nightly] stage=legacy-import status=SKIPPED release=$RELEASE_ID reason=no-activate-proof"
    fi
  else
    echo "[Nightly] stage=legacy-import status=started release=$RELEASE_ID"
    run_static_departures_stage
    echo "[Nightly] stage=legacy-import status=PASS release=$RELEASE_ID"
    persist_release_stage "static-departures"
  fi
else
  inspect_resume
  if [[ "$RESUME_STATUS" == "already-active" ]]; then
    if [[ "$RESUME_COMPLETED_STAGE" != "commit" ]]; then
      persist_release_stage "commit"
    fi
    echo "[StopData] release=$RELEASE_ID resume=true already-active; state reconciled without activation"
    exit 0
  fi
  case "$RESUME_COMPLETED_STAGE" in
    build)
      echo "[StopData] release=$RELEASE_ID resume=true skipping_completed_stage=build"
      run_candidate_validation
      persist_release_stage "candidate-validation"
      run_static_departures_stage
      persist_release_stage "static-departures"
      ;;
    candidate-validation)
      echo "[StopData] release=$RELEASE_ID resume=true skipping_completed_stage=build"
      echo "[StopData] release=$RELEASE_ID resume=true skipping_completed_stage=candidate-validation"
      run_static_departures_stage
      persist_release_stage "static-departures"
      ;;
    static-departures)
      echo "[StopData] release=$RELEASE_ID resume=true skipping_completed_stage=build"
      echo "[StopData] release=$RELEASE_ID resume=true skipping_completed_stage=candidate-validation"
      echo "[StopData] release=$RELEASE_ID resume=true skipping_completed_stage=static-departures"
      ;;
    handoff-readiness)
      echo "[StopData] release=$RELEASE_ID resume=true skipping_completed_stage=build"
      echo "[StopData] release=$RELEASE_ID resume=true skipping_completed_stage=candidate-validation"
      echo "[StopData] release=$RELEASE_ID resume=true skipping_completed_stage=static-departures"
      echo "[StopData] release=$RELEASE_ID resume=true skipping_completed_stage=handoff-readiness"
      ;;
    *)
      echo "[StopData] ERROR: unsupported resume stage: $RESUME_COMPLETED_STAGE" >&2
      exit 1
      ;;
  esac
fi

if [[ "$NO_ACTIVATE" == "1" ]]; then
  diagnostics_set_stage "incremental-provider"
  NORMALIZED_CACHE_ROOT="${HALTEWECKER_NORMALIZED_PROVIDER_CACHE_ROOT:-${DATA_ROOT}/provider-artifacts/normalized}"
  STATIC_ARTIFACT_ROOT="${HALTEWECKER_STATIC_PROVIDER_ARTIFACT_ROOT:-${DATA_ROOT}/provider-artifacts/static}"
  INCREMENTAL_STARTED=$SECONDS
  echo "[Nightly] stage=incremental-provider status=started release=$RELEASE_ID"
  INCREMENTAL_ARGS=(
    --repository-root "$REPO"
    --release-id "$RELEASE_ID"
    --releases-root "$INCREMENTAL_RELEASES_ROOT"
    --stop-data "$BUILD_DIR"
    --gtfs-artifacts "$ARTIFACTS_JSON"
    --normalized-cache-root "$NORMALIZED_CACHE_ROOT"
    --static-artifact-root "$STATIC_ARTIFACT_ROOT"
  )
  if [[ "$INCREMENTAL_NO_ACTIVATE" == "1" ]]; then
    INCREMENTAL_ARGS+=(--result-json "$RELEASE_DIR/incremental-result.json")
  fi
  python3 "$REPO/scripts/run_incremental_provider_pipeline.py" \
    "${INCREMENTAL_ARGS[@]}"
  if [[ "$INCREMENTAL_NO_ACTIVATE" == "1" ]]; then
    python3 - "$RELEASE_DIR/incremental-result.json" "$RELEASE_ID" "$BUILD_DIR" "$BUILD_FINGERPRINT" <<'PY'
import json
import sys
from pathlib import Path

result_path = Path(sys.argv[1])
release_id = sys.argv[2]
stop_data_path = Path(sys.argv[3]).resolve()
expected_fingerprint = sys.argv[4]
result = json.loads(result_path.read_text(encoding="utf-8"))
if result.get("releaseID") != release_id:
    raise SystemExit("incremental result releaseID does not match stop-data releaseID")
stop_data = result.get("stopData")
if not isinstance(stop_data, dict):
    raise SystemExit("incremental result has no stopData metadata")
if stop_data.get("releaseID") != release_id:
    raise SystemExit("incremental result stop-data generation does not match releaseID")
if stop_data.get("buildFingerprint") != expected_fingerprint:
    raise SystemExit("incremental result stop-data fingerprint does not match current build")
if Path(str(stop_data.get("path", ""))).resolve() != stop_data_path:
    raise SystemExit("incremental result stop-data path does not match fresh generation")
candidate = Path(str(result.get("releaseDirectory", ""))).resolve()
if not candidate.is_dir() or not (candidate / "release.json").is_file():
    raise SystemExit("incremental result candidate is not a published release")
print(
    "[Nightly] stage=incremental-metadata status=PASS "
    f"release={release_id} candidate={candidate} fingerprint={expected_fingerprint}"
)
PY
  fi
  if [[ "$REUSE_STOP_DATA" == "1" ]]; then
    verify_reused_stop_data_unchanged
  fi
  echo "[Nightly] stage=incremental-provider status=PASS release=$RELEASE_ID duration=$((SECONDS - INCREMENTAL_STARTED))s"
  echo "[Nightly] stage=readiness status=PASS release=$RELEASE_ID no_activate=true"
  if [[ "$INCREMENTAL_NO_ACTIVATE" == "1" ]]; then
    echo "[Nightly] stage=nightly-complete status=PASS release=$RELEASE_ID mode=production-shaped-no-activate activation=NOT_RUN"
  else
    echo "[Nightly] stage=nightly-complete status=PASS release=$RELEASE_ID no_activate=true"
  fi
  log_disk_state "after"
  log_disk_peak
  exit 0
fi

OLD_RELEASE_TARGET=""
if [[ -L "$CURRENT_RELEASE" ]]; then
  OLD_RELEASE_TARGET="$(readlink "$CURRENT_RELEASE")"
fi
if [[ "$RUN_MODE" == "normal" || "$RESUME_COMPLETED_STAGE" != "handoff-readiness" ]]; then
  if ! prepare_runtime; then
    exit 1
  fi
  persist_release_stage "handoff-readiness"
else
  echo "[StopData] release=$RELEASE_ID resume=true skipping_completed_stage=handoff-readiness"
fi
mkdir -p "$ROLLBACK"
if [[ -d "$CURRENT" && ! -L "$CURRENT" ]]; then
  mv "$CURRENT" "$ROLLBACK/stop-data"
fi
if [[ -f "$DEPARTURES_CURRENT" && ! -L "$DEPARTURES_CURRENT" ]]; then
  mv "$DEPARTURES_CURRENT" "$ROLLBACK/departures.sqlite"
fi
if [[ -z "$OLD_RELEASE_TARGET" && -e "$ROLLBACK/stop-data" ]]; then
  LEGACY_RELEASE_ID="legacy-$RELEASE_ID"
  mkdir -p "$RELEASES/$LEGACY_RELEASE_ID"
  mv "$ROLLBACK/stop-data" "$RELEASES/$LEGACY_RELEASE_ID/stop-data"
  if [[ -e "$ROLLBACK/departures.sqlite" ]]; then
    mv "$ROLLBACK/departures.sqlite" "$RELEASES/$LEGACY_RELEASE_ID/departures.sqlite"
  elif [[ -e "$DEPARTURES_CURRENT" ]]; then
    OLD_DATABASE_PATH="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$DEPARTURES_CURRENT")"
    ln -s "$OLD_DATABASE_PATH" "$RELEASES/$LEGACY_RELEASE_ID/departures.sqlite"
  fi
  OLD_RELEASE_TARGET="releases/$LEGACY_RELEASE_ID"
fi
COMMIT_STARTED=$SECONDS
echo "[StopData] release=$RELEASE_ID stage=commit started"
replace_link "$CURRENT_RELEASE" "releases/$RELEASE_ID"
replace_link "$CURRENT" "releases/$RELEASE_ID/stop-data"
replace_link "$DEPARTURES_CURRENT" "releases/$RELEASE_ID/departures.sqlite"

if ! activate_runtime; then
  echo "[StopData] ERROR: release=$RELEASE_ID readiness failed; restoring previous release" >&2
  if [[ -n "$OLD_RELEASE_TARGET" ]]; then
    replace_link "$CURRENT_RELEASE" "$OLD_RELEASE_TARGET"
    replace_link "$CURRENT" "$OLD_RELEASE_TARGET/stop-data"
    replace_link "$DEPARTURES_CURRENT" "$OLD_RELEASE_TARGET/departures.sqlite"
  else
    rm -f "$CURRENT_RELEASE" "$CURRENT" "$DEPARTURES_CURRENT"
    [[ -e "$ROLLBACK/stop-data" ]] && mv "$ROLLBACK/stop-data" "$CURRENT"
    [[ -e "$ROLLBACK/departures.sqlite" ]] && mv "$ROLLBACK/departures.sqlite" "$DEPARTURES_CURRENT"
  fi
  exit 1
fi

# Publish the handoff only after the release has passed runtime readiness and
# all canonical release pointers have been activated.
replace_link "$STATIC_DEPARTURES_RELEASE" "releases/$RELEASE_ID"

if [[ -n "$OLD_RELEASE_TARGET" ]]; then
  mkdir -p "$(dirname "$PREVIOUS")"
  rm -rf "$PREVIOUS"
  ln -s "../releases/${OLD_RELEASE_TARGET#releases/}/stop-data" "$PREVIOUS"
fi
persist_release_stage "commit"
echo "[StopData] release=$RELEASE_ID stage=commit duration=$(elapsed_seconds "$COMMIT_STARTED")"
echo "[StopData] release=$RELEASE_ID total duration=$(elapsed_seconds "$TOTAL_STARTED")"
