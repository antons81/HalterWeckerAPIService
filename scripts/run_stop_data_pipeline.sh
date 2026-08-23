#!/usr/bin/env bash
set -euo pipefail
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

REPO="${REPO:-/srv/haltewecker/pipeline/HalterWeckerAPIService}"
DATA_ROOT="${DATA_ROOT:-/srv/haltewecker/data}"
RELEASES="$DATA_ROOT/releases"
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RELEASE_DIR="$RELEASES/$RELEASE_ID"
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
# for the downstream nightly static-departures timer.
rm -f "$STATIC_DEPARTURES_RELEASE"

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
python3 "$REPO/scripts/prepare_gtfs_artifacts.py" "${PREPARE_ARGS[@]}"
VBB_INPUT_URL="${VBB_GTFS_URL:-https://unternehmen.vbb.de/fileadmin/user_upload/VBB/Dokumente/API-Datensaetze/gtfs-mastscharf/GTFS.zip}"
RNV_INPUT_URL="${RNV_GTFS_URL:-https://gtfs-sandbox-dds.rnv-online.de/latest/gtfs.zip}"
CUSTOM_ARTIFACTS_JSON="$RELEASE_DIR/custom-gtfs-artifacts.json"
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
VALIDATION_STARTED=$SECONDS
test -f "$CUSTOM_ARTIFACTS_JSON"
if [[ -f "$MVO_ENV_FILE" ]]; then
  test -f "$RELEASE_DIR/austrian-artifacts.json"
fi
python3 - "$BUILD_DIR/manifest.json" "$RELEASE_ID" "$RELEASE_DIR/release-metadata.json" "$BUILD_FINGERPRINT" "$ARTIFACTS_JSON" "$CURRENT_RELEASE" "$REPO" <<'PY'
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
        if not artifact_path.exists():
            raise SystemExit(f"GTFS artifact path is missing for {source_id}: {artifact_path}")
        sys.path.insert(0, str(repository_root / "scripts"))
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
custom_path = artifacts_path.parent / "custom-gtfs-artifacts.json"
if custom_path.is_file():
    custom = json.loads(custom_path.read_text(encoding="utf-8"))
    for source_id, entry in (custom.get("sources") or {}).items():
        if not isinstance(entry, dict) or not entry.get("path"):
            raise SystemExit(f"Custom artifact provenance is missing for {source_id}")
        supplemental[str(source_id)] = entry
        try:
            validate_artifact_entry(
                str(source_id), entry, base_dir=artifacts_path.parent
            )
        except ValueError as error:
            raise SystemExit(str(error)) from error
    if set(custom.get("sources") or {}) != {"vbb", "rnv"}:
        raise SystemExit("Custom GTFS provenance must contain exactly vbb and rnv")
austria_path = artifacts_path.parent / "austrian-artifacts.json"
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
                    source_id, entry, base_dir=artifacts_path.parent
                )
            except ValueError as error:
                raise SystemExit(str(error)) from error
            actual_austria[source_id] = entry
    if set(actual_austria) != expected_austria:
        raise SystemExit("Austrian provenance does not contain all eight configured sources")
    supplemental.update(actual_austria)
input_path = artifacts_path.parent / "stop-data" / "provenance" / "input-artifacts.json"
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
systems_path = artifacts_path.parent / "stop-data" / "transit" / "kyiv-systems.json"
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
    missing_old_cities = sorted(old_city_ids - manifest_city_ids)
    try:
        validate_previous_release_cities(old_city_ids, manifest_city_ids)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if missing_old_cities:
        raise SystemExit(
            "Candidate lost cities from active release: " + ", ".join(missing_old_cities)
        )
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

if [[ -d "$DATA_ROOT/ireland/static" ]]; then
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

OLD_RELEASE_TARGET=""
if [[ -L "$CURRENT_RELEASE" ]]; then
  OLD_RELEASE_TARGET="$(readlink "$CURRENT_RELEASE")"
fi
if ! prepare_runtime; then
  exit 1
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
echo "[StopData] release=$RELEASE_ID stage=commit duration=$(elapsed_seconds "$COMMIT_STARTED")"
echo "[StopData] release=$RELEASE_ID total duration=$(elapsed_seconds "$TOTAL_STARTED")"
