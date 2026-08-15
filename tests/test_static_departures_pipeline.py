import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PIPELINE = REPOSITORY_ROOT / "scripts" / "run_static_departures_pipeline.sh"


class StaticDeparturesPipelineTests(unittest.TestCase):
    def test_systemd_environment_is_loaded_before_wmata_secret_for_importer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment_file = root / "haltewecker-stop-data.env"
            environment_file.write_text(
                "GTFS_URL=https://example.invalid/german.zip\n"
                "WMATA_API_KEY=stale-base-value\n",
                encoding="utf-8",
            )
            wmata_file = root / "wmata.env"
            wmata_file.write_text("WMATA_API_KEY=operator-secret-value\n", encoding="utf-8")
            importer_observation = root / "importer.env"
            mock_python = root / "python3"
            mock_python.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \"$*\" == *import_static_departures_database.py* ]]; then\n"
                f"  printf '%s\\n' \"$WMATA_API_KEY\" > \"{importer_observation}\"\n"
                "  exit 0\n"
                "fi\n"
                f"exec \"{sys.executable}\" \"$@\"\n",
                encoding="utf-8",
            )
            mock_python.chmod(0o755)
            mock_date = root / "date"
            mock_date.write_text("#!/usr/bin/env bash\nprintf '%s\\n' 2026-08-15T00:00:00+00:00\n", encoding="utf-8")
            mock_date.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "REPO": str(REPOSITORY_ROOT),
                    "STOP_DATA_ENV_FILE": str(environment_file),
                    "WMATA_ENV_FILE": str(wmata_file),
                    "NEXT_DATABASE_PATH": str(root / "departures-next.sqlite"),
                    "STOP_DATA_PATH": str(root / "stop-data"),
                    "RELEASE_ID": "test-release",
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
