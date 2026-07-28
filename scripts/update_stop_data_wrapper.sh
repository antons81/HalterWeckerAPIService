#!/usr/bin/env bash
set -euo pipefail

REPO="/srv/haltewecker/pipeline/HalterWeckerAPIService"

git -C "$REPO" fetch origin main
git -C "$REPO" merge --ff-only origin/main
exec "$REPO/scripts/run_stop_data_pipeline.sh"
