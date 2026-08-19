import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from apple_store_business_events import NormalizedAppleStoreEvent
from apple_store_notification_store import (
    AppleStoreNotificationStore,
    AppleStoreNotificationStoreError,
)


def make_event(notification_uuid: str = "notification-uuid") -> NormalizedAppleStoreEvent:
    return NormalizedAppleStoreEvent(
        notification_uuid=notification_uuid,
        notification_type="DID_RENEW",
        subtype=None,
        app="haltewecker",
        bundle_id="com.aSoft.HalteWecker",
        environment="Production",
        product_id="com.asoft.haltewecker.monthly",
        purchase_kind="monthly",
        transaction_id="transaction-id",
        original_transaction_id="original-transaction-id",
        purchase_date=1_700_000_000_000,
        expires_date=1_800_000_000_000,
        revocation_date=None,
        auto_renew_status=1,
        signed_date=1_700_000_000_001,
        received_at=1_700_000_000_002,
        is_handled=True,
    )


class AppleStoreNotificationStoreTests(unittest.TestCase):
    def test_insert_and_schema_do_not_store_signed_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.sqlite3"
            with AppleStoreNotificationStore(path) as store:
                self.assertTrue(store.insert_once(make_event()))
                columns = {
                    row[1] for row in store._connection.execute(
                        "PRAGMA table_info(apple_store_notification_events)"
                    )
                }
                row = store._connection.execute(
                    "SELECT notification_uuid, product_id, purchase_kind FROM apple_store_notification_events"
                ).fetchone()

            self.assertEqual(columns, {
                "notification_uuid", "notification_type", "subtype", "app", "bundle_id",
                "environment", "product_id", "purchase_kind", "transaction_id",
                "original_transaction_id", "purchase_date", "expires_date",
                "revocation_date", "auto_renew_status", "signed_date", "received_at",
                "is_handled",
            })
            self.assertEqual(row, ("notification-uuid", "com.asoft.haltewecker.monthly", "monthly"))
            self.assertNotIn("signed_payload", columns)
            self.assertNotIn("signed_transaction_info", columns)
            self.assertNotIn("signed_renewal_info", columns)

    def test_duplicate_uuid_is_not_inserted_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with AppleStoreNotificationStore(Path(temp) / "events.sqlite3") as store:
                self.assertTrue(store.insert_once(make_event()))
                self.assertFalse(store.insert_once(make_event()))

    def test_reopen_preserves_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.sqlite3"
            first = AppleStoreNotificationStore(path)
            self.assertTrue(first.insert_once(make_event()))
            first.close()

            second = AppleStoreNotificationStore(path)
            try:
                self.assertFalse(second.insert_once(make_event()))
            finally:
                second.close()

    def test_concurrent_duplicate_attempts_have_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with AppleStoreNotificationStore(Path(temp) / "events.sqlite3") as store:
                with ThreadPoolExecutor(max_workers=12) as executor:
                    results = list(
                        executor.map(
                            lambda _index: store.insert_once(make_event("concurrent-uuid")),
                            range(24),
                        )
                    )
                self.assertEqual(results.count(True), 1)
                self.assertEqual(results.count(False), 23)

    def test_store_uses_default_sqlite_journal_without_wal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with AppleStoreNotificationStore(Path(temp) / "events.sqlite3") as store:
                journal_mode = store._connection.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(journal_mode.lower(), "delete")

    def test_storage_error_is_wrapped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.sqlite3"
            store = AppleStoreNotificationStore(path)
            store.close()
            with self.assertRaises(AppleStoreNotificationStoreError):
                store.insert_once(make_event())


if __name__ == "__main__":
    unittest.main()
