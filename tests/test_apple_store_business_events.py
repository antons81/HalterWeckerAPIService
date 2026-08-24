import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from apple_store_business_events import (
    UNKNOWN_PURCHASE_KIND,
    classify_product,
    load_product_mapping,
    normalize_notification,
)
from apple_store_notifications import VerifiedAppleNotification


def make_notification(
    notification_type: str,
    bundle_id: str,
    product_id: str | None = None,
    transaction_id: str | None = "transaction-id",
    original_transaction_id: str | None = "original-transaction-id",
    renewal: bool = False,
    storefront: str | None = None,
) -> VerifiedAppleNotification:
    transaction = (
        SimpleNamespace(
            productId=product_id,
            transactionId=transaction_id,
            originalTransactionId=original_transaction_id,
            transactionReason=("RENEWAL" if notification_type == "DID_RENEW" else "PURCHASE"),
            rawTransactionReason=("RENEWAL" if notification_type == "DID_RENEW" else "PURCHASE"),
            purchaseDate=1_700_000_000_001,
            expiresDate=1_800_000_000_001,
            revocationDate=None,
            price=990,
            currency="EUR",
            storefront=storefront,
        )
        if product_id and not renewal
        else None
    )
    renewal_info = (
        SimpleNamespace(
            productId=product_id,
            autoRenewProductId=product_id,
            originalTransactionId=original_transaction_id,
            rawAutoRenewStatus=1,
            autoRenewStatus=1,
            renewalPrice=990,
            currency="EUR",
        )
        if renewal
        else None
    )
    return VerifiedAppleNotification(
        notification_uuid=f"uuid-{notification_type}-{product_id}",
        notification_type=notification_type,
        subtype="INITIAL_BUY",
        bundle_id=bundle_id,
        environment="Production",
        signed_date=1_700_000_000_002,
        transaction_info=transaction,
        renewal_info=renewal_info,
    )


class AppleStoreBusinessEventTests(unittest.TestCase):
    def test_all_confirmed_product_mappings(self) -> None:
        mapping = load_product_mapping()
        expected = {
            ("com.aSoft.HalteWecker", "com.asoft.haltewecker.monthly"): ("haltewecker", "monthly"),
            ("com.aSoft.HalteWecker", "com.asoft.haltewecker.yearly"): ("haltewecker", "yearly"),
            ("com.aSoft.HalteWecker", "com.asoft.haltewecker.unlimited.new"): ("haltewecker", "lifetime_current"),
            ("com.aSoft.HalteWecker", "com.asoft.haltewecker.unlimited"): ("haltewecker", "lifetime_legacy"),
            ("com.aSoft.Pasty", "com.aSoft.Pasty.pro.lifetime"): ("pasty", "lifetime"),
        }
        actual = {
            (bundle_id, product_id): (classification.app, classification.purchase_kind)
            for bundle_id, products in mapping.items()
            for product_id, classification in products.items()
        }
        self.assertEqual(actual, expected)

    def test_monthly_and_yearly_subscription_events(self) -> None:
        for product_id, purchase_kind in (
            ("com.asoft.haltewecker.monthly", "monthly"),
            ("com.asoft.haltewecker.yearly", "yearly"),
        ):
            with self.subTest(product_id=product_id):
                event = normalize_notification(
                    make_notification("SUBSCRIBED", "com.aSoft.HalteWecker", product_id),
                    received_at=1_700_000_000_003,
                )
                self.assertEqual(event.purchase_kind, purchase_kind)
                self.assertTrue(event.is_handled)
                self.assertEqual(event.received_at, 1_700_000_000_003)

    def test_did_renew_uses_renewal_product_and_status(self) -> None:
        event = normalize_notification(
            make_notification(
                "DID_RENEW",
                "com.aSoft.HalteWecker",
                "com.asoft.haltewecker.monthly",
                renewal=True,
            )
        )

        self.assertEqual(event.product_id, "com.asoft.haltewecker.monthly")
        self.assertEqual(event.auto_renew_status, 1)
        self.assertEqual(event.original_transaction_id, "original-transaction-id")
        self.assertEqual(event.transaction_reason, "RENEWAL")
        self.assertEqual(event.price_milliunits, 990)
        self.assertEqual(event.currency, "EUR")
        self.assertTrue(event.is_handled)

    def test_storefront_is_preserved_from_transaction(self) -> None:
        event = normalize_notification(
            make_notification(
                "DID_CHANGE_RENEWAL_STATUS",
                "com.aSoft.HalteWecker",
                "com.asoft.haltewecker.monthly",
                storefront="DEU",
            )
        )

        self.assertEqual(event.storefront, "DEU")

    def test_lifetime_current_legacy_and_pasty_are_non_subscription_events(self) -> None:
        cases = (
            ("com.aSoft.HalteWecker", "com.asoft.haltewecker.unlimited.new", "lifetime_current"),
            ("com.aSoft.HalteWecker", "com.asoft.haltewecker.unlimited", "lifetime_legacy"),
            ("com.aSoft.Pasty", "com.aSoft.Pasty.pro.lifetime", "lifetime"),
        )
        for bundle_id, product_id, purchase_kind in cases:
            with self.subTest(bundle_id=bundle_id, product_id=product_id):
                event = normalize_notification(
                    make_notification("ONE_TIME_CHARGE", bundle_id, product_id)
                )
                self.assertEqual(event.purchase_kind, purchase_kind)
                self.assertTrue(event.is_handled)

    def test_refund_and_refund_reversed_are_classified(self) -> None:
        for notification_type in ("REFUND", "REFUND_REVERSED"):
            with self.subTest(notification_type=notification_type):
                event = normalize_notification(
                    make_notification(
                        notification_type,
                        "com.aSoft.Pasty",
                        "com.aSoft.Pasty.pro.lifetime",
                    )
                )
                self.assertTrue(event.is_handled)
                self.assertEqual(event.notification_type, notification_type)

    def test_unknown_product_is_safe_and_unhandled(self) -> None:
        event = normalize_notification(
            make_notification(
                "ONE_TIME_CHARGE",
                "com.aSoft.HalteWecker",
                "com.asoft.haltewecker.future-product",
            )
        )

        self.assertEqual(event.app, "haltewecker")
        self.assertEqual(event.purchase_kind, UNKNOWN_PURCHASE_KIND)
        self.assertFalse(event.is_handled)

    def test_unknown_notification_type_is_safe_and_unhandled(self) -> None:
        event = normalize_notification(
            make_notification(
                "FUTURE_APPLE_NOTIFICATION",
                "com.aSoft.Pasty",
                "com.aSoft.Pasty.pro.lifetime",
            )
        )

        self.assertEqual(event.purchase_kind, "lifetime")
        self.assertFalse(event.is_handled)

    def test_test_notification_without_transaction_data_is_safe(self) -> None:
        event = normalize_notification(
            make_notification(
                "TEST",
                "com.aSoft.HalteWecker",
                product_id=None,
                transaction_id=None,
                original_transaction_id=None,
            )
        )

        self.assertEqual(event.app, "haltewecker")
        self.assertIsNone(event.product_id)
        self.assertIsNone(event.transaction_id)
        self.assertFalse(event.is_handled)

    def test_mapping_file_is_valid_json(self) -> None:
        path = Path(__file__).resolve().parents[1] / "config/apple/store-notification-products.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("products", payload)

    def test_unknown_bundle_is_safe(self) -> None:
        classification = classify_product("com.example.unknown", "product")
        self.assertEqual(classification.app, "unknown")
        self.assertEqual(classification.purchase_kind, UNKNOWN_PURCHASE_KIND)


if __name__ == "__main__":
    unittest.main()
