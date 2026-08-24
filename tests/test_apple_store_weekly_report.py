import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from apple_store_business_events import NormalizedAppleStoreEvent
from apple_store_notification_store import AppleStoreNotificationStore
from apple_store_weekly_report import (
    build_weekly_summary,
    format_weekly_summary,
)


def timestamp(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def make_event(
    notification_uuid: str,
    notification_type: str,
    *,
    transaction_id: str | None,
    app: str = "haltewecker",
    environment: str = "Production",
    storefront: str | None = "DEU",
    price_milliunits: int | None = 990,
    currency: str | None = "EUR",
    revocation_type: str | None = None,
    revocation_percentage: int | None = None,
    received_at: int = timestamp("2026-08-23T16:00:00"),
) -> NormalizedAppleStoreEvent:
    purchase_kind = {
        "SUBSCRIBED": "monthly",
        "DID_RENEW": "monthly",
        "ONE_TIME_CHARGE": "lifetime",
        "REFUND": "monthly",
    }.get(notification_type, "unknown")
    return NormalizedAppleStoreEvent(
        notification_uuid=notification_uuid,
        notification_type=notification_type,
        subtype=None,
        app=app,
        bundle_id="com.aSoft.HalteWecker",
        environment=environment,
        product_id="product",
        purchase_kind=purchase_kind,
        transaction_id=transaction_id,
        original_transaction_id=None,
        purchase_date=None,
        expires_date=None,
        revocation_date=None,
        auto_renew_status=None,
        signed_date=None,
        received_at=received_at,
        is_handled=True,
        storefront=storefront,
        price_milliunits=price_milliunits,
        currency=currency,
        transaction_reason=None,
        revocation_type=revocation_type,
        revocation_percentage=revocation_percentage,
    )


class AppleStoreWeeklyReportTests(unittest.TestCase):
    def test_summary_deduplicates_by_transaction_and_separates_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.sqlite3"
            with AppleStoreNotificationStore(path) as store:
                store.insert_once(
                    make_event("subscription-1", "SUBSCRIBED", transaction_id="tx-1")
                )
                store.insert_once(
                    make_event("subscription-1-duplicate", "SUBSCRIBED", transaction_id="tx-1")
                )
                store.insert_once(
                    make_event("renewal-1", "DID_RENEW", transaction_id="renewal-tx-1")
                )
                store.insert_once(
                    make_event(
                        "pasty-1",
                        "ONE_TIME_CHARGE",
                        transaction_id="tx-2",
                        app="pasty",
                        price_milliunits=4990,
                    )
                )
                store.insert_once(
                    make_event(
                        "refund-1",
                        "REFUND",
                        transaction_id="tx-1",
                        price_milliunits=990,
                    )
                )
                store.insert_once(
                    make_event(
                        "sandbox-1",
                        "ONE_TIME_CHARGE",
                        transaction_id="sandbox-tx",
                        environment="Sandbox",
                        price_milliunits=17970,
                        currency="USD",
                    )
                )

            summary = build_weekly_summary(
                path,
                end_at=datetime(2026, 8, 24, 18, tzinfo=timezone.utc),
                environment="Production",
            )

        self.assertEqual(summary.new_subscriptions, 1)
        self.assertEqual(summary.renewals, 1)
        self.assertEqual(summary.lifetime_purchases, 1)
        self.assertEqual(summary.refunds, 1)
        self.assertEqual(summary.app_counts, {"haltewecker": 2, "pasty": 1})
        self.assertEqual(summary.storefront_counts, {"DEU": 3})
        self.assertEqual(summary.revenue_by_currency, {"EUR": Decimal("5.98")})

    def test_zero_summary_is_still_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.sqlite3"
            with AppleStoreNotificationStore(path):
                pass

            message = format_weekly_summary(
                build_weekly_summary(
                    path,
                    end_at=datetime(2026, 8, 24, 18, tzinfo=timezone.utc),
                )
            )

        self.assertIn("New subscriptions: 0", message)
        self.assertIn("Renewals: 0", message)
        self.assertIn("Lifetime purchases: 0", message)
        self.assertIn("Refunds: 0", message)
        self.assertIn("HalteWecker: 0", message)
        self.assertIn("Pasty: 0", message)
        self.assertIn("— 0.00", message)

    def test_refund_full_prorated_and_reversed_adjust_revenue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.sqlite3"
            with AppleStoreNotificationStore(path) as store:
                store.insert_once(
                    make_event(
                        "sale",
                        "ONE_TIME_CHARGE",
                        transaction_id="sale-tx",
                        price_milliunits=10000,
                    )
                )
                store.insert_once(
                    make_event(
                        "full-refund",
                        "REFUND",
                        transaction_id="full-tx",
                        price_milliunits=5000,
                        revocation_type="REFUND_FULL",
                    )
                )
                store.insert_once(
                    make_event(
                        "prorated-refund",
                        "REFUND",
                        transaction_id="prorated-tx",
                        price_milliunits=4990,
                        revocation_type="REFUND_PRORATED",
                        revocation_percentage=25000,
                    )
                )
                store.insert_once(
                    make_event(
                        "reversed-refund",
                        "REFUND_REVERSED",
                        transaction_id="full-tx",
                        price_milliunits=1,
                        currency="USD",
                        revocation_type=None,
                        revocation_percentage=None,
                    )
                )
                store.insert_once(
                    make_event(
                        "reversed-refund-duplicate",
                        "REFUND_REVERSED",
                        transaction_id="full-tx",
                        price_milliunits=1,
                        currency="USD",
                        revocation_type=None,
                        revocation_percentage=None,
                    )
                )

            summary = build_weekly_summary(
                path,
                end_at=datetime(2026, 8, 24, 18, tzinfo=timezone.utc),
            )

        self.assertEqual(summary.refunds, 2)
        self.assertEqual(summary.lifetime_purchases, 1)
        self.assertEqual(summary.revenue_by_currency, {"EUR": Decimal("8.7525")})

    def test_unresolved_refund_reversal_does_not_change_revenue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.sqlite3"
            with AppleStoreNotificationStore(path) as store:
                store.insert_once(
                    make_event(
                        "unresolved-reversal",
                        "REFUND_REVERSED",
                        transaction_id="missing-refund-tx",
                        price_milliunits=9990,
                        currency="USD",
                        revocation_percentage=100000,
                    )
                )

            with self.assertLogs("apple_store_weekly_report", level="WARNING") as logs:
                summary = build_weekly_summary(
                    path,
                    end_at=datetime(2026, 8, 24, 18, tzinfo=timezone.utc),
                )

        self.assertEqual(summary.revenue_by_currency, {})
        self.assertTrue(
            any("apple_store_refund_reversal_unresolved" in message for message in logs.output)
        )


if __name__ == "__main__":
    unittest.main()
