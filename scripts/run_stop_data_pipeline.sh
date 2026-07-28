#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-/srv/haltewecker/pipeline/HalterWeckerAPIService}"
DATA_ROOT="${DATA_ROOT:-/srv/haltewecker/data}"
STAGING="$DATA_ROOT/temp/stop-data"
BUILD_DIR="$STAGING/data"
CURRENT="$DATA_ROOT/current"
PREVIOUS="$DATA_ROOT/previous/stop-data"
ROLLBACK="$DATA_ROOT/temp/current-rollback"

source /etc/haltewecker-stop-data.env

cd "$REPO"

rm -rf "$STAGING" "$ROLLBACK"
mkdir -p "$STAGING"

if [ "${FORCE_PRESERVE_NL:-0}" = "1" ]; then
  BUILD_WITHOUT_NL=1
elif ! python3 "$REPO/scripts/build_stop_packages.py" \
  --gtfs-url "$GTFS_URL" \
  --swiss-gtfs-url "$SWISS_GTFS_URL" \
  --nl-gtfs-url "$NL_GTFS_URL" \
  --output "$BUILD_DIR"; then
  BUILD_WITHOUT_NL=1
else
  BUILD_WITHOUT_NL=0
fi

if [ "$BUILD_WITHOUT_NL" = "1" ]; then
  test -d "$CURRENT"
  echo "Preserving the last published Dutch assets."
  rm -rf "$BUILD_DIR"
  python3 "$REPO/scripts/build_stop_packages.py" \
    --gtfs-url "$GTFS_URL" \
    --swiss-gtfs-url "$SWISS_GTFS_URL" \
    --output "$BUILD_DIR"
  python3 "$REPO/scripts/preserve_nl_assets.py" \
    --current "$CURRENT" \
    --output "$BUILD_DIR" \
    --cities "$REPO/config/cities.json"
fi

python3 "$REPO/scripts/build_swiss_departure_index.py" \
  --gtfs-url "$SWISS_GTFS_URL" \
  --output "$BUILD_DIR/swiss-static"

python3 "$REPO/scripts/build_german_departure_index.py" \
  --gtfs-url "$GTFS_URL" \
  --output "$BUILD_DIR" \
  --city-id-aliases "$REPO/config/city-id-aliases.json"

test -f "$BUILD_DIR/manifest.json"
test -f "$BUILD_DIR/transit-radar-cities.json"
test -f "$BUILD_DIR/swiss-static/manifest.json"
test -f "$BUILD_DIR/departures-manifest.json"
python3 -m json.tool "$BUILD_DIR/departures-manifest.json" >/dev/null

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
  rm -rf "$PREVIOUS"
  mv "$ROLLBACK" "$PREVIOUS"
fi

mkdir -p "$STAGING"
echo "Published: $CURRENT"
