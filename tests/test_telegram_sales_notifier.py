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
