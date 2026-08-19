"""Persistent SQLite storage and idempotency for verified Apple notifications."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from apple_store_business_events import NormalizedAppleStoreEvent


class AppleStoreNotificationStoreError(RuntimeError):
    """Raised when the notification store cannot persist an event."""


class AppleStoreNotificationStore:
    def __init__(self, path: str | Path, timeout: float = 5.0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=timeout,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS apple_store_notification_events (
                    notification_uuid TEXT PRIMARY KEY,
                    notification_type TEXT,
                    subtype TEXT,
                    app TEXT NOT NULL,
                    bundle_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    product_id TEXT,
                    purchase_kind TEXT NOT NULL,
                    transaction_id TEXT,
                    original_transaction_id TEXT,
                    purchase_date INTEGER,
                    expires_date INTEGER,
                    revocation_date INTEGER,
                    auto_renew_status INTEGER,
                    signed_date INTEGER,
                    received_at INTEGER NOT NULL,
                    is_handled INTEGER NOT NULL
                );
                """
            )

    def insert_once(self, event: NormalizedAppleStoreEvent) -> bool:
        values = (
            event.notification_uuid,
            event.notification_type,
            event.subtype,
            event.app,
            event.bundle_id,
            event.environment,
            event.product_id,
            event.purchase_kind,
            event.transaction_id,
            event.original_transaction_id,
            event.purchase_date,
            event.expires_date,
            event.revocation_date,
            event.auto_renew_status,
            event.signed_date,
            event.received_at,
            int(event.is_handled),
        )
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                cursor = self._connection.execute(
                    """
                    INSERT INTO apple_store_notification_events (
                        notification_uuid, notification_type, subtype, app, bundle_id,
                        environment, product_id, purchase_kind, transaction_id,
                        original_transaction_id, purchase_date, expires_date,
                        revocation_date, auto_renew_status, signed_date, received_at,
                        is_handled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(notification_uuid) DO NOTHING
                    """,
                    values,
                )
                inserted = cursor.rowcount == 1
                self._connection.commit()
                return inserted
            except sqlite3.Error as error:
                try:
                    self._connection.rollback()
                except sqlite3.Error:
                    pass
                raise AppleStoreNotificationStoreError(
                    f"Unable to persist Apple notification: {error}"
                ) from error

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "AppleStoreNotificationStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
