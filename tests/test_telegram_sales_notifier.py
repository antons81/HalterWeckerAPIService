import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from apple_store_business_events import NormalizedAppleStoreEvent
from telegram_sales_notifier import (
    TelegramSalesNotificationError,
    TelegramSalesNotifier,
    format_sales_message,
    format_test_message,
)


def make_event(**overrides: object) -> NormalizedAppleStoreEvent:
    values: dict[str, object] = {
        "notification_uuid": "notification-uuid",
        "notification_type": "SUBSCRIBED",
        "subtype": None,
        "app": "haltewecker",
        "bundle_id": "com.aSoft.HalteWecker",
        "environment": "Production",
        "product_id": "com.asoft.haltewecker.monthly",
        "purchase_kind": "monthly",
        "transaction_id": None,
        "original_transaction_id": None,
        "purchase_date": None,
        "expires_date": None,
        "revocation_date": None,
        "auto_renew_status": None,
        "signed_date": None,
        "received_at": 1,
        "is_handled": True,
    }
    values.update(overrides)
    return NormalizedAppleStoreEvent(**values)


class TelegramSalesNotifierTests(unittest.TestCase):
    def test_missing_environment_configuration_disables_notifier(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(TelegramSalesNotifier.from_environment())

    def test_format_monthly_subscription(self) -> None:
        message = format_sales_message(make_event())
        self.assertEqual(
            message,
            "💰 HalteWecker\n"
            "New monthly subscription\n"
            "Environment: Production\n"
            "Product: com.asoft.haltewecker.monthly",
        )

    def test_format_renewal_and_pasty_lifetime_messages(self) -> None:
        self.assertIn(
            "Yearly subscription renewed",
            format_sales_message(
                make_event(
                    notification_type="DID_RENEW",
                    purchase_kind="yearly",
                    product_id="com.asoft.haltewecker.yearly",
                )
            ),
        )
        self.assertEqual(
            format_sales_message(
                make_event(
                    notification_type="ONE_TIME_CHARGE",
                    app="pasty",
                    bundle_id="com.aSoft.Pasty",
                    product_id="com.aSoft.Pasty.pro.lifetime",
                    purchase_kind="lifetime",
                )
            ),
            "♾️ Pasty\n"
            "Lifetime purchased\n"
            "Environment: Production\n"
            "Product: com.aSoft.Pasty.pro.lifetime",
        )

    def test_format_refund_message(self) -> None:
        self.assertIn(
            "↩️ HalteWecker\nRefund",
            format_sales_message(
                make_event(notification_type="REFUND")
            ),
        )

    def test_format_auto_renew_disabled_message_with_access_until(self) -> None:
        message = format_sales_message(
            make_event(
                notification_type="DID_CHANGE_RENEWAL_STATUS",
                subtype="AUTO_RENEW_DISABLED",
                expires_date=1_800_000_000_000,
                storefront="DEU",
            )
        )

        self.assertEqual(
            message,
            "🔕 HalteWecker\n"
            "Auto-renew disabled\n"
            "Storefront: DEU\n"
            "Auto-renew: OFF\n"
            "Access until: 2027-01-15 08:00 UTC\n"
            "Environment: Production\n"
            "Product: com.asoft.haltewecker.monthly",
        )

    def test_format_auto_renew_enabled_message(self) -> None:
        message = format_sales_message(
            make_event(
                notification_type="DID_CHANGE_RENEWAL_STATUS",
                subtype="AUTO_RENEW_ENABLED",
                storefront="USA",
            )
        )

        self.assertEqual(
            message,
            "🔔 HalteWecker\n"
            "Auto-renew enabled\n"
            "Storefront: USA\n"
            "Auto-renew: ON\n"
            "Environment: Production\n"
            "Product: com.asoft.haltewecker.monthly",
        )

    def test_format_purchase_with_price_transaction_and_trial(self) -> None:
        message = format_sales_message(
            make_event(
                storefront="DEU",
                price_milliunits=990,
                currency="EUR",
                transaction_reason="PURCHASE",
                offer_type="INTRODUCTORY_OFFER",
                offer_identifier="intro-offer",
                offer_discount_type="FREE_TRIAL",
                offer_period="P1M",
            )
        )

        self.assertEqual(
            message,
            "💰 HalteWecker\n"
            "New monthly subscription\n"
            "Storefront: DEU\n"
            "Price: 0.99 EUR\n"
            "Transaction: Purchase\n"
            "Offer: INTRODUCTORY_OFFER\n"
            "Offer ID: intro-offer\n"
            "Trial: Yes\n"
            "Offer period: P1M\n"
            "Environment: Production\n"
            "Product: com.asoft.haltewecker.monthly",
        )

    def test_format_refund_with_renewal_reason(self) -> None:
        message = format_sales_message(
            make_event(
                notification_type="REFUND",
                storefront="USA",
                price_milliunits=4990,
                currency="USD",
                transaction_reason="RENEWAL",
                revocation_type="REFUND_PRORATED",
                revocation_percentage=50,
            )
        )

        self.assertIn("Storefront: USA", message)
        self.assertIn("Price: 4.99 USD", message)
        self.assertIn("Transaction: Renewal", message)
        self.assertIn("Refund type: REFUND_PRORATED", message)
        self.assertIn("Refund percentage: 50%", message)

    def test_unknown_renewal_status_subtype_keeps_generic_message(self) -> None:
        message = format_sales_message(
            make_event(
                notification_type="DID_CHANGE_RENEWAL_STATUS",
                subtype="UNKNOWN_SUBTYPE",
            )
        )

        self.assertIn("Renewal status changed", message)
        self.assertNotIn("Auto-renew:", message)
        self.assertNotIn("Access until:", message)

    def test_format_test_message(self) -> None:
        self.assertEqual(
            format_test_message(
                make_event(
                    notification_type="TEST",
                    product_id=None,
                    purchase_kind="unknown",
                    is_handled=False,
                )
            ),
            "🧪 Apple Store TEST\nHalteWecker\nEnvironment: Production",
        )
        self.assertIsNone(format_test_message(make_event()))

    def test_successful_send_uses_configured_chat_and_timeout(self) -> None:
        calls: list[tuple[str, bytes, dict[str, str], float]] = []

        def transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
            calls.append((url, body, headers, timeout))
            return 200

        notifier = TelegramSalesNotifier(
            "bot-token",
            "private-chat",
            timeout=3.0,
            transport=transport,
        )
        notifier.send(make_event())

        self.assertEqual(len(calls), 1)
        url, body, headers, timeout = calls[0]
        self.assertEqual(url, "https://api.telegram.org/botbot-token/sendMessage")
        self.assertIn('"chat_id":"private-chat"', body.decode())
        self.assertIn("New monthly subscription", body.decode())
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(timeout, 3.0)

    def test_successful_report_send_uses_configured_transport(self) -> None:
        calls: list[tuple[str, bytes, dict[str, str], float]] = []

        def transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
            calls.append((url, body, headers, timeout))
            return 200

        notifier = TelegramSalesNotifier("token", "chat", transport=transport)
        notifier.send_report("📊 weekly", environment="Production")

        self.assertEqual(len(calls), 1)
        self.assertIn("weekly", calls[0][1].decode())

    def test_api_failure_is_reported_without_exposing_token(self) -> None:
        notifier = TelegramSalesNotifier(
            "private-bot-token",
            "private-chat",
            transport=lambda *_args: 503,
        )

        with self.assertRaises(TelegramSalesNotificationError) as context:
            notifier.send(make_event())

        self.assertEqual(context.exception.reason, "http_status=503")
        self.assertNotIn("private-bot-token", str(context.exception))

    def test_sandbox_is_disabled_by_default(self) -> None:
        calls: list[object] = []
        notifier = TelegramSalesNotifier(
            "token",
            "chat",
            transport=lambda *_args: calls.append(True) or 200,
        )

        notifier.send(make_event(environment="Sandbox"))

        self.assertEqual(calls, [])

    def test_sandbox_can_be_enabled(self) -> None:
        calls: list[object] = []
        notifier = TelegramSalesNotifier(
            "token",
            "chat",
            notify_sandbox=True,
            transport=lambda *_args: calls.append(True) or 200,
        )

        notifier.send(make_event(environment="Sandbox"))

        self.assertEqual(calls, [True])

    def test_test_notification_is_disabled_by_default(self) -> None:
        calls: list[object] = []
        notifier = TelegramSalesNotifier(
            "token",
            "chat",
            transport=lambda *_args: calls.append(True) or 200,
        )

        notifier.send_test(
            make_event(
                notification_type="TEST",
                product_id=None,
                purchase_kind="unknown",
                is_handled=False,
            )
        )

        self.assertEqual(calls, [])

    def test_test_notification_can_be_enabled(self) -> None:
        calls: list[tuple[str, bytes, dict[str, str], float]] = []

        def transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
            calls.append((url, body, headers, timeout))
            return 200

        notifier = TelegramSalesNotifier(
            "token",
            "chat",
            notify_test=True,
            transport=transport,
        )
        notifier.send_test(
            make_event(
                notification_type="TEST",
                product_id=None,
                purchase_kind="unknown",
                is_handled=False,
            )
        )

        self.assertTrue(notifier.notify_test)
        self.assertEqual(len(calls), 1)
        self.assertIn("Apple Store TEST", calls[0][1].decode())

    def test_test_notification_flag_is_read_from_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_SALES_BOT_TOKEN": "token",
                "TELEGRAM_SALES_CHAT_ID": "chat",
                "TELEGRAM_SALES_NOTIFY_TEST": "true",
            },
            clear=True,
        ):
            notifier = TelegramSalesNotifier.from_environment()

        self.assertIsNotNone(notifier)
        assert notifier is not None
        self.assertTrue(notifier.notify_test)

    def test_unhandled_event_is_not_sent(self) -> None:
        calls: list[object] = []
        notifier = TelegramSalesNotifier(
            "token",
            "chat",
            transport=lambda *_args: calls.append(True) or 200,
        )

        notifier.send(make_event(is_handled=False))

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
