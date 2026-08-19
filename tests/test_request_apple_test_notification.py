import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from appstoreserverlibrary.api_client import APIException
from appstoreserverlibrary.models.Environment import Environment

from scripts import request_apple_test_notification as utility


class RequestAppleTestNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.private_key_path = Path(self.temp_directory.name) / "SubscriptionKey.p8"
        self.private_key_path.write_bytes(b"test-private-key")
        self.environment = {
            "APPLE_ISSUER_ID": "issuer-id",
            "APPLE_KEY_ID": "key-id",
            "APPLE_IAP_PRIVATE_KEY_PATH": str(self.private_key_path),
            "APPLE_BUNDLE_ID": "com.aSoft.HalteWecker",
            "APPLE_APPLE_ID": "6789654959",
            "APPLE_ENVIRONMENT": "production",
        }

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def run_main(self, environment: dict[str, str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = utility.main(environment, stdout, stderr)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_missing_environment_variable(self) -> None:
        environment = dict(self.environment)
        del environment["APPLE_KEY_ID"]

        exit_code, stdout, stderr = self.run_main(environment)

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("APPLE_KEY_ID", stderr)

    def test_missing_private_key_file(self) -> None:
        environment = dict(self.environment)
        environment["APPLE_IAP_PRIVATE_KEY_PATH"] = str(
            Path(self.temp_directory.name) / "missing.p8"
        )

        exit_code, stdout, stderr = self.run_main(environment)

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Private key file not found", stderr)

    def test_invalid_environment(self) -> None:
        environment = dict(self.environment)
        environment["APPLE_ENVIRONMENT"] = "development"

        exit_code, stdout, stderr = self.run_main(environment)

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Invalid APPLE_ENVIRONMENT", stderr)

    @patch.object(utility, "AppStoreServerAPIClient")
    def test_successful_request_uses_official_client(self, client_class) -> None:
        client_class.return_value.request_test_notification.return_value = SimpleNamespace(
            testNotificationToken="safe-test-token"
        )

        exit_code, stdout, stderr = self.run_main(self.environment)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            stdout,
            "environment=Production\n"
            "bundleId=com.aSoft.HalteWecker\n"
            "testNotificationToken=safe-test-token\n",
        )
        self.assertEqual(stderr, "")
        client_class.assert_called_once_with(
            signing_key=b"test-private-key",
            key_id="key-id",
            issuer_id="issuer-id",
            bundle_id="com.aSoft.HalteWecker",
            environment=Environment.PRODUCTION,
        )
        client_class.return_value.request_test_notification.assert_called_once_with()

    @patch.object(utility, "AppStoreServerAPIClient")
    def test_apple_api_failure_is_reported_without_secrets(self, client_class) -> None:
        client_class.return_value.request_test_notification.side_effect = APIException(
            401,
            raw_api_error=4010000,
            error_message="unauthorized",
        )

        exit_code, stdout, stderr = self.run_main(self.environment)

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Apple API authentication failure", stderr)
        self.assertIn("http_status=401", stderr)
        self.assertIn("apple_error_code=4010000", stderr)
        self.assertIn("message=unauthorized", stderr)
        self.assertNotIn("test-private-key", stderr)


if __name__ == "__main__":
    unittest.main()
