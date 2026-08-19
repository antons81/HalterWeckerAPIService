import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

import static_departures_api
from appstoreserverlibrary.models.Environment import Environment
from apple_store_notifications import (
    AppleStoreNotificationVerificationError,
    AppleStoreNotificationVerifier,
    AppleVerifierCandidate,
    VerifiedAppleNotification,
    build_verifier_candidates,
    load_root_certificates,
)


class FakeSignedDataVerifier:
    def __init__(self, notification: object, fail_nested: bool = False) -> None:
        self.notification = notification
        self.fail_nested = fail_nested
        self.transaction_calls: list[str] = []
        self.renewal_calls: list[str] = []

    def verify_and_decode_notification(self, _signed_payload: str) -> object:
        return self.notification

    def verify_and_decode_signed_transaction(self, signed_transaction: str) -> object:
        self.transaction_calls.append(signed_transaction)
        if self.fail_nested:
            raise ValueError("invalid nested transaction")
        return object()

    def verify_and_decode_renewal_info(self, signed_renewal_info: str) -> object:
        self.renewal_calls.append(signed_renewal_info)
        if self.fail_nested:
            raise ValueError("invalid nested renewal")
        return object()


def make_notification(bundle_id: str, environment: Environment, nested: bool = False) -> object:
    data = SimpleNamespace(
        bundleId=bundle_id,
        appAppleId=6789654959 if bundle_id == "com.aSoft.HalteWecker" else 6766716767,
        environment=environment,
        signedTransactionInfo="nested-transaction" if nested else None,
        signedRenewalInfo="nested-renewal" if nested else None,
    )
    return SimpleNamespace(
        notificationUUID="notification-uuid",
        rawNotificationType="DID_RENEW",
        rawSubtype="INITIAL_BUY",
        signedDate=1_700_000_000_000,
        data=data,
        summary=None,
        externalPurchaseToken=None,
        appData=None,
    )


class AppleStoreNotificationVerifierTests(unittest.TestCase):
    def test_configuration_contains_both_apps_and_environments(self) -> None:
        candidates = build_verifier_candidates(enable_online_checks=False)
        self.assertEqual(len(candidates), 4)
        self.assertEqual(
            {(candidate.bundle_id, candidate.environment) for candidate in candidates},
            {
                ("com.aSoft.HalteWecker", Environment.PRODUCTION),
                ("com.aSoft.HalteWecker", Environment.SANDBOX),
                ("com.aSoft.Pasty", Environment.PRODUCTION),
                ("com.aSoft.Pasty", Environment.SANDBOX),
            },
        )

    def test_root_certificates_are_loaded_from_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "one.cer").write_bytes(b"one")
            (root / "two.cer").write_bytes(b"two")
            self.assertEqual(load_root_certificates(str(root)), (b"one", b"two"))

    def test_verified_notification_checks_nested_jws(self) -> None:
        fake = FakeSignedDataVerifier(
            make_notification("com.aSoft.HalteWecker", Environment.PRODUCTION, nested=True)
        )
        candidate = AppleVerifierCandidate(
            "haltewecker-production",
            "com.aSoft.HalteWecker",
            6789654959,
            Environment.PRODUCTION,
            fake,
        )

        result = AppleStoreNotificationVerifier((candidate,)).verify("signed")

        self.assertEqual(result.bundle_id, "com.aSoft.HalteWecker")
        self.assertEqual(fake.transaction_calls, ["nested-transaction"])
        self.assertEqual(fake.renewal_calls, ["nested-renewal"])

    def test_invalid_nested_jws_fails_verification(self) -> None:
        fake = FakeSignedDataVerifier(
            make_notification("com.aSoft.HalteWecker", Environment.PRODUCTION, nested=True),
            fail_nested=True,
        )
        candidate = AppleVerifierCandidate(
            "haltewecker-production",
            "com.aSoft.HalteWecker",
            6789654959,
            Environment.PRODUCTION,
            fake,
        )

        with self.assertRaises(AppleStoreNotificationVerificationError):
            AppleStoreNotificationVerifier((candidate,)).verify("signed")


class AppleStoreNotificationEndpointStubTests(unittest.TestCase):
    def test_verified_haltewecker_and_pasty_production_sandbox_notifications_are_accepted(self) -> None:
        class StubVerifier:
            def __init__(self, notification: VerifiedAppleNotification) -> None:
                self.notification = notification

            def verify(self, _signed_payload: str) -> VerifiedAppleNotification:
                return self.notification

        cases = (
            ("com.aSoft.HalteWecker", "PRODUCTION", 6789654959),
            ("com.aSoft.HalteWecker", "SANDBOX", 6789654959),
            ("com.aSoft.Pasty", "PRODUCTION", 6766716767),
            ("com.aSoft.Pasty", "SANDBOX", 6766716767),
        )
        for bundle_id, environment, _app_apple_id in cases:
            notification = VerifiedAppleNotification(
                "notification-uuid",
                "DID_RENEW",
                "INITIAL_BUY",
                bundle_id,
                environment,
                1_700_000_000_000,
            )
            with self.subTest(bundle_id=bundle_id, environment=environment):
                with patch.object(static_departures_api, "default_verifier", return_value=StubVerifier(notification)):
                    self.assertEqual(self._post_notification(), 200)

    def test_verification_failure_returns_bad_request(self) -> None:
        class FailingVerifier:
            def verify(self, _signed_payload: str) -> VerifiedAppleNotification:
                raise AppleStoreNotificationVerificationError

        with patch.object(static_departures_api, "default_verifier", return_value=FailingVerifier()):
            self.assertEqual(self._post_notification(), 400)

    @staticmethod
    def _post_notification() -> int:
        from http.server import ThreadingHTTPServer
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        class EndpointHandler(static_departures_api.Handler):
            pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), EndpointHandler)
        thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/apple/store-notifications",
                data=json.dumps({"signedPayload": "test"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status
            except HTTPError as error:
                return error.code
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
