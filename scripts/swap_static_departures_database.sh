#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/swap_static_departures_database.py" --data-root "${DATA_ROOT:-/srv/haltewecker/data}"
