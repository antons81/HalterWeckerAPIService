import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PIPELINE = REPOSITORY_ROOT / "scripts" / "run_static_departures_pipeline.sh"


class StaticDeparturesPipelineTests(unittest.TestCase):
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

    def test_release_scoped_nightly_run_derives_artifacts_from_same_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            release_id = "release-a"
            release_dir = root / "releases" / release_id
            release_dir.mkdir(parents=True)
            (release_dir / "gtfs-artifacts.json").write_text("{}", encoding="utf-8")
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
            (release_dir / "gtfs-artifacts.json").write_text("{}", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
