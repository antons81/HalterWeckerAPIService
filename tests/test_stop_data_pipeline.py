import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_stop_packages as stop_package_builder
from kyiv_open_data import KyivOpenDataError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PIPELINE = REPOSITORY_ROOT / "scripts" / "run_stop_data_pipeline.sh"


class StopDataPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.data_root = self.root / "data"
        self.data_root.mkdir()
        (self.data_root / "current").mkdir()
        (self.data_root / "current" / "release-marker").write_text("old", encoding="utf-8")
        self.environment_file = self.root / "stop-data.env"
        self.environment_file.write_text("", encoding="utf-8")
        self.bin_directory = self.root / "bin"
        self.bin_directory.mkdir()
        self.systemctl_log = self.root / "systemctl.log"
        self.write_mock("sudo", """#!/usr/bin/env bash
if [ "${1:-}" = "-n" ]; then
  shift
fi
exec \"$@\"
""")
        self.write_mock("flock", """#!/usr/bin/env bash
[ "${FLOCK_FAIL:-0}" != "1" ] || exit 1
exit 0
""")
        self.write_mock("python3", """#!/usr/bin/env bash
set -euo pipefail

case \"${1:-}\" in
  *prepare_gtfs_artifacts.py)
    output=\"\"
    while [ \"$#\" -gt 0 ]; do
      if [ \"$1\" = \"--output\" ]; then
        output=\"$2\"
        break
      fi
      shift
    done
    mkdir -p \"$(dirname \"$output\")\"
    \"$REAL_PYTHON\" - \"$output\" <<'PY'
import hashlib
import json
import sys
from pathlib import Path
output = Path(sys.argv[1])
root = output.parent
def artifact(source_id):
    path = root / f\"{source_id}.zip\"
    path.write_bytes(source_id.encode(\"utf-8\"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {\"path\": str(path), \"sha256\": digest, \"size\": path.stat().st_size}
source_ids = [\"sweden\", \"norway\", \"ireland\", \"translink\", \"ttc-surface\", \"ttc-subway\", \"511-bay-area\", \"cta-chicago\", \"king-county-metro\", \"mta-ny-subway\", \"mta-ny-nyct-bus\", \"mta-ny-mta-bus\", \"mbta-boston\", \"wmata-bus\", \"wmata-rail\", \"kyiv\"]
sources = {\"germany\": artifact(\"germany\"), \"swiss\": artifact(\"swiss\")}
external = {source_id: artifact(source_id) for source_id in source_ids}
output.write_text(json.dumps({\"sources\": sources, \"external\": external, \"nlFailure\": None}), encoding=\"utf-8\")
PY
    exit 0
    ;;
  *build_fingerprint.py)
    printf 'test-build-fingerprint\\n'
    ;;
  *build_stop_packages.py)
    printf 'build\\n' >> \"$BUILD_CALLS_LOG\"
    if [ \"${BUILD_FAIL:-0}\" = \"1\" ]; then
      exit 1
    fi
    if [ -n \"${BUILD_ARGS_LOG:-}\" ]; then
      printf '%s\\n' \"$*\" > \"$BUILD_ARGS_LOG\"
    fi
    output=\"\"
    while [ \"$#\" -gt 0 ]; do
      if [ \"$1\" = \"--output\" ]; then
        output=\"$2\"
        break
      fi
      shift
    done
    mkdir -p \"$output/swiss-static\"
    mkdir -p "$output/provenance"
    printf '{"sources":{}}' > "$output/provenance/input-artifacts.json"
    \"$REAL_PYTHON\" - \"$output\" \"${BUILD_INVALID:-0}\" <<'PY'
import json
import sys
from pathlib import Path
output = Path(sys.argv[1])
invalid = sys.argv[2] == \"1\"
cities = [{\"id\": \"test-city\", \"name\": \"Test City\", \"url\": \"stops/test-city.json\"}]
if not invalid:
    cities.extend({\"id\": city_id, \"name\": city_id, \"url\": f\"stops/{city_id}.json\"} for city_id in (\"san-francisco\", \"oakland\", \"berkeley\", \"san-jose\"))
(output / \"manifest.json\").write_text(json.dumps({\"version\": \"2026-07-30\", \"cities\": cities}), encoding=\"utf-8\")
if not invalid:
    (output / \"transit-radar-cities.json\").touch()
PY
    printf 'new' > \"$output/release-marker\"
    exit 0
    ;;
  *prepare_gtfs_artifacts.py)
    output=\"\"
    while [ \"$#\" -gt 0 ]; do
      if [ \"$1\" = \"--output\" ]; then
        output=\"$2\"
        break
      fi
      shift
    done
    mkdir -p \"$(dirname \"$output\")\"
    printf '{\"sources\":{\"germany\":{\"path\":\"/tmp/germany.zip\",\"sha256\":\"germany\",\"size\":10},\"swiss\":{\"path\":\"/tmp/swiss.zip\",\"sha256\":\"swiss\",\"size\":10}},\"external\":{},\"nlFailure\":null}' > \"$output\"
    ;;
  *prepare_custom_gtfs_artifacts.py)
    output=\"\"
    while [ \"$#\" -gt 0 ]; do
      if [ \"$1\" = \"--output\" ]; then
        output=\"$2\"
        break
      fi
      shift
    done
    mkdir -p \"$(dirname \"$output\")\"
    \"$REAL_PYTHON\" - \"$output\" <<'PY'
import hashlib
import json
import sys
from pathlib import Path
output = Path(sys.argv[1])
root = output.parent
sources = {}
for source_id in (\"vbb\", \"rnv\"):
    path = root / f\"{source_id}.zip\"
    path.write_bytes(source_id.encode(\"utf-8\"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sources[source_id] = {\"sourceID\": source_id, \"path\": str(path), \"sha256\": digest, \"size\": path.stat().st_size}
output.write_text(json.dumps({\"sources\": sources}), encoding=\"utf-8\")
PY
    exit 0
    ;;
  *prepare_custom_gtfs_artifacts.py)
    output=\"\"
    while [ \"$#\" -gt 0 ]; do
      if [ \"$1\" = \"--output\" ]; then
        output=\"$2\"
        break
      fi
      shift
    done
    mkdir -p \"$(dirname \"$output\")\"
    printf '{\"sources\":{\"vbb\":{\"sourceID\":\"vbb\",\"path\":\"/tmp/vbb.zip\",\"sha256\":\"vbb\",\"size\":10},\"rnv\":{\"sourceID\":\"rnv\",\"path\":\"/tmp/rnv.zip\",\"sha256\":\"rnv\",\"size\":10}}}' > \"$output\"
    ;;
  *build_stop_packages.py)
    printf 'build\\n' >> "$BUILD_CALLS_LOG"
    if [ \"${BUILD_FAIL:-0}\" = \"1\" ]; then
      exit 1
    fi
    if [ -n \"${BUILD_ARGS_LOG:-}\" ]; then
      printf '%s\\n' \"$*\" > \"$BUILD_ARGS_LOG\"
    fi
    output=\"\"
    while [ \"$#\" -gt 0 ]; do
      if [ \"$1\" = \"--output\" ]; then
        output=\"$2\"
        break
      fi
      shift
    done
    mkdir -p \"$output/swiss-static\"
    if [ \"${BUILD_INVALID:-0}\" = \"1\" ]; then
      printf '{\"version\":\"2026-07-30\",\"cities\":[{\"id\":\"test-city\",\"name\":\"Test City\",\"url\":\"stops/test-city.json\"}]}' > \"$output/manifest.json\"
    else
      printf '{\"version\":\"2026-07-30\",\"cities\":[{\"id\":\"test-city\",\"name\":\"Test City\",\"url\":\"stops/test-city.json\"}]}' > \"$output/manifest.json\"
      : > \"$output/transit-radar-cities.json\"
    fi
    printf 'new' > \"$output/release-marker\"
    ;;
  *build_swiss_departure_index.py)
    output=\"\"
    while [ \"$#\" -gt 0 ]; do
      if [ \"$1\" = \"--output\" ]; then
        output=\"$2\"
        break
      fi
      shift
    done
    mkdir -p \"$output\"
    : > \"$output/manifest.json\"
    ;;
  *validate_release_consistency.py)
    exit 0
    ;;
  -)
    exec "$REAL_PYTHON" "$@"
    ;;
  *)
    echo \"unexpected python3 invocation: $*\" >&2
    exit 64
    ;;
esac
""")
        self.write_mock("systemctl", """#!/usr/bin/env bash
set -euo pipefail

printf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"

if [ \"${1:-}\" = \"start\" ] && [ \"${2:-}\" = \"--help\" ]; then
  if [ \"${SYSTEMCTL_SUPPORTS_WAIT:-1}\" = \"1\" ]; then
    printf '%s\\n' '  --wait'
  fi
  exit 0
fi

if [ \"${1:-}\" = \"start\" ]; then
  current_marker=\"$(cat \"$DATA_ROOT/current/release-marker\" 2>/dev/null || true)\"
  printf 'start-current=%s\\n' \"$current_marker\" >> \"$SYSTEMCTL_LOG\"
  [ \"$current_marker\" = \"new\" ] || exit 70
  [ \"${SYSTEMCTL_START_FAIL:-0}\" != \"1\" ] || exit 1
  exit 0
fi

if [ \"${1:-}\" = \"show\" ]; then
  printf 'Result=%s\\n' \"${SYSTEMCTL_RESULT:-success}\"
  printf 'ExecMainStatus=%s\\n' \"${SYSTEMCTL_EXEC_MAIN_STATUS:-0}\"
  exit 0
fi

echo \"unexpected systemctl invocation: $*\" >&2
exit 64
""")
        self.write_mock("static-departures-pipeline", """#!/usr/bin/env bash
set -euo pipefail

if [ "${READINESS_ONLY:-0}" = "1" ]; then
  [ "${READINESS_FAIL:-0}" != "1" ] || exit 1
  exit 0
fi
[ "${STATIC_IMPORT_FAIL:-0}" != "1" ] || exit 1
printf '%s\\n' "${STOP_DATA_PATH}" > "${STAGED_STOP_DATA_LOG}"
mkdir -p "$(dirname "$NEXT_DATABASE_PATH")"
printf 'releaseID=%s\\n' "$RELEASE_ID" > "$NEXT_DATABASE_PATH"
""")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_mock(self, name: str, content: str) -> None:
        path = self.bin_directory / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def run_pipeline(self, **extra_environment: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update({
            "REPO": str(REPOSITORY_ROOT),
            "DATA_ROOT": str(self.data_root),
            "STOP_DATA_LOCK": str(self.root / "stop-data.lock"),
            "STATIC_DEPARTURES_LOCK": str(self.root / "static-departures.lock"),
            "STOP_DATA_ENV_FILE": str(self.environment_file),
            "SYSTEMCTL_LOG": str(self.systemctl_log),
            "BUILD_ARGS_LOG": str(self.root / "build-args.log"),
            "BUILD_CALLS_LOG": str(self.root / "build-calls.log"),
            "STAGED_STOP_DATA_LOG": str(self.root / "staged-stop-data.log"),
            "STATIC_DEPARTURES_PIPELINE": str(self.bin_directory / "static-departures-pipeline"),
            "REAL_PYTHON": sys.executable,
            "GTFS_URL": "https://example.invalid/german.zip",
            "SWISS_GTFS_URL": "https://example.invalid/swiss.zip",
            "NL_GTFS_URL": "https://example.invalid/netherlands.zip",
            "PATH": f"{self.bin_directory}:{environment['PATH']}"
        })
        environment.update(extra_environment)
        return subprocess.run(
            ["bash", str(PIPELINE)],
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False
        )

    def systemctl_calls(self) -> list[str]:
        if not self.systemctl_log.exists():
            return []
        return self.systemctl_log.read_text(encoding="utf-8").splitlines()

    def test_successful_publication_waits_for_static_departures_service(self) -> None:
        result = self.run_pipeline()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.data_root / "current" / "release-marker").read_text(encoding="utf-8"), "new")
        self.assertEqual((self.data_root / "previous" / "stop-data" / "release-marker").read_text(encoding="utf-8"), "old")
        self.assertRegex(result.stdout, r"release=.* stage=commit duration=")
        self.assertIn("static departures synchronized", result.stdout)
        self.assertEqual(self.systemctl_calls(), [])
        staged_path = (self.root / "staged-stop-data.log").read_text().strip()
        self.assertIn("/releases/", staged_path)
        self.assertTrue(staged_path.endswith("/stop-data"))
        self.assertIn("releaseID=", (self.data_root / "departures-current.sqlite").read_text())
        self.assertEqual(
            (self.data_root / "current" / "release-marker").read_text(),
            "new",
        )
        self.assertNotEqual(
            (self.data_root / "previous" / "stop-data" / "release-marker").read_text(),
            "new",
        )

    def test_external_sources_do_not_require_a_norway_cli_override(self) -> None:
        result = self.run_pipeline()

        self.assertEqual(result.returncode, 0, result.stderr)
        build_args = (self.root / "build-args.log").read_text(encoding="utf-8")
        self.assertIn(
            f"--external-gtfs-sources {REPOSITORY_ROOT / 'config' / 'external-gtfs-sources.json'}",
            build_args,
        )
        self.assertIn(f"--kyiv-cache-root {self.data_root / 'kyiv-open-data-cache'}", build_args)
        self.assertIn("--gtfs-cache-root /srv/haltewecker/cache/gtfs", build_args)
        self.assertIn(f"--previous-stop-data {self.data_root / 'current'}", build_args)
        self.assertIn("511-bay-area=", build_args)
        self.assertEqual(
            (self.data_root / "current" / "release-marker").read_text(encoding="utf-8"),
            "new",
        )

    def test_failed_static_departures_result_fails_after_publication(self) -> None:
        result = self.run_pipeline(READINESS_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.data_root / "current" / "release-marker").read_text(encoding="utf-8"), "old")
        self.assertIn("runtime readiness failed", result.stderr)

    def test_build_failure_does_not_trigger_static_departures(self) -> None:
        result = self.run_pipeline(BUILD_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.data_root / "current" / "release-marker").read_text(encoding="utf-8"), "old")
        self.assertEqual(self.systemctl_calls(), [])
        self.assertEqual((self.root / "build-calls.log").read_text().splitlines(), ["build"])
        self.assertNotIn("Dutch", result.stdout)

    def test_kyiv_without_any_fallback_fails_candidate_after_other_stages(self) -> None:
        output = self.root / "candidate"
        kyiv_city = json.loads(
            (REPOSITORY_ROOT / "config" / "kyiv-cities.json").read_text(encoding="utf-8")
        )[0]
        manifest_entry = {
            "id": "kyiv",
            "name": "Kyiv",
            "aliases": [],
            "stopCount": 1,
            "url": "stops/kyiv.json",
            "country": "UA",
            "_source": "test external source",
        }

        def fake_external_sources(**_kwargs):
            return [manifest_entry], [kyiv_city], {"kyiv": []}, {}

        with mock.patch(
            "external_gtfs.process_external_gtfs_sources",
            side_effect=fake_external_sources,
        ), mock.patch(
            "external_gtfs.validate_external_stop_packages",
        ), mock.patch(
            "kyiv_open_data.build_kyiv_systems_artifact",
            side_effect=KyivOpenDataError("simulated Kyiv outage"),
        ):
            with self.assertRaises(KyivOpenDataError):
                stop_package_builder.main([
                    "--skip-german",
                    "--external-gtfs-url",
                    "kyiv=https://data.kyivcity.gov.ua/gtfs.zip",
                    "--external-gtfs-sources",
                    str(REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"),
                    "--output",
                    str(output),
                    "--kyiv-cache-root",
                    str(self.root / "missing-kyiv-cache"),
                    "--gtfs-cache-root",
                    str(self.root / "missing-gtfs-cache"),
                    "--previous-stop-data",
                    str(self.data_root / "current"),
                ])

        self.assertEqual(
            (self.data_root / "current" / "release-marker").read_text(encoding="utf-8"),
            "old",
        )
        self.assertIn("kyiv", json.loads((output / "manifest.json").read_text())["cities"][0]["id"])
        self.assertTrue((output / "transit" / "city-lines" / "kyiv.json").is_file())

    def test_validation_failure_does_not_trigger_static_departures(self) -> None:
        result = self.run_pipeline(BUILD_INVALID="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.data_root / "current" / "release-marker").read_text(encoding="utf-8"), "old")
        self.assertEqual(self.systemctl_calls(), [])

    def test_readiness_success_commits_without_systemd_rebuild(self) -> None:
        result = self.run_pipeline(SYSTEMCTL_SUPPORTS_WAIT="0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.systemctl_calls(), [])

    def test_static_import_failure_keeps_previous_pair(self) -> None:
        result = self.run_pipeline(STATIC_IMPORT_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.data_root / "current" / "release-marker").read_text(), "old")
        self.assertFalse((self.data_root / "current-release").exists())

    def test_readiness_failure_keeps_previous_pair(self) -> None:
        result = self.run_pipeline(READINESS_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.data_root / "current" / "release-marker").read_text(), "old")
        self.assertFalse((self.data_root / "current-release").exists())

    def test_overlapping_stop_data_publication_is_rejected_before_rebuild(self) -> None:
        result = self.run_pipeline(FLOCK_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.data_root / "current" / "release-marker").read_text(encoding="utf-8"), "old")
        self.assertIn("another stop-data publication is already running", result.stderr)
        self.assertEqual(self.systemctl_calls(), [])


if __name__ == "__main__":
    unittest.main()
