import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_fingerprint
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
        self.write_mock("df", """#!/usr/bin/env bash
if [ -n "${DF_FREE_KB:-}" ]; then
  printf '%s\n' 'Filesystem 1024-blocks Used Available Capacity Mounted on'
  printf 'mock 1 %s %s 0%% %s\n' "$((DF_FREE_KB + 1))" "$DF_FREE_KB" "$PWD"
else
  exec /bin/df "$@"
fi
""")
        self.write_mock("ln", """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$LINK_CALLS_LOG"
exec /bin/ln "$@"
""")
        self.write_mock("python3", """#!/usr/bin/env bash
set -euo pipefail

case \"${1:-}\" in
  *release_state.py)
    stage=\"\"
    previous=\"\"
    for argument in \"$@\"; do
      if [ \"$previous\" = \"--completed-stage\" ]; then
        stage=\"$argument\"
        break
      fi
      previous=\"$argument\"
    done
    if [ \"${2:-}\" = \"write-state\" ]; then
      printf '%s\\n' \"$*\" >> \"$STATE_WRITE_CALLS_LOG\"
    fi
    if [ \"${CRASH_BEFORE_COMMIT:-0}\" = \"1\" ] && [ \"$stage\" = \"commit\" ]; then
      exit 99
    fi
    \"$REAL_PYTHON\" \"$@\"
    if [ \"${CRASH_AFTER_STATE:-}\" = \"1\" ] && [ \"$stage\" = \"${CRASH_AFTER_STAGE:-}\" ]; then
      exit 99
    fi
    exit 0
    ;;
  *run_incremental_provider_pipeline.py)
    printf '%s\n' "$*" >> "$INCREMENTAL_CALLS_LOG"
    release_root=""
    release_id=""
    stop_data=""
    result_json=""
    previous=""
    for argument in "$@"; do
      if [ "$previous" = "--releases-root" ]; then
        release_root="$argument"
      elif [ "$previous" = "--release-id" ]; then
        release_id="$argument"
      elif [ "$previous" = "--stop-data" ]; then
        stop_data="$argument"
      elif [ "$previous" = "--result-json" ]; then
        result_json="$argument"
      fi
      previous="$argument"
    done
    if [ -n "$result_json" ] || [ "${REUSE_STOP_DATA:-0}" = "1" ] || [ "${INCREMENTAL_FAIL:-0}" = "1" ] || [ "${INCREMENTAL_PUBLISHED:-0}" = "1" ]; then
      mkdir -p "$release_root/$release_id"
    fi
    if [ -n "$result_json" ]; then
      printf '{"releaseID":"%s","releaseDirectory":"%s","stopData":{"releaseID":"%s","path":"%s","buildFingerprint":"test-build-fingerprint"},"providers":{}}\n' \
        "$release_id" "$release_root/$release_id" "$release_id" "$stop_data" > "$result_json"
      if [ "${INCREMENTAL_FAIL:-0}" != "1" ] && [ "${INCREMENTAL_READINESS_FAIL:-0}" != "1" ]; then
        printf '{"releaseID":"%s"}\n' "$release_id" > "$release_root/$release_id/release.json"
      fi
    fi
    if [ "${REUSE_STOP_DATA:-0}" = "1" ]; then
      ln -s "$stop_data" "$release_root/$release_id/stop-data"
    fi
    if [ "${INCREMENTAL_FAIL:-0}" = "1" ] || [ "${INCREMENTAL_READINESS_FAIL:-0}" = "1" ]; then
      if [ "${INCREMENTAL_PUBLISHED:-0}" = "1" ]; then
        printf '{"releaseID":"%s"}\n' "$release_id" > "$release_root/$release_id/release.json"
      fi
      exit 1
    fi
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
    \"$REAL_PYTHON\" - \"$output\" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path
output = Path(sys.argv[1])
root = output.parent
def artifact(source_id):
    if source_id == "ireland":
        sys.path.insert(0, os.environ["REPO"] + "/scripts")
        from artifact_provenance import artifact_provenance
        path = root / "external-artifacts" / "ireland"
        path.mkdir(parents=True, exist_ok=True)
        (path / "stops.txt").write_text("stop_id,stop_name\\nA,Alpha\\n", encoding="utf-8")
        digest, size = artifact_provenance(path)
        return {"path": str(path), "sha256": digest, "size": size}
    path = root / f\"{source_id}.zip\"
    path.write_bytes(source_id.encode(\"utf-8\"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {\"path\": str(path), \"sha256\": digest, \"size\": path.stat().st_size}
registry = json.loads(
    (Path(os.environ[\"REPO\"]) / \"config\" / \"external-gtfs-sources.json\").read_text(
        encoding=\"utf-8\"
    )
)
source_ids = [
    str(source[\"id\"])
    for source in registry
    if str(source.get(\"classification\", \"required\")) == \"required\"
]
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
    mkdir -p \"$output/stops\" \"$output/routes\" \"$output/departures\" \"$output/trips\" \"$output/transit\" \"$output/radar\"
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
    cities.extend({\"id\": city_id, \"name\": city_id, \"url\": f\"stops/{city_id}.json\"} for city_id in (\"dublin\", \"cork\", \"galway\", \"limerick\", \"waterford\"))
(output / \"manifest.json\").write_text(json.dumps({\"version\": \"2026-07-30\", \"cities\": cities}), encoding=\"utf-8\")
for city in cities:
    package_path = output / city[\"url\"]
    package_path.parent.mkdir(parents=True, exist_ok=True)
    package_path.write_text(\"{}\", encoding=\"utf-8\")
for city_id in (\"dublin\", \"cork\", \"galway\", \"limerick\", \"waterford\"):
    for directory in (\"routes\", \"departures\"):
        package_path = output / directory / f\"{city_id}.json\"
        package_path.write_text(\"{}\", encoding=\"utf-8\")
if not invalid:
    (output / \"transit-radar-cities.json\").write_text(
        json.dumps({\"cities\": [{\"appCityID\": city_id} for city_id in (\"dublin\", \"cork\", \"galway\", \"limerick\", \"waterford\")]}),
        encoding=\"utf-8\",
    )
(output / \"swiss-static\" / \"manifest.json\").write_text(\"{}\", encoding=\"utf-8\")
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
    printf '{}' > \"$output/manifest.json\"
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

printf '%s\\n' "${READINESS_ONLY:-0}" >> "$STATIC_CALLS_LOG"
if [ "${READINESS_ONLY:-0}" = "1" ]; then
  [ "${READINESS_FAIL:-0}" != "1" ] || exit 1
  exit 0
fi
[ "${STATIC_IMPORT_FAIL:-0}" != "1" ] || exit 1
printf '%s\\n' "${STOP_DATA_PATH}" > "${STAGED_STOP_DATA_LOG}"
mkdir -p "$(dirname "$NEXT_DATABASE_PATH")"
"$REAL_PYTHON" - "$NEXT_DATABASE_PATH" "$RELEASE_ID" <<'PY'
import sqlite3
import sys
from pathlib import Path

database_path = Path(sys.argv[1])
release_id = sys.argv[2]
connection = sqlite3.connect(database_path)
connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
connection.executemany(
    "INSERT INTO metadata VALUES (?, ?)",
    [
        ("releaseID", release_id),
        ("stopDataReleaseID", release_id),
        ("stopDataManifestVersion", "2026-07-30"),
    ],
)
connection.commit()
connection.close()
PY
""")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_stop_data_fingerprint_is_deterministic_and_exposes_components(self) -> None:
        first = build_fingerprint.compute(REPOSITORY_ROOT)
        second = build_fingerprint.compute(REPOSITORY_ROOT)
        manifest = build_fingerprint.component_manifest(REPOSITORY_ROOT)

        self.assertEqual(first, second)
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["orchestrationVersion"], 1)
        self.assertTrue(manifest["components"])
        self.assertTrue(
            all("path" in item and "sha256" in item for item in manifest["components"])
        )

    def test_unrelated_validator_change_does_not_change_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "builder.py").write_text("builder-v1", encoding="utf-8")
            validator = root / "run_incremental_provider_pipeline.py"
            validator.write_text("validator-v1", encoding="utf-8")

            first = build_fingerprint.compute(root, ("builder.py",))
            validator.write_text("validator-v2", encoding="utf-8")
            second = build_fingerprint.compute(root, ("builder.py",))

            self.assertEqual(first, second)
            self.assertNotIn(
                "scripts/run_incremental_provider_pipeline.py",
                build_fingerprint.STOP_DATA_INPUTS,
            )

    def test_git_head_change_does_not_change_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            (root / "builder.py").write_text("builder-v1", encoding="utf-8")
            unrelated = root / "validator.py"
            unrelated.write_text("validator-v1", encoding="utf-8")
            subprocess.run(["git", "add", "builder.py", "validator.py"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "first",
                ],
                cwd=root,
                check=True,
            )
            first_revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            first = build_fingerprint.compute(root, ("builder.py",))

            unrelated.write_text("validator-v2", encoding="utf-8")
            subprocess.run(["git", "add", "validator.py"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "unrelated",
                ],
                cwd=root,
                check=True,
            )
            second_revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            second = build_fingerprint.compute(root, ("builder.py",))

            self.assertNotEqual(first_revision, second_revision)
            self.assertEqual(first, second)

    def test_relevant_builder_and_config_changes_change_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = root / "builder.py"
            config = root / "config.json"
            builder.write_text("builder-v1", encoding="utf-8")
            config.write_text("config-v1", encoding="utf-8")
            inputs = ("builder.py", "config.json")

            baseline = build_fingerprint.compute(root, inputs)
            builder.write_text("builder-v2", encoding="utf-8")
            builder_changed = build_fingerprint.compute(root, inputs)
            config.write_text("config-v2", encoding="utf-8")
            config_changed = build_fingerprint.compute(root, inputs)

            self.assertNotEqual(baseline, builder_changed)
            self.assertNotEqual(builder_changed, config_changed)

    def test_fingerprint_algorithm_version_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "builder.py").write_text("builder", encoding="utf-8")

            version_one = build_fingerprint.compute(root, ("builder.py",), version=1)
            version_two = build_fingerprint.compute(root, ("builder.py",), version=2)

            self.assertNotEqual(version_one, version_two)

    def write_mock(self, name: str, content: str) -> None:
        path = self.bin_directory / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def run_pipeline(self, *arguments: str, **extra_environment: str) -> subprocess.CompletedProcess[str]:
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
            "LINK_CALLS_LOG": str(self.root / "link-calls.log"),
            "STATE_WRITE_CALLS_LOG": str(self.root / "state-write-calls.log"),
            "INCREMENTAL_CALLS_LOG": str(self.root / "incremental-calls.log"),
            "STATIC_CALLS_LOG": str(self.root / "static-calls.log"),
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
            ["bash", str(PIPELINE), *arguments],
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

    def configure_resume_pointer_layout(self) -> None:
        old_release = self.data_root / "releases" / "old"
        old_stop_data = old_release / "stop-data"
        old_stop_data.mkdir(parents=True)
        (old_stop_data / "release-marker").write_text("old", encoding="utf-8")
        (old_release / "departures.sqlite").write_bytes(b"old")
        current = self.data_root / "current"
        (current / "release-marker").unlink()
        current.rmdir()
        self._link(self.data_root / "current-release", old_release)
        self._link(current, old_stop_data)
        self._link(self.data_root / "departures-current.sqlite", old_release / "departures.sqlite")

    def _link(self, path: Path, target: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(os.path.relpath(target, path.parent))

    def candidate_release_id(self) -> str:
        releases = [
            path
            for path in (self.data_root / "releases").iterdir()
            if path.is_dir() and path.name != "old"
        ]
        self.assertEqual(len(releases), 1)
        return releases[0].name

    def prepare_reusable_current_release(self) -> tuple[Path, str]:
        initial = self.run_pipeline()
        self.assertEqual(initial.returncode, 0, initial.stderr)
        current_target = Path(os.path.realpath(self.data_root / "current"))
        release_id = current_target.parent.name
        self.assertEqual(
            json.loads((current_target / "manifest.json").read_text(encoding="utf-8"))["releaseID"],
            release_id,
        )
        return current_target, release_id

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
        with sqlite3.connect(self.data_root / "departures-current.sqlite") as database:
            metadata = dict(database.execute("SELECT key, value FROM metadata"))
        published_release = next(
            path
            for path in (self.data_root / "releases").iterdir()
            if not path.name.startswith("legacy-")
        )
        self.assertEqual(metadata["releaseID"], published_release.name)
        state = json.loads((published_release / "release-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["completedStage"], "commit")
        self.assertEqual(
            (self.data_root / "current" / "release-marker").read_text(),
            "new",
        )
        self.assertNotEqual(
            (self.data_root / "previous" / "stop-data" / "release-marker").read_text(),
            "new",
        )

    def test_stop_data_only_persists_validated_source_without_downstream_stages(self) -> None:
        result = self.run_pipeline("--stop-data-only")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stage=stop-data-only status=PASS", result.stdout)
        self.assertIn("stage=legacy-import status=SKIPPED", result.stdout)
        self.assertNotIn("stage=incremental-provider", result.stdout)
        self.assertEqual(
            (self.root / "build-calls.log").read_text(encoding="utf-8").splitlines(),
            ["build"],
        )
        self.assertFalse((self.root / "static-calls.log").exists())
        self.assertFalse((self.root / "incremental-calls.log").exists())
        self.assertFalse((self.data_root / "current-release").exists())
        self.assertFalse((self.data_root / "departures-current.sqlite").exists())

        release_id = self.candidate_release_id()
        release_dir = self.data_root / "releases" / release_id
        manifest = json.loads((release_dir / "stop-data" / "manifest.json").read_text(encoding="utf-8"))
        metadata = json.loads((release_dir / "release-metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["releaseID"], release_id)
        self.assertEqual(metadata["releaseID"], release_id)
        self.assertEqual(metadata["buildFingerprint"], "test-build-fingerprint")
        self.assertEqual(
            json.loads((release_dir / "release-state.json").read_text(encoding="utf-8"))["completedStage"],
            "candidate-validation",
        )
        reports = sorted((self.data_root / "pipeline-diagnostics").glob("*.report"))
        self.assertEqual(len(reports), 1)
        report_path = reports[0]
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("status=PASS", report)
        self.assertIn("run_id=stop-data-only-", report)
        self.assertIn("fingerprint_version=2", report)
        self.assertIn("build_fingerprint=test-build-fingerprint", report)
        self.assertIn(f"release_id={release_id}", report)
        self.assertTrue(report_path.with_suffix(".log").is_file())
        self.assertTrue(report_path.with_suffix(".stderr.log").is_file())

    def test_production_shaped_incremental_mode_is_fresh_and_non_activating(self) -> None:
        result = self.run_pipeline(
            "--incremental-no-activate",
            HALTEWECKER_MIN_FREE_GB="35",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mode=production-shaped-no-activate", result.stdout)
        self.assertIn("minimum_free_gb=45", result.stdout)
        self.assertIn("stage=legacy-import status=SKIPPED", result.stdout)
        self.assertIn("reason=production-shaped-no-activate", result.stdout)
        self.assertIn("stage=incremental-metadata status=PASS", result.stdout)
        self.assertIn("activation=NOT_RUN", result.stdout)
        self.assertEqual(
            (self.data_root / "current" / "release-marker").read_text(encoding="utf-8"),
            "old",
        )
        self.assertFalse((self.data_root / "current-release").exists())
        self.assertFalse((self.data_root / "departures-current.sqlite").exists())
        self.assertFalse((self.root / "static-calls.log").exists())

        calls = (self.root / "incremental-calls.log").read_text(encoding="utf-8")
        self.assertIn("--result-json", calls)
        release_id = next(
            path.name
            for path in (self.data_root / "releases").iterdir()
            if (path / "stop-data" / "manifest.json").is_file()
        )
        release_dir = self.data_root / "releases" / release_id
        result_metadata = json.loads(
            (release_dir / "incremental-result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result_metadata["releaseID"], release_id)
        self.assertEqual(result_metadata["stopData"]["releaseID"], release_id)
        self.assertEqual(
            result_metadata["stopData"]["buildFingerprint"],
            "test-build-fingerprint",
        )
        candidate = Path(result_metadata["releaseDirectory"])
        self.assertTrue((candidate / "release.json").is_file())

        reports = sorted((self.data_root / "pipeline-diagnostics").glob("*.report"))
        self.assertEqual(len(reports), 1)
        report = reports[0].read_text(encoding="utf-8")
        self.assertIn("run_kind=incremental-no-activate", report)
        self.assertIn(f"release_id={release_id}", report)

    def test_manual_incremental_proof_override_is_explicit_and_scoped(self) -> None:
        result = self.run_pipeline(
            "--incremental-no-activate",
            HALTEWECKER_INCREMENTAL_PROOF_OVERRIDE="1",
            HALTEWECKER_MIN_FREE_GB="45",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mode=manual-production-shaped-proof", result.stdout)
        self.assertIn("warning_free_gb=40", result.stdout)
        self.assertIn("minimum_free_gb=35", result.stdout)
        self.assertIn("stage=legacy-import status=SKIPPED", result.stdout)

    def test_production_shaped_incremental_failure_cleans_candidate_and_keeps_pointers(self) -> None:
        result = self.run_pipeline(
            "--incremental-no-activate",
            INCREMENTAL_FAIL="1",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            (self.data_root / "current" / "release-marker").read_text(encoding="utf-8"),
            "old",
        )
        self.assertFalse((self.data_root / "current-release").exists())
        self.assertFalse((self.data_root / "departures-current.sqlite").exists())
        self.assertFalse(
            list((self.data_root / "releases" / "incremental").glob("**/release.json"))
        )
        reports = sorted((self.data_root / "pipeline-diagnostics").glob("*.report"))
        self.assertEqual(len(reports), 1)
        report = reports[0].read_text(encoding="utf-8")
        self.assertIn("status=FAIL", report)
        self.assertIn("run_kind=incremental-no-activate", report)

    def test_production_shaped_readiness_failure_keeps_pointers_unchanged(self) -> None:
        result = self.run_pipeline(
            "--incremental-no-activate",
            INCREMENTAL_READINESS_FAIL="1",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            (self.data_root / "current" / "release-marker").read_text(encoding="utf-8"),
            "old",
        )
        self.assertFalse((self.data_root / "current-release").exists())
        self.assertFalse((self.data_root / "departures-current.sqlite").exists())
        report = next((self.data_root / "pipeline-diagnostics").glob("*.report"))
        self.assertIn("status=FAIL", report.read_text(encoding="utf-8"))

    def test_production_shaped_mode_uses_production_disk_floor(self) -> None:
        result = self.run_pipeline(
            "--incremental-no-activate",
            HALTEWECKER_MIN_FREE_GB="35",
            DF_FREE_KB=str(44 * 1024 * 1024),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("minimum_free_gb=45", result.stdout)
        self.assertIn("insufficient disk for production-shaped-no-activate", result.stderr)
        self.assertFalse((self.root / "build-calls.log").exists())
        self.assertFalse((self.root / "incremental-calls.log").exists())

    def test_explicit_stop_data_generation_can_be_reused_without_current_pointer(self) -> None:
        fresh = self.run_pipeline("--stop-data-only")
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        release_id = self.candidate_release_id()
        source = self.data_root / "releases" / release_id / "stop-data"
        source_manifest = (source / "manifest.json").read_bytes()
        source_metadata = (source.parent / "release-metadata.json").read_bytes()

        reused = self.run_pipeline(
            "--no-activate",
            "--reuse-stop-data",
            release_id,
            REUSE_STOP_DATA="1",
        )

        self.assertEqual(reused.returncode, 0, reused.stderr)
        self.assertIn(f"stage=stop-data-reuse status=PASS release={release_id}", reused.stdout)
        self.assertIn("stage=stop-data-build status=SKIPPED", reused.stdout)
        self.assertIn("stage=incremental-provider status=PASS", reused.stdout)
        self.assertEqual(
            (self.root / "build-calls.log").read_text(encoding="utf-8").splitlines(),
            ["build"],
        )
        self.assertEqual((source / "manifest.json").read_bytes(), source_manifest)
        self.assertEqual((source.parent / "release-metadata.json").read_bytes(), source_metadata)
        self.assertFalse((self.data_root / "current").is_symlink())
        reports = sorted((self.data_root / "pipeline-diagnostics").glob("*.report"))
        self.assertEqual(len(reports), 2)
        self.assertNotEqual(reports[0].stem, reports[1].stem)
        self.assertTrue(all("run_id=" in report.read_text(encoding="utf-8") for report in reports))

    def test_failure_preserves_already_published_incremental_candidate(self) -> None:
        result = self.run_pipeline(
            "--no-activate",
            INCREMENTAL_FAIL="1",
            INCREMENTAL_PUBLISHED="1",
        )

        self.assertNotEqual(result.returncode, 0)
        incremental_root = self.data_root / "releases" / "incremental"
        published = list(incremental_root.glob("*/release.json"))
        self.assertEqual(len(published), 1)
        reports = sorted((self.data_root / "pipeline-diagnostics").glob("*.report"))
        self.assertEqual(len(reports), 1)
        self.assertIn(
            "preserved published_incremental_release=",
            reports[0].read_text(encoding="utf-8"),
        )

    def test_stop_data_only_failure_cleans_new_generation_only(self) -> None:
        result = self.run_pipeline("--stop-data-only", BUILD_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stage=cleanup status=PASS", result.stderr)
        self.assertEqual(
            (self.data_root / "current" / "release-marker").read_text(encoding="utf-8"),
            "old",
        )
        self.assertEqual(list((self.data_root / "releases").iterdir()), [])
        reports = sorted((self.data_root / "pipeline-diagnostics").glob("*.report"))
        self.assertEqual(len(reports), 1)
        report = reports[0].read_text(encoding="utf-8")
        self.assertIn("status=FAIL", report)
        self.assertIn("exit_code=1", report)
        self.assertIn("fingerprint_version=2", report)
        self.assertIn("current_stage=stop-data-build", report)
        self.assertIn("cleanup_actions=removed release_dir=", report)
        stderr_log = next((self.data_root / "pipeline-diagnostics").glob("*.stderr.log"))
        self.assertIn("stage=cleanup status=PASS", stderr_log.read_text(encoding="utf-8"))

    def test_no_activate_proof_skips_legacy_database_and_runs_incremental_first(self) -> None:
        result = self.run_pipeline("--no-activate", STATIC_IMPORT_FAIL="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stage=legacy-import status=SKIPPED", result.stdout)
        self.assertIn("stage=incremental-provider status=PASS", result.stdout)
        self.assertIn("disk phase=before", result.stdout)
        self.assertIn("disk phase=after", result.stdout)
        self.assertIn("disk phase=peak", result.stdout)
        self.assertFalse((self.root / "static-calls.log").exists())
        self.assertTrue((self.root / "incremental-calls.log").is_file())
        release_id = self.candidate_release_id()
        release_dir = self.data_root / "releases" / release_id
        self.assertFalse((release_dir / "departures.sqlite").exists())
        self.assertEqual(
            (self.data_root / "current" / "release-marker").read_text(),
            "old",
        )
        self.assertFalse((self.data_root / "current-release").exists())

    def test_no_activate_failure_cleans_source_and_incremental_generations(self) -> None:
        result = self.run_pipeline("--no-activate", INCREMENTAL_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stage=legacy-import status=SKIPPED", result.stdout)
        releases_root = self.data_root / "releases"
        incremental_root = self.data_root / "releases" / "incremental"
        self.assertFalse([path for path in releases_root.iterdir() if path != incremental_root])
        self.assertTrue(incremental_root.is_dir())
        self.assertFalse(list(incremental_root.glob("*")))
        self.assertEqual(
            (self.data_root / "current" / "release-marker").read_text(),
            "old",
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

    def test_reuse_stop_data_skips_build_and_legacy_import_without_mutating_source(self) -> None:
        source, release_id = self.prepare_reusable_current_release()
        source_manifest = (source / "manifest.json").read_bytes()
        source_metadata = (source.parent / "release-metadata.json").read_bytes()
        static_calls_before = (self.root / "static-calls.log").read_text(encoding="utf-8").splitlines()

        result = self.run_pipeline(
            "--no-activate",
            "--reuse-stop-data",
            STATIC_IMPORT_FAIL="1",
            REUSE_STOP_DATA="1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stage=stop-data-build status=SKIPPED", result.stdout)
        self.assertIn("stage=validation status=PASS", result.stdout)
        self.assertIn("stage=legacy-import status=SKIPPED", result.stdout)
        self.assertIn("stage=stop-data-reuse status=UNCHANGED", result.stdout)
        self.assertIn("reuse_stop_data=1", result.stdout)
        self.assertEqual(
            (self.root / "build-calls.log").read_text(encoding="utf-8").splitlines(),
            ["build"],
        )
        self.assertEqual(
            (self.root / "static-calls.log").read_text(encoding="utf-8").splitlines(),
            static_calls_before,
        )
        self.assertEqual((source / "manifest.json").read_bytes(), source_manifest)
        self.assertEqual((source.parent / "release-metadata.json").read_bytes(), source_metadata)
        self.assertEqual(os.path.realpath(self.data_root / "current"), str(source))

        reuse_candidates = list((self.data_root / "releases" / "incremental").glob("reuse-*"))
        self.assertEqual(len(reuse_candidates), 1)
        stop_data_reference = reuse_candidates[0] / release_id / "stop-data"
        self.assertTrue(stop_data_reference.is_symlink())
        self.assertEqual(os.path.realpath(stop_data_reference), str(source))

    def test_reuse_stop_data_requires_explicit_published_current_symlink(self) -> None:
        current = self.data_root / "current"
        current_marker = current / "release-marker"
        current_marker.unlink()
        current.rmdir()
        current.symlink_to(self.data_root / "releases" / "missing" / "stop-data")

        result = self.run_pipeline("--no-activate", "--reuse-stop-data", REUSE_STOP_DATA="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reuse source is missing or broken", result.stderr)

    def test_reuse_stop_data_rejects_incompatible_source(self) -> None:
        source, _ = self.prepare_reusable_current_release()
        metadata_path = source.parent / "release-metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["buildFingerprint"] = "stale-build"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        result = self.run_pipeline("--no-activate", "--reuse-stop-data")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build fingerprint is incompatible", result.stderr)
        self.assertEqual(
            (self.root / "build-calls.log").read_text(encoding="utf-8").splitlines(),
            ["build"],
        )

    def test_reuse_failure_does_not_delete_reused_source(self) -> None:
        source, _ = self.prepare_reusable_current_release()
        source_manifest = (source / "manifest.json").read_bytes()
        result = self.run_pipeline(
            "--no-activate",
            "--reuse-stop-data",
            INCREMENTAL_FAIL="1",
            REUSE_STOP_DATA="1",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stage=cleanup status=PASS", result.stderr)
        self.assertTrue(source.is_dir())
        self.assertEqual((source / "manifest.json").read_bytes(), source_manifest)
        self.assertEqual(os.path.realpath(self.data_root / "current"), str(source))
        reuse_candidates = list((self.data_root / "releases" / "incremental").glob("reuse-*"))
        self.assertEqual(len(reuse_candidates), 1)
        self.assertFalse(list(reuse_candidates[0].iterdir()))

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

    def test_resume_requires_an_existing_explicit_release(self) -> None:
        result = self.run_pipeline("--resume", "missing-release")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release directory is missing", result.stderr)
        self.assertFalse((self.root / "build-calls.log").exists())

    def test_resume_rejects_unsafe_release_id(self) -> None:
        result = self.run_pipeline("--resume", "../candidate")

        self.assertEqual(result.returncode, 64)
        self.assertIn("invalid release ID", result.stderr)

    def test_resume_rejects_mutated_candidate_without_running_downstream_stages(self) -> None:
        interrupted = self.run_pipeline(
            CRASH_AFTER_STATE="1",
            CRASH_AFTER_STAGE="candidate-validation",
        )
        self.assertEqual(interrupted.returncode, 99, interrupted.stderr)
        release_id = self.candidate_release_id()
        manifest_path = self.data_root / "releases" / release_id / "stop-data" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["cities"][0]["name"] = "Changed after validation"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        resumed = self.run_pipeline("--resume", release_id)

        self.assertNotEqual(resumed.returncode, 0)
        self.assertIn("candidate integrity", resumed.stderr)
        self.assertEqual(
            (self.root / "build-calls.log").read_text(encoding="utf-8").splitlines(),
            ["build"],
        )
        self.assertFalse((self.root / "static-calls.log").exists())
        self.assertFalse((self.root / "systemctl.log").exists())
        self.assertFalse((self.root / "link-calls.log").exists())

    def test_resume_rejects_fifo_candidate_without_running_downstream_stages(self) -> None:
        interrupted = self.run_pipeline(
            CRASH_AFTER_STATE="1",
            CRASH_AFTER_STAGE="candidate-validation",
        )
        self.assertEqual(interrupted.returncode, 99, interrupted.stderr)
        release_id = self.candidate_release_id()
        fifo_path = self.data_root / "releases" / release_id / "stop-data" / "routes" / "unsupported.fifo"
        os.mkfifo(fifo_path)

        resumed = self.run_pipeline("--resume", release_id)

        self.assertNotEqual(resumed.returncode, 0)
        self.assertIn("FIFO", resumed.stderr)
        self.assertEqual(
            (self.root / "build-calls.log").read_text(encoding="utf-8").splitlines(),
            ["build"],
        )
        self.assertFalse((self.root / "static-calls.log").exists())
        self.assertFalse((self.root / "systemctl.log").exists())
        self.assertFalse((self.root / "link-calls.log").exists())

    def test_resume_after_each_persisted_stage_skips_heavy_build(self) -> None:
        for stage in (
            "build",
            "candidate-validation",
            "static-departures",
            "handoff-readiness",
            "commit",
        ):
            with self.subTest(stage=stage):
                if stage != "build":
                    self.tearDown()
                    self.setUp()
                self.configure_resume_pointer_layout()
                interrupted = self.run_pipeline(
                    CRASH_AFTER_STATE="1",
                    CRASH_AFTER_STAGE=stage,
                )

                self.assertEqual(interrupted.returncode, 99, interrupted.stderr)
                release_id = self.candidate_release_id()
                state = json.loads(
                    (self.data_root / "releases" / release_id / "release-state.json").read_text()
                )
                self.assertEqual(state["completedStage"], stage)

                resumed = self.run_pipeline("--resume", release_id)

                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(
                    (self.root / "build-calls.log").read_text(encoding="utf-8").splitlines(),
                    ["build"],
                )
                if stage == "commit":
                    self.assertIn("already-active", resumed.stdout)
                else:
                    final_state = json.loads(
                        (self.data_root / "releases" / release_id / "release-state.json").read_text()
                    )
                    self.assertEqual(final_state["completedStage"], "commit")

    def test_crash_after_activation_reconciles_state_without_activation(self) -> None:
        self.configure_resume_pointer_layout()
        interrupted = self.run_pipeline(CRASH_BEFORE_COMMIT="1")

        self.assertEqual(interrupted.returncode, 99, interrupted.stderr)
        release_id = self.candidate_release_id()
        release_dir = self.data_root / "releases" / release_id
        state = json.loads((release_dir / "release-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["completedStage"], "handoff-readiness")

        build_calls = (self.root / "build-calls.log").read_text(encoding="utf-8").splitlines()
        static_calls = (self.root / "static-calls.log").read_text(encoding="utf-8").splitlines()
        systemctl_calls = self.systemctl_calls()
        link_calls = (self.root / "link-calls.log").read_text(encoding="utf-8").splitlines()
        previous_target = os.readlink(self.data_root / "previous" / "stop-data")

        resumed = self.run_pipeline("--resume", release_id)

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("already-active", resumed.stdout)
        self.assertIn("state reconciled without activation", resumed.stdout)
        self.assertNotIn("activating canonical runtime", resumed.stdout)
        final_state = json.loads((release_dir / "release-state.json").read_text(encoding="utf-8"))
        self.assertEqual(final_state["completedStage"], "commit")
        self.assertEqual(
            (self.root / "build-calls.log").read_text(encoding="utf-8").splitlines(),
            build_calls,
        )
        self.assertEqual(
            (self.root / "static-calls.log").read_text(encoding="utf-8").splitlines(),
            static_calls,
        )
        self.assertEqual(self.systemctl_calls(), systemctl_calls)
        self.assertEqual(
            (self.root / "link-calls.log").read_text(encoding="utf-8").splitlines(),
            link_calls,
        )
        self.assertEqual(os.readlink(self.data_root / "previous" / "stop-data"), previous_target)
        self.assertFalse(list(release_dir.glob(".release-state-*.tmp")))

    def test_already_committed_resume_does_not_rewrite_state(self) -> None:
        self.configure_resume_pointer_layout()
        successful = self.run_pipeline()

        self.assertEqual(successful.returncode, 0, successful.stderr)
        release_id = self.candidate_release_id()
        release_dir = self.data_root / "releases" / release_id
        state_path = release_dir / "release-state.json"
        state_bytes = state_path.read_bytes()
        state_payload = json.loads(state_bytes)
        state_mtime_ns = state_path.stat().st_mtime_ns
        state_write_calls = (self.root / "state-write-calls.log").read_text(encoding="utf-8").splitlines()
        build_calls = (self.root / "build-calls.log").read_text(encoding="utf-8").splitlines()
        static_calls = (self.root / "static-calls.log").read_text(encoding="utf-8").splitlines()
        systemctl_calls = self.systemctl_calls()
        link_calls = (self.root / "link-calls.log").read_text(encoding="utf-8").splitlines()
        previous_path = self.data_root / "previous" / "stop-data"
        previous_target = os.readlink(previous_path)
        previous_mtime_ns = previous_path.lstat().st_mtime_ns

        resumed = self.run_pipeline("--resume", release_id)

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("already-active", resumed.stdout)
        self.assertNotIn("activating canonical runtime", resumed.stdout)
        self.assertEqual(state_path.read_bytes(), state_bytes)
        self.assertEqual(json.loads(state_path.read_bytes())["updatedAt"], state_payload["updatedAt"])
        self.assertEqual(state_path.stat().st_mtime_ns, state_mtime_ns)
        self.assertEqual(
            (self.root / "state-write-calls.log").read_text(encoding="utf-8").splitlines(),
            state_write_calls,
        )
        self.assertEqual(
            (self.root / "build-calls.log").read_text(encoding="utf-8").splitlines(),
            build_calls,
        )
        self.assertEqual(
            (self.root / "static-calls.log").read_text(encoding="utf-8").splitlines(),
            static_calls,
        )
        self.assertEqual(self.systemctl_calls(), systemctl_calls)
        self.assertEqual(
            (self.root / "link-calls.log").read_text(encoding="utf-8").splitlines(),
            link_calls,
        )
        self.assertEqual(os.readlink(previous_path), previous_target)
        self.assertEqual(previous_path.lstat().st_mtime_ns, previous_mtime_ns)


if __name__ == "__main__":
    unittest.main()
