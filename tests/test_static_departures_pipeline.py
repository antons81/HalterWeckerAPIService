import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.artifact_provenance import artifact_provenance


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PIPELINE = REPOSITORY_ROOT / "scripts" / "run_static_departures_pipeline.sh"


class StaticDeparturesPipelineTests(unittest.TestCase):
    def test_standalone_import_cannot_overwrite_an_incremental_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "releases/immutable"
            release.mkdir(parents=True)
            (release / "release.json").write_text('{"releaseID":"immutable"}')
            database = release / "departures.sqlite"
            database.write_bytes(b"keep")
            environment = os.environ.copy()
            environment.update({
                "REPO": str(REPOSITORY_ROOT), "DATA_ROOT": str(root),
                "STOP_DATA_ENV_FILE": str(root / "missing.env"),
                "WMATA_API_KEY": "fixture", "WMATA_SECRET_FILE": str(root / "missing-secret.env"),
                "GTFS_URL": "https://example.invalid/germany.zip",
                "RELEASE_ID": "immutable", "SKIP_ACTIVATION": "0", "READINESS_ONLY": "0",
            })
            result = subprocess.run(["bash", str(PIPELINE)], env=environment, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("immutable incremental release requires", result.stderr)
            self.assertEqual(database.read_bytes(), b"keep")
            self.assertFalse((release / "departures-next.sqlite").exists())

    def _write_artifact_fixture(self, root: Path, release_dir: Path) -> Path:
        archive = release_dir / "germany.zip"
        archive.write_bytes(b"fixture")
        sources = json.loads((REPOSITORY_ROOT / "config/external-gtfs-sources.json").read_text())
        payload = {"sources": {"germany": {"path": str(archive)}},
                   "external": {source["id"]: {"path": str(archive)} for source in sources
                                if source.get("importIntoStaticDepartures") is True}}
        ireland = release_dir / "external-artifacts/ireland"
        ireland.mkdir(parents=True)
        (ireland / "stops.txt").write_text("stop_id,stop_name\nA,Alpha\n")
        digest, size = artifact_provenance(ireland)
        payload["external"]["ireland"] = {"path": str(ireland), "sha256": digest, "size": size}
        (release_dir / "gtfs-artifacts.json").write_text(json.dumps(payload))
        return archive

    def test_provider_runtime_mounts_data_root_without_legacy_database_file(self) -> None:
        compose_file = REPOSITORY_ROOT / "deploy" / "static-departures.compose.yml"
        compose = compose_file.read_text(encoding="utf-8")

        self.assertIn("- /srv/haltewecker/data:/data:ro", compose)
        self.assertNotIn(
            "/srv/haltewecker/data/departures-current.sqlite:/data/departures-current.sqlite:ro",
            compose,
        )

    def _run_readiness_with_mock_docker(
        self, root: Path, *, health_release_id: str, runtime_providers: str | None = None,
        pinned_image: str | None = None, conflicting_environment: bool = False
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        docker_log = root / "docker.log"
        docker_state = root / "docker-state"
        docker_state.write_text("canonical\n", encoding="utf-8")
        mock_docker = root / "docker"
        mock_docker.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"log={str(docker_log)!r}\n"
            f"state={str(docker_state)!r}\n"
            "printf '%s\\n' \"$*\" >> \"$log\"\n"
            "if [[ \"$1\" == inspect ]]; then\n"
            "  if [[ \"${2:-}\" == --format ]]; then echo sha256:tested; exit 0; fi\n"
            "  name=\"${2:-}\"\n"
            "  current=\"$(cat \"$state\")\"\n"
            "  if [[ \"$name\" == static-departures-api && \"$current\" == canonical ]]; then exit 0; fi\n"
            "  if [[ \"$name\" == static-departures-api-rollback-release-a && \"$current\" == rollback ]]; then exit 0; fi\n"
            "  exit 1\n"
            "fi\n"
            "if [[ \"$1\" == rename ]]; then\n"
            "  if [[ \"${3:-}\" == static-departures-api-rollback-release-a ]]; then printf '%s\\n' rollback > \"$state\"; else printf '%s\\n' canonical > \"$state\"; fi\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1\" == rm ]]; then printf '%s\\n' rollback > \"$state\"; exit 0; fi\n"
            "if [[ \"$1\" == stop || \"$1\" == start ]]; then exit 0; fi\n"
            "if [[ \"$1\" == compose ]]; then "
            f"printf '%s\\n' \"${{HALTEWECKER_STATIC_DEPARTURES_PROVIDER_IDS:-}}\" > {str(root / 'runtime-providers.log')!r}; "
            "printf '%s\\n' canonical > \"$state\"; exit 0; fi\n"
            "if [[ \"$1\" == exec ]]; then\n"
            f"  printf '%s\\n' '{{\"status\":\"ok\",\"database\":{{\"releaseID\":\"{health_release_id}\"}}}}'\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        mock_docker.chmod(0o755)
        environment_file = root / "haltewecker-stop-data.env"
        environment_file.write_text(
            "GTFS_URL=https://example.invalid/german.zip\n" + (
                "HALTEWECKER_STATIC_DEPARTURES_PROVIDER_IDS=israel-mot,germany\n" if runtime_providers else ""
            ) + ("STATIC_DEPARTURES_IMAGE=sha256:wrong\nHALTEWECKER_RUNTIME_PROVIDER_IDS=wrong-provider\n" if conflicting_environment else ""),
            encoding="utf-8",
        )
        wmata_file = root / "wmata.env"
        wmata_file.write_text("WMATA_API_KEY=test-secret\n", encoding="utf-8")
        environment = os.environ.copy()
        if runtime_providers is not None:
            environment["HALTEWECKER_RUNTIME_PROVIDER_IDS"] = runtime_providers
        if pinned_image is not None:
            environment["STATIC_DEPARTURES_IMAGE"] = pinned_image
        environment.update(
            {
                "REPO": str(REPOSITORY_ROOT),
                "DATA_ROOT": str(root),
                "STOP_DATA_ENV_FILE": str(environment_file),
                "WMATA_SECRET_FILE": str(wmata_file),
                "RELEASE_ID": "release-a",
                "READINESS_ONLY": "1",
                "PATH": f"{mock_docker.parent}:{environment['PATH']}",
            }
        )
        result = subprocess.run(
            ["bash", str(PIPELINE)],
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        return result, docker_log

    def test_pinned_activation_runs_exact_preflight_image_without_build_or_pull(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, log = self._run_readiness_with_mock_docker(Path(temporary), health_release_id="release-a", pinned_image="sha256:tested")
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text()
            self.assertIn("compose -p haltewecker-static-release-a", calls)
            self.assertIn("--no-build --pull never --no-deps --force-recreate", calls)
            self.assertNotIn("--build", calls)

    def test_changed_image_restores_preserved_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, log = self._run_readiness_with_mock_docker(Path(temporary), health_release_id="release-a", pinned_image="sha256:different")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("activated image differs", result.stderr)
            self.assertIn("start static-departures-api", log.read_text())

    def test_pinned_contract_survives_conflicting_environment_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, _log = self._run_readiness_with_mock_docker(
                root, health_release_id="release-a", pinned_image="sha256:tested",
                runtime_providers="israel-mot,germany", conflicting_environment=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "runtime-providers.log").read_text().strip(), "israel-mot,germany")

    def test_runtime_contract_overrides_stale_build_provider_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, _ = self._run_readiness_with_mock_docker(
                root, health_release_id="release-a", runtime_providers="israel-mot",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "runtime-providers.log").read_text().strip(), "israel-mot")

    def test_canonical_readiness_preserves_existing_container(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result, docker_log = self._run_readiness_with_mock_docker(
                Path(temporary_directory), health_release_id="release-a"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = docker_log.read_text(encoding="utf-8")
            self.assertIn(
                "rename static-departures-api static-departures-api-rollback-release-a",
                calls,
            )
            self.assertIn(
                "stop --time 30 static-departures-api-rollback-release-a", calls
            )
            self.assertIn("compose -f", calls)
            self.assertNotIn("rm -f static-departures-api", calls)

    def test_canonical_readiness_rolls_back_on_health_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            result, docker_log = self._run_readiness_with_mock_docker(
                root, health_release_id="wrong-release"
            )
            self.assertNotEqual(result.returncode, 0)
            calls = docker_log.read_text(encoding="utf-8")
            self.assertIn("rm -f static-departures-api", calls)
            self.assertIn(
                "rename static-departures-api-rollback-release-a static-departures-api",
                calls,
            )
            self.assertIn("start static-departures-api", calls)

    def test_standalone_run_fails_closed_without_successful_stop_data_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment_file = root / "haltewecker-stop-data.env"
            environment_file.write_text(
                "GTFS_URL=https://example.invalid/german.zip\n"
                "WMATA_API_KEY=operator-secret-value\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "REPO": str(REPOSITORY_ROOT),
                    "DATA_ROOT": str(root),
                    "STOP_DATA_ENV_FILE": str(environment_file),
                    "WMATA_SECRET_FILE": str(root / "missing-wmata.env"),
                    "RELEASE_ID": "",
                }
            )

            result = subprocess.run(
                ["bash", str(PIPELINE)],
                cwd=REPOSITORY_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no successful stop-data handoff", result.stderr)

    def test_standalone_run_rejects_handoff_from_previous_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "releases" / "old").mkdir(parents=True)
            (root / "releases" / "active").mkdir()
            (root / "static-departures-release").symlink_to("releases/old")
            (root / "current-release").symlink_to("releases/active")
            environment_file = root / "stop-data.env"
            environment_file.write_text("GTFS_URL=https://example.invalid/german.zip\nWMATA_API_KEY=test\n")
            environment = os.environ.copy()
            environment.update({
                "REPO": str(REPOSITORY_ROOT),
                "DATA_ROOT": str(root),
                "STOP_DATA_ENV_FILE": str(environment_file),
                "WMATA_SECRET_FILE": str(root / "missing-wmata.env"),
                "RELEASE_ID": "",
            })
            result = subprocess.run(
                ["bash", str(PIPELINE)], cwd=REPOSITORY_ROOT, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing stale standalone import", result.stderr)

    def test_release_scoped_nightly_run_derives_artifacts_from_same_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            release_id = "release-a"
            release_dir = root / "releases" / release_id
            release_dir.mkdir(parents=True)
            self._write_artifact_fixture(root, release_dir)
            (release_dir / "release-metadata.json").write_text(
                '{"releaseID": "release-a"}', encoding="utf-8"
            )
            (root / "static-departures-release").symlink_to("releases/release-a")
            observed_artifacts = root / "observed-artifacts-path"
            environment_file = root / "haltewecker-stop-data.env"
            environment_file.write_text(
                "GTFS_URL=https://example.invalid/german.zip\n"
                "WMATA_API_KEY=stale-base-value\n",
                encoding="utf-8",
            )
            mock_python = root / "python3"
            mock_python.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \"$*\" == *validate_stop_data_provenance.py* ]]; then\n"
                f"  args=(\"$@\"); for ((i=0; i<${{#args[@]}}; i++)); do if [[ \"${{args[i]}}\" == --artifacts ]]; then printf '%s\\n' \"${{args[i+1]}}\" > \"{observed_artifacts}\"; fi; done\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$*\" == *import_static_departures_database.py* ]]; then exit 0; fi\n"
                f"exec \"{sys.executable}\" \"$@\"\n",
                encoding="utf-8",
            )
            mock_python.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "REPO": str(REPOSITORY_ROOT),
                    "DATA_ROOT": str(root),
                    "STOP_DATA_ENV_FILE": str(environment_file),
                    "WMATA_SECRET_FILE": str(root / "missing-wmata.env"),
                    "RELEASE_ID": "",
                    "STOP_DATA_PATH": str(release_dir / "stop-data"),
                    "NEXT_DATABASE_PATH": str(root / "departures-next.sqlite"),
                    "SKIP_ACTIVATION": "1",
                    "PATH": f"{mock_python.parent}:{environment['PATH']}",
                }
            )

            result = subprocess.run(
                ["bash", str(PIPELINE)],
                cwd=REPOSITORY_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                observed_artifacts.read_text(encoding="utf-8").strip(),
                str(root / "static-departures-release" / "gtfs-artifacts.json"),
            )

    def test_systemd_environment_is_loaded_before_wmata_secret_for_importer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment_file = root / "haltewecker-stop-data.env"
            environment_file.write_text(
                "GTFS_URL=https://example.invalid/german.zip\n"
                "WMATA_API_KEY=stale-base-value\n",
                encoding="utf-8",
            )
            environment_file.write_text(
                environment_file.read_text(encoding="utf-8")
                + 'WMATA_ENV_FILE="${WMATA_ENV_FILE:-/srv/haltewecker/secrets/wmata/.env}"\n',
                encoding="utf-8",
            )
            wmata_file = root / "wmata.env"
            wmata_file.write_text("WMATA_API_KEY=operator-secret-value\n", encoding="utf-8")
            release_dir = root / "releases" / "release-a"
            release_dir.mkdir(parents=True)
            (release_dir / "release-metadata.json").write_text(
                '{"releaseID": "release-a"}', encoding="utf-8"
            )
            self._write_artifact_fixture(root, release_dir)
            (root / "static-departures-release").symlink_to("releases/release-a")
            importer_observation = root / "importer.env"
            mock_python = root / "python3"
            mock_python.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \"$*\" == *import_static_departures_database.py* ]]; then\n"
                f"  printf '%s\\n' \"$WMATA_API_KEY\" > \"{importer_observation}\"\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$*\" == *validate_stop_data_provenance.py* ]]; then exit 0; fi\n"
                f"exec \"{sys.executable}\" \"$@\"\n",
                encoding="utf-8",
            )
            mock_python.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "REPO": str(REPOSITORY_ROOT),
                    "STOP_DATA_ENV_FILE": str(environment_file),
                    "WMATA_SECRET_FILE": str(wmata_file),
                    "DATA_ROOT": str(root),
                    "NEXT_DATABASE_PATH": str(root / "departures-next.sqlite"),
                    "STOP_DATA_PATH": str(root / "stop-data"),
                    "RELEASE_ID": "",
                    "SKIP_ACTIVATION": "1",
                    "PATH": f"{mock_python.parent}:{environment['PATH']}",
                    "WMATA_API_KEY": "inherited-old-value",
                }
            )

            result = subprocess.run(
                ["bash", str(PIPELINE)],
                cwd=REPOSITORY_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                importer_observation.read_text(encoding="utf-8").strip(),
                "operator-secret-value",
            )
            before = importer_observation.stat().st_mtime_ns
            artifacts_path = release_dir / "gtfs-artifacts.json"
            payload = json.loads(artifacts_path.read_text())
            del payload["external"]["finland-hsl"]
            artifacts_path.write_text(json.dumps(payload))
            incomplete = subprocess.run(["bash", str(PIPELINE)], cwd=REPOSITORY_ROOT,
                                        env=environment, text=True, capture_output=True)
            self.assertNotEqual(incomplete.returncode, 0)
            self.assertIn("incomplete release-scoped static import plan", incomplete.stderr)
            self.assertEqual(importer_observation.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
