#!/usr/bin/env python3
"""Standalone HTTP service for verified App Store Server Notifications."""

from __future__ import annotations

import json
import logging
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from apple_store_business_events import normalize_notification
from apple_store_notification_store import (
    AppleStoreNotificationStore,
    AppleStoreNotificationStoreError,
)
from apple_store_notifications import AppleStoreNotificationVerificationError, default_verifier
from telegram_sales_notifier import TelegramSalesNotificationError, TelegramSalesNotifier


DEFAULT_NOTIFICATION_STORE_PATH = "/data/apple-store-notifications/events.sqlite3"
MAX_REQUEST_BODY_BYTES = 1_000_000
LOGGER = logging.getLogger("haltewecker.app_store_notifications_api")


class AppStoreNotificationsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    apple_store_notification_verifier = None
    apple_store_notification_store: AppleStoreNotificationStore | None = None
    telegram_sales_notifier: TelegramSalesNotifier | None = None

    def version_string(self) -> str:
        return "HalteWecker App Store Notifications"

    def send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health":
            self.send_json(HTTPStatus.OK, {"ok": True})
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/appstore/notifications":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BODY_BYTES:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request body"})
                return
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
            signed_payload = payload.get("signedPayload") if isinstance(payload, dict) else None
            if not isinstance(signed_payload, str) or not signed_payload:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "signedPayload is required"})
                return
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, OSError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request body"})
            return

        try:
            verifier = self.apple_store_notification_verifier or default_verifier()
            verified_notification = verifier.verify(signed_payload)
        except AppleStoreNotificationVerificationError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid signedPayload"})
            return

        try:
            event = normalize_notification(verified_notification)
            store = self.apple_store_notification_store
            if store is None:
                raise AppleStoreNotificationStoreError("Apple notification store is not configured")
            inserted = store.insert_once(event)
        except (AppleStoreNotificationStoreError, ValueError):
            LOGGER.exception(
                "event=apple_store_notification_persistence_failed notificationUUID=%s",
                verified_notification.notification_uuid,
            )
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "notification persistence unavailable"},
            )
            return

        if not inserted:
            LOGGER.info(
                "event=apple_store_notification_duplicate notificationUUID=%s",
                event.notification_uuid,
            )
            self.send_json(HTTPStatus.OK, {"ok": True})
            return

        notifier = self.telegram_sales_notifier
        if event.is_handled and notifier is not None:
            try:
                notifier.send(event)
            except TelegramSalesNotificationError as error:
                LOGGER.warning(
                    "event=telegram_sales_notification_failed app=%s "
                    "notificationType=%s reason=%s",
                    event.app,
                    event.notification_type,
                    error.reason,
                )
            except Exception as error:
                LOGGER.warning(
                    "event=telegram_sales_notification_failed app=%s "
                    "notificationType=%s reason=%s",
                    event.app,
                    event.notification_type,
                    type(error).__name__,
                )
            else:
                if not (
                    event.environment.casefold() == "sandbox"
                    and not notifier.notify_sandbox
                ):
                    LOGGER.info(
                        "event=telegram_sales_notification_sent app=%s "
                        "notificationType=%s",
                        event.app,
                        event.notification_type,
                    )

        if event.is_handled:
            LOGGER.info(
                "event=apple_store_business_event app=%s notificationType=%s "
                "purchaseKind=%s productId=%s environment=%s notificationUUID=%s",
                event.app,
                event.notification_type,
                event.purchase_kind,
                event.product_id,
                event.environment,
                event.notification_uuid,
            )
        else:
            LOGGER.info(
                "event=apple_store_notification_unhandled app=%s notificationType=%s "
                "purchaseKind=%s productId=%s environment=%s notificationUUID=%s",
                event.app,
                event.notification_type,
                event.purchase_kind,
                event.product_id,
                event.environment,
                event.notification_uuid,
            )
        self.send_json(HTTPStatus.OK, {"ok": True})


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    store = AppleStoreNotificationStore(
        os.environ.get("APPLE_NOTIFICATION_STORE_PATH", DEFAULT_NOTIFICATION_STORE_PATH)
    )
    Handler.apple_store_notification_store = store
    Handler.telegram_sales_notifier = TelegramSalesNotifier.from_environment()
    server = AppStoreNotificationsHTTPServer(
        ("0.0.0.0", int(os.environ.get("PORT", "8080"))),
        Handler,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
