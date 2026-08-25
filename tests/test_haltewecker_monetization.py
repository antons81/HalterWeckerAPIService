import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import haltewecker_monetization as utility


class HalteWeckerMonetizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_directory.name) / "haltewecker.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "monetizationFlow": "app_trial",
                    "existingField": {"keep": True},
                }
            ),
            encoding="utf-8",
        )
        self.environment = {
            utility.CONFIG_PATH_ENVIRONMENT_VARIABLE: str(self.config_path)
        }

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def run_main(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = utility.main(arguments, self.environment, stdout, stderr)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_app_trial_update_preserves_existing_fields(self) -> None:
        exit_code, stdout, stderr = self.run_main(["app_trial"])

        self.assertEqual(exit_code, 0)
        self.assertIn("HalteWecker monetization flow: app_trial", stdout)
        self.assertIn(str(self.config_path), stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8")),
            {
                "version": 1,
                "monetizationFlow": "app_trial",
                "existingField": {"keep": True},
            },
        )

    def test_storekit_trial_update_changes_only_strategy(self) -> None:
        exit_code, stdout, stderr = self.run_main(["storekit_trial"])

        self.assertEqual(exit_code, 0)
        self.assertIn("HalteWecker monetization flow: storekit_trial", stdout)
        self.assertEqual(stderr, "")
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(document["monetizationFlow"], "storekit_trial")
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["existingField"], {"keep": True})

    def test_invalid_flow_fails_without_changing_file(self) -> None:
        original_bytes = self.config_path.read_bytes()

        exit_code, stdout, stderr = self.run_main(["invalid"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("allowed values: app_trial, storekit_trial", stderr)
        self.assertEqual(self.config_path.read_bytes(), original_bytes)

    def test_wrong_argument_count_fails_without_changing_file(self) -> None:
        original_bytes = self.config_path.read_bytes()

        exit_code, stdout, stderr = self.run_main([])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Usage: haltewecker-monetization", stderr)
        self.assertEqual(self.config_path.read_bytes(), original_bytes)

    def test_malformed_existing_config_is_not_replaced(self) -> None:
        original_bytes = b"{broken"
        self.config_path.write_bytes(original_bytes)

        exit_code, stdout, stderr = self.run_main(["storekit_trial"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("not valid JSON", stderr)
        self.assertEqual(self.config_path.read_bytes(), original_bytes)

    def test_missing_config_is_created_with_supported_version(self) -> None:
        self.config_path.unlink()

        exit_code, stdout, stderr = self.run_main(["app_trial"])

        self.assertEqual(exit_code, 0)
        self.assertIn("HalteWecker monetization flow: app_trial", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8")),
            {"version": 1, "monetizationFlow": "app_trial"},
        )

    def test_atomic_replace_is_used(self) -> None:
        with patch.object(utility.os, "replace", wraps=os.replace) as replace:
            exit_code, _, stderr = self.run_main(["storekit_trial"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        replace.assert_called_once()
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8"))["monetizationFlow"],
            "storekit_trial",
        )

    def test_existing_file_mode_is_preserved(self) -> None:
        os.chmod(self.config_path, 0o640)

        exit_code, _, stderr = self.run_main(["storekit_trial"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o640)


if __name__ == "__main__":
    unittest.main()
