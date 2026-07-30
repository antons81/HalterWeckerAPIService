import os
import subprocess
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
    if [ \"${BUILD_FAIL:-0}\" = \"1\" ]; then
      exit 1
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
    printf '{\"version\":\"2026-07-30\",\"cities\":[]}' > \"$output/manifest.json\"
    if [ \"${BUILD_INVALID:-0}\" != \"1\" ]; then
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
    printf '2026-07-30\\n'
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
        self.assertIn("published stop release version=2026-07-30", result.stdout)
        self.assertIn("static departures synchronized", result.stdout)
        self.assertEqual(
            self.systemctl_calls(),
            [
                "start --help",
                "start --wait haltewecker-static-departures.service",
                "start-current=new",
                "show haltewecker-static-departures.service -p Result -p ExecMainStatus"
            ]
        )

    def test_failed_static_departures_result_fails_after_publication(self) -> None:
        result = self.run_pipeline(SYSTEMCTL_RESULT="failed", SYSTEMCTL_EXEC_MAIN_STATUS="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.data_root / "current" / "release-marker").read_text(encoding="utf-8"), "new")
        self.assertIn("static departures are not synchronized", result.stderr)
        self.assertIn("Result=failed", result.stdout)
        self.assertIn("ExecMainStatus=1", result.stdout)

    def test_build_failure_does_not_trigger_static_departures(self) -> None:
        result = self.run_pipeline(BUILD_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.data_root / "current" / "release-marker").read_text(encoding="utf-8"), "old")
        self.assertEqual(self.systemctl_calls(), [])

    def test_validation_failure_does_not_trigger_static_departures(self) -> None:
        result = self.run_pipeline(BUILD_INVALID="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.data_root / "current" / "release-marker").read_text(encoding="utf-8"), "old")
        self.assertEqual(self.systemctl_calls(), [])

    def test_fallback_without_wait_verifies_oneshot_result(self) -> None:
        result = self.run_pipeline(SYSTEMCTL_SUPPORTS_WAIT="0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemctl --wait is unavailable", result.stdout)
        self.assertEqual(
            self.systemctl_calls(),
            [
                "start --help",
                "start haltewecker-static-departures.service",
                "start-current=new",
                "show haltewecker-static-departures.service -p Result -p ExecMainStatus"
            ]
        )

    def test_overlapping_stop_data_publication_is_rejected_before_rebuild(self) -> None:
        result = self.run_pipeline(FLOCK_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.data_root / "current" / "release-marker").read_text(encoding="utf-8"), "old")
        self.assertIn("another stop-data publication is already running", result.stderr)
        self.assertEqual(self.systemctl_calls(), [])


if __name__ == "__main__":
    unittest.main()
