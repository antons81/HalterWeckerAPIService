import subprocess
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ScriptImportModeTests(unittest.TestCase):
    def run_python(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *arguments],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_process_succeeded(self, process: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(
            process.returncode,
            0,
            msg=f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}",
        )

    def test_build_stop_packages_supports_package_import(self) -> None:
        process = self.run_python(
            "-c",
            "from scripts.build_stop_packages import load_cities, transit_radar_manifest",
        )

        self.assert_process_succeeded(process)

    def test_build_stop_packages_supports_direct_execution(self) -> None:
        process = self.run_python("scripts/build_stop_packages.py", "--help")

        self.assert_process_succeeded(process)

    def test_external_gtfs_supports_package_import(self) -> None:
        process = self.run_python("-c", "import scripts.external_gtfs")

        self.assert_process_succeeded(process)


if __name__ == "__main__":
    unittest.main()
