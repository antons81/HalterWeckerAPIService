"""Best-effort outbound Telegram notifications for Apple Store business events."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from apple_store_business_events import NormalizedAppleStoreEvent


TELEGRAM_API_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 5.0


class TelegramSalesNotificationError(RuntimeError):
    """Raised when Telegram cannot accept a sales notification."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


Transport = Callable[[str, bytes, Mapping[str, str], float], int]


def _default_transport(
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
) -> int:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read()
            return int(response.status)
    except HTTPError as error:
        raise TelegramSalesNotificationError(f"http_status={error.code}") from None
    except (OSError, URLError, TimeoutError) as error:
        raise TelegramSalesNotificationError(
            f"network_error={type(error).__name__}"
        ) from None


def _environment_label(environment: str) -> str:
    return "Sandbox" if environment.casefold() == "sandbox" else "Production"


def _purchase_label(purchase_kind: str) -> str:
    return {
        "monthly": "monthly",
        "yearly": "yearly",
    }.get(purchase_kind, "")


def _event_title(event: NormalizedAppleStoreEvent) -> tuple[str, str]:
    notification_type = event.notification_type
    purchase_label = _purchase_label(event.purchase_kind)

    if notification_type == "SUBSCRIBED":
        if purchase_label:
            return "💰", f"New {purchase_label} subscription"
        return "♾️", "Lifetime purchased"
    if notification_type == "DID_RENEW":
        if purchase_label:
            return "🔁", f"{purchase_label.capitalize()} subscription renewed"
        return "🔁", "Subscription renewed"
    if notification_type == "ONE_TIME_CHARGE":
        return "♾️", "Lifetime purchased"
    if notification_type == "REFUND":
        return "↩️", "Refund"
    if notification_type == "REFUND_REVERSED":
        return "↩️", "Refund reversed"
    if notification_type == "DID_FAIL_TO_RENEW":
        return "⚠️", "Subscription renewal failed"
    if notification_type == "EXPIRED":
        return "⏳", "Subscription expired"
    if notification_type == "GRACE_PERIOD_EXPIRED":
        return "⏳", "Subscription grace period expired"
    if notification_type == "DID_CHANGE_RENEWAL_STATUS":
        if event.subtype == "AUTO_RENEW_DISABLED":
            return "🔕", "Auto-renew disabled"
        if event.subtype == "AUTO_RENEW_ENABLED":
            return "🔔", "Auto-renew enabled"
        return "🔁", "Renewal status changed"
    if notification_type == "DID_CHANGE_RENEWAL_PREF":
        return "🔁", "Renewal preference changed"
    return "ℹ️", "Apple Store event"


def _format_access_until(expires_date: int | None) -> str | None:
    if expires_date is None:
        return None
    return datetime.fromtimestamp(
        expires_date / 1000,
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M UTC")


def _renewal_status_lines(event: NormalizedAppleStoreEvent) -> list[str]:
    if event.notification_type != "DID_CHANGE_RENEWAL_STATUS":
        return []
    if event.subtype == "AUTO_RENEW_DISABLED":
        lines = []
        if event.storefront:
            lines.append(f"Storefront: {event.storefront}")
        lines.append("Auto-renew: OFF")
        access_until = _format_access_until(event.expires_date)
        if access_until is not None:
            lines.append(f"Access until: {access_until}")
        return lines
    if event.subtype == "AUTO_RENEW_ENABLED":
        lines = []
        if event.storefront:
            lines.append(f"Storefront: {event.storefront}")
        lines.append("Auto-renew: ON")
        return lines
    return []


def _format_price(price_milliunits: int | None, currency: str | None) -> str | None:
    if price_milliunits is None or not currency:
        return None
    amount = Decimal(price_milliunits) / Decimal(1000)
    return f"{amount:.2f} {currency.upper()}"


def _financial_detail_lines(event: NormalizedAppleStoreEvent) -> list[str]:
    if event.notification_type not in {
        "SUBSCRIBED",
        "DID_RENEW",
        "ONE_TIME_CHARGE",
        "REFUND",
        "REFUND_REVERSED",
    }:
        return []

    lines: list[str] = []
    if event.storefront:
        lines.append(f"Storefront: {event.storefront}")
    price = _format_price(event.price_milliunits, event.currency)
    if price is not None:
        lines.append(f"Price: {price}")
    if event.transaction_reason:
        lines.append(f"Transaction: {event.transaction_reason.capitalize()}")
    if event.offer_type:
        lines.append(f"Offer: {event.offer_type}")
    if event.offer_identifier:
        lines.append(f"Offer ID: {event.offer_identifier}")
    if event.offer_discount_type == "FREE_TRIAL":
        lines.append("Trial: Yes")
    elif event.offer_discount_type:
        lines.append(f"Offer discount: {event.offer_discount_type}")
    if event.offer_period:
        lines.append(f"Offer period: {event.offer_period}")
    if event.revocation_type:
        lines.append(f"Refund type: {event.revocation_type}")
    if event.revocation_type == "REFUND_PRORATED" and event.revocation_percentage is not None:
        lines.append(f"Refund percentage: {event.revocation_percentage}%")
    return lines


def format_sales_message(event: NormalizedAppleStoreEvent) -> str | None:
    """Return a short message only for a classified business event."""
    if not event.is_handled:
        return None

    icon, title = _event_title(event)
    app_name = "HalteWecker" if event.app == "haltewecker" else "Pasty"
    lines = [
        f"{icon} {app_name}",
        title,
    ]
    lines.extend(_renewal_status_lines(event))
    lines.extend(_financial_detail_lines(event))
    lines.append(f"Environment: {_environment_label(event.environment)}")
    if event.product_id:
        lines.append(f"Product: {event.product_id}")
    return "\n".join(lines)


def format_test_message(event: NormalizedAppleStoreEvent) -> str | None:
    """Return the opt-in Telegram text for a verified Apple TEST event."""
    if event.notification_type != "TEST":
        return None

    app_name = "HalteWecker" if event.app == "haltewecker" else "Pasty"
    return "\n".join(
        [
            "🧪 Apple Store TEST",
            app_name,
            f"Environment: {_environment_label(event.environment)}",
        ]
    )


def _notifications_enabled(raw_value: str | None) -> bool:
    return (raw_value or "").strip().casefold() in {"1", "true", "yes", "on"}


class TelegramSalesNotifier:
    """Send one outbound Telegram message using one configured chat."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        notify_sandbox: bool = False,
        notify_test: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Transport = _default_transport,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("Telegram token and chat ID are required")
        self.notify_sandbox = notify_sandbox
        self.notify_test = notify_test
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout
        self._transport = transport

    @classmethod
    def from_environment(cls) -> "TelegramSalesNotifier | None":
        token = os.environ.get("TELEGRAM_SALES_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_SALES_CHAT_ID", "").strip()
        if not token or not chat_id:
            return None
        return cls(
            token,
            chat_id,
            notify_sandbox=_notifications_enabled(
                os.environ.get("TELEGRAM_SALES_NOTIFY_SANDBOX")
            ),
            notify_test=_notifications_enabled(
                os.environ.get("TELEGRAM_SALES_NOTIFY_TEST")
            ),
        )

    def send(self, event: NormalizedAppleStoreEvent) -> None:
        message = format_sales_message(event)
        self._send_message(event, message)

    def send_test(self, event: NormalizedAppleStoreEvent) -> None:
        if not self.notify_test:
            return
        message = format_test_message(event)
        self._send_message(event, message)

    def send_report(self, message: str, *, environment: str = "Production") -> None:
        self._send_text(environment, message)

    def _send_message(
        self,
        event: NormalizedAppleStoreEvent,
        message: str | None,
    ) -> None:
        if message is None:
            return
        self._send_text(event.environment, message)

    def _send_text(self, environment: str, message: str) -> None:
        if environment.casefold() == "sandbox" and not self.notify_sandbox:
            return

        body = json.dumps(
            {"chat_id": self._chat_id, "text": message},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        status = self._transport(
            f"{TELEGRAM_API_URL}/bot{self._token}/sendMessage",
            body,
            {"Content-Type": "application/json"},
            self._timeout,
        )
        if status < 200 or status >= 300:
            raise TelegramSalesNotificationError(f"http_status={status}")
