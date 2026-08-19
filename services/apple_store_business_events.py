"""Classify verified Apple Store notifications into normalized business events."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from apple_store_notifications import VerifiedAppleNotification


DEFAULT_PRODUCT_CONFIG = "config/apple/store-notification-products.json"
UNKNOWN_APP = "unknown"
UNKNOWN_PURCHASE_KIND = "unknown"

HALTEWECKER_NOTIFICATION_TYPES = frozenset(
    {
        "SUBSCRIBED",
        "DID_RENEW",
        "DID_CHANGE_RENEWAL_STATUS",
        "DID_CHANGE_RENEWAL_PREF",
        "DID_FAIL_TO_RENEW",
        "EXPIRED",
        "GRACE_PERIOD_EXPIRED",
        "REFUND",
        "REFUND_REVERSED",
        "ONE_TIME_CHARGE",
    }
)
PASTY_NOTIFICATION_TYPES = frozenset(
    {
        "ONE_TIME_CHARGE",
        "REFUND",
        "REFUND_REVERSED",
    }
)


@dataclass(frozen=True)
class ProductClassification:
    app: str
    purchase_kind: str


@dataclass(frozen=True)
class NormalizedAppleStoreEvent:
    notification_uuid: str
    notification_type: str | None
    subtype: str | None
    app: str
    bundle_id: str
    environment: str
    product_id: str | None
    purchase_kind: str
    transaction_id: str | None
    original_transaction_id: str | None
    purchase_date: int | None
    expires_date: int | None
    revocation_date: int | None
    auto_renew_status: int | None
    signed_date: int | None
    received_at: int
    is_handled: bool


def _configured_path() -> Path:
    configured = os.environ.get("APPLE_STORE_NOTIFICATION_PRODUCT_CONFIG")
    if configured:
        return Path(configured)
    container_path = Path("/app") / DEFAULT_PRODUCT_CONFIG
    if container_path.exists():
        return container_path
    return Path(__file__).resolve().parents[1] / DEFAULT_PRODUCT_CONFIG


def _validate_mapping(payload: Any) -> Mapping[str, Mapping[str, ProductClassification]]:
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, dict):
        raise ValueError("Apple product mapping must contain a products object")

    result: dict[str, dict[str, ProductClassification]] = {}
    for bundle_id, app_products in products.items():
        if not isinstance(bundle_id, str) or not isinstance(app_products, dict):
            raise ValueError("Apple product mapping contains an invalid bundle entry")
        result[bundle_id] = {}
        for product_id, classification in app_products.items():
            if not isinstance(product_id, str) or not isinstance(classification, dict):
                raise ValueError("Apple product mapping contains an invalid product entry")
            app = classification.get("app")
            purchase_kind = classification.get("purchaseKind")
            if not isinstance(app, str) or not isinstance(purchase_kind, str):
                raise ValueError("Apple product mapping entries require app and purchaseKind")
            result[bundle_id][product_id] = ProductClassification(app, purchase_kind)
    return result


@lru_cache(maxsize=1)
def load_product_mapping(config_path: str | None = None) -> Mapping[str, Mapping[str, ProductClassification]]:
    path = Path(config_path) if config_path else _configured_path()
    return _validate_mapping(json.loads(path.read_text(encoding="utf-8")))


def classify_product(
    bundle_id: str,
    product_id: str | None,
    mapping: Mapping[str, Mapping[str, ProductClassification]] | None = None,
) -> ProductClassification:
    if mapping is None:
        mapping = load_product_mapping()
    app_products = mapping.get(bundle_id, {})
    known_app = next(iter(app_products.values()), None)
    if product_id is None:
        return ProductClassification(
            known_app.app if known_app else UNKNOWN_APP,
            UNKNOWN_PURCHASE_KIND,
        )
    classification = app_products.get(product_id)
    if classification:
        return classification
    return ProductClassification(
        known_app.app if known_app else UNKNOWN_APP,
        UNKNOWN_PURCHASE_KIND,
    )


def _raw_value(value: object, raw_name: str, enum_name: str) -> object:
    raw_value = getattr(value, raw_name, None)
    if raw_value is not None:
        return raw_value
    enum_value = getattr(value, enum_name, None)
    return getattr(enum_value, "value", enum_value)


def _product_id(notification: VerifiedAppleNotification) -> str | None:
    transaction = notification.transaction_info
    renewal = notification.renewal_info
    return (
        getattr(transaction, "productId", None)
        or getattr(renewal, "productId", None)
        or getattr(renewal, "autoRenewProductId", None)
    )


def normalize_notification(
    notification: VerifiedAppleNotification,
    received_at: int | None = None,
    mapping: Mapping[str, Mapping[str, ProductClassification]] | None = None,
) -> NormalizedAppleStoreEvent:
    if not notification.notification_uuid:
        raise ValueError("Verified Apple notification has no notificationUUID")

    transaction = notification.transaction_info
    renewal = notification.renewal_info
    product_id = _product_id(notification)
    classification = classify_product(notification.bundle_id, product_id, mapping)
    notification_type = notification.notification_type
    known_type = (
        notification_type in HALTEWECKER_NOTIFICATION_TYPES
        if classification.app == "haltewecker"
        else notification_type in PASTY_NOTIFICATION_TYPES
        if classification.app == "pasty"
        else False
    )
    is_handled = known_type and classification.purchase_kind != UNKNOWN_PURCHASE_KIND

    auto_renew_status = _raw_value(renewal, "rawAutoRenewStatus", "autoRenewStatus") if renewal else None
    if hasattr(auto_renew_status, "value"):
        auto_renew_status = auto_renew_status.value

    return NormalizedAppleStoreEvent(
        notification_uuid=notification.notification_uuid,
        notification_type=notification_type,
        subtype=notification.subtype,
        app=classification.app,
        bundle_id=notification.bundle_id,
        environment=notification.environment,
        product_id=product_id,
        purchase_kind=classification.purchase_kind,
        transaction_id=getattr(transaction, "transactionId", None),
        original_transaction_id=(
            getattr(transaction, "originalTransactionId", None)
            or getattr(renewal, "originalTransactionId", None)
        ),
        purchase_date=getattr(transaction, "purchaseDate", None),
        expires_date=getattr(transaction, "expiresDate", None),
        revocation_date=getattr(transaction, "revocationDate", None),
        auto_renew_status=auto_renew_status,
        signed_date=notification.signed_date,
        received_at=received_at if received_at is not None else time.time_ns() // 1_000_000,
        is_handled=is_handled,
    )
