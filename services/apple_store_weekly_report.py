"""Read-only operational analytics for persisted Apple Store events."""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


WEEKLY_REPORT_TIMEZONE = ZoneInfo("Europe/Berlin")
DEFAULT_NOTIFICATION_STORE_PATH = "/data/apple-store-notifications/events.sqlite3"
DEFAULT_REPORT_ENVIRONMENT = "Production"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeeklySalesSummary:
    start_at: datetime
    end_at: datetime
    environment: str
    new_subscriptions: int
    renewals: int
    lifetime_purchases: int
    refunds: int
    app_counts: dict[str, int]
    storefront_counts: dict[str, int]
    revenue_by_currency: dict[str, Decimal]


def normalize_environment(environment: str) -> str:
    normalized = environment.strip().casefold()
    if normalized == "production":
        return "Production"
    if normalized == "sandbox":
        return "Sandbox"
    raise ValueError(f"Unsupported Apple report environment: {environment}")


def _timestamp_ms(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("Weekly report datetimes must be timezone-aware")
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _category(notification_type: str | None) -> str | None:
    return {
        "SUBSCRIBED": "new_subscription",
        "DID_RENEW": "renewal",
        "ONE_TIME_CHARGE": "lifetime",
        "REFUND": "refund",
        "REFUND_REVERSED": "refund_reversed",
    }.get(notification_type)


def _deduplication_key(row: sqlite3.Row, category: str) -> tuple[str, str]:
    transaction_id = row["transaction_id"]
    identifier = (
        f"transaction:{transaction_id}"
        if transaction_id
        else f"notification:{row['notification_uuid']}"
    )
    return category, identifier


def _amount(row: sqlite3.Row) -> Decimal | None:
    price = row["price_milliunits"]
    currency = row["currency"]
    if price is None or not currency:
        return None
    return Decimal(price) / Decimal(1000)


def _refund_amount(row: sqlite3.Row) -> Decimal | None:
    amount = _amount(row)
    if amount is None:
        return None
    if row["revocation_type"] != "REFUND_PRORATED":
        return amount
    percentage = row["revocation_percentage"]
    if percentage is None or percentage < 0 or percentage > 100000:
        return None
    return amount * Decimal(percentage) / Decimal(100000)


def build_weekly_summary(
    path: str | Path = DEFAULT_NOTIFICATION_STORE_PATH,
    *,
    end_at: datetime | None = None,
    environment: str = DEFAULT_REPORT_ENVIRONMENT,
) -> WeeklySalesSummary:
    report_environment = normalize_environment(environment)
    end_local = (end_at or datetime.now(WEEKLY_REPORT_TIMEZONE)).astimezone(
        WEEKLY_REPORT_TIMEZONE
    )
    start_local = end_local - timedelta(days=7)

    database_uri = f"file:{Path(path).absolute()}?mode=ro"
    app_counts: Counter[str] = Counter()
    storefront_counts: Counter[str] = Counter()
    revenue_by_currency: defaultdict[str, Decimal] = defaultdict(Decimal)
    counts = Counter()
    seen: set[tuple[str, str]] = set()

    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
                   SELECT notification_uuid, notification_type, app, environment,
                   transaction_id, storefront, price_milliunits, currency,
                   revocation_type, revocation_percentage, received_at
            FROM apple_store_notification_events
            WHERE is_handled = 1
              AND environment = ?
              AND received_at >= ?
              AND received_at < ?
            ORDER BY received_at ASC, notification_uuid ASC
            """,
            (
                report_environment,
                _timestamp_ms(start_local),
                _timestamp_ms(end_local),
            ),
        ).fetchall()
        reversal_transaction_ids = {
            row["transaction_id"]
            for row in rows
            if row["notification_type"] == "REFUND_REVERSED"
            and row["transaction_id"]
        }
        refund_history_rows: list[sqlite3.Row] = []
        if reversal_transaction_ids:
            placeholders = ", ".join("?" for _ in reversal_transaction_ids)
            refund_history_rows = connection.execute(
                f"""
                SELECT notification_uuid, notification_type, transaction_id,
                       price_milliunits, currency, revocation_type,
                       revocation_percentage, received_at
                FROM apple_store_notification_events
                WHERE is_handled = 1
                  AND environment = ?
                  AND notification_type = 'REFUND'
                  AND transaction_id IN ({placeholders})
                ORDER BY received_at ASC, notification_uuid ASC
                """,
                (report_environment, *sorted(reversal_transaction_ids)),
            ).fetchall()

    refund_history: defaultdict[str, list[tuple[int, str, Decimal]]] = defaultdict(list)
    for row in refund_history_rows:
        transaction_id = row["transaction_id"]
        currency = row["currency"]
        amount = _refund_amount(row)
        if not transaction_id or not currency or amount is None:
            continue
        refund_history[transaction_id].append(
            (row["received_at"], currency.upper(), amount)
        )

    for row in rows:
        category = _category(row["notification_type"])
        if category is None:
            continue
        key = _deduplication_key(row, category)
        if key in seen:
            continue
        seen.add(key)

        if category == "refund_reversed":
            transaction_id = row["transaction_id"]
            candidates = refund_history.get(transaction_id or "", [])
            previous_refunds = [
                candidate
                for candidate in candidates
                if candidate[0] <= row["received_at"]
            ]
            if not previous_refunds:
                LOGGER.warning(
                    "event=apple_store_refund_reversal_unresolved "
                    "notificationUUID=%s transactionId=%s",
                    row["notification_uuid"],
                    transaction_id,
                )
                continue
            _, currency, adjustment = previous_refunds[-1]
            revenue_by_currency[currency] += adjustment
            continue

        amount = _amount(row)
        currency = row["currency"]
        if currency and amount is not None:
            currency = currency.upper()
            if category == "refund":
                adjustment = _refund_amount(row)
                if adjustment is not None:
                    revenue_by_currency[currency] -= adjustment
            else:
                revenue_by_currency[currency] += amount

        if category == "refund":
            counts[category] += 1
            continue

        counts[category] += 1
        app_counts[row["app"]] += 1
        if row["storefront"]:
            storefront_counts[row["storefront"]] += 1

    return WeeklySalesSummary(
        start_at=start_local,
        end_at=end_local,
        environment=report_environment,
        new_subscriptions=counts["new_subscription"],
        renewals=counts["renewal"],
        lifetime_purchases=counts["lifetime"],
        refunds=counts["refund"],
        app_counts=dict(app_counts),
        storefront_counts=dict(storefront_counts),
        revenue_by_currency=dict(revenue_by_currency),
    )


def _format_amount(amount: Decimal) -> str:
    return f"{amount:.2f}"


def format_weekly_summary(summary: WeeklySalesSummary) -> str:
    date_range = (
        f"{summary.start_at:%Y-%m-%d %H:%M} – "
        f"{summary.end_at:%Y-%m-%d %H:%M} Europe/Berlin"
    )
    storefront_lines = [
        f"{storefront} — {count}"
        for storefront, count in sorted(
            summary.storefront_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:5]
    ] or ["— 0"]
    revenue_lines = [
        f"{currency} — {_format_amount(amount)}"
        for currency, amount in sorted(summary.revenue_by_currency.items())
    ] or ["— 0.00"]

    return "\n".join(
        [
            "📊 Apple Sales Weekly Summary",
            date_range,
            f"Environment: {summary.environment}",
            "",
            f"New subscriptions: {summary.new_subscriptions}",
            f"Renewals: {summary.renewals}",
            f"Lifetime purchases: {summary.lifetime_purchases}",
            f"Refunds: {summary.refunds}",
            "",
            f"HalteWecker: {summary.app_counts.get('haltewecker', 0)}",
            f"Pasty: {summary.app_counts.get('pasty', 0)}",
            "",
            "Top storefronts:",
            *storefront_lines,
            "",
            "Revenue:",
            *revenue_lines,
        ]
    )
