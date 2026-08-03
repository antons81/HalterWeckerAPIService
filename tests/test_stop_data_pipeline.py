import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
        self.assertNotIn("norway=", build_args)
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
