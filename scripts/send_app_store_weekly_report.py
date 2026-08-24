#!/usr/bin/env python3
"""Build and send one weekly Apple Store operational report."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from apple_store_weekly_report import (  # noqa: E402
    DEFAULT_NOTIFICATION_STORE_PATH,
    build_weekly_summary,
    format_weekly_summary,
)
from telegram_sales_notifier import TelegramSalesNotifier  # noqa: E402


def main() -> None:
    summary = build_weekly_summary(
        os.environ.get(
            "APPLE_NOTIFICATION_STORE_PATH",
            DEFAULT_NOTIFICATION_STORE_PATH,
        ),
        environment=os.environ.get(
            "APPLE_STORE_WEEKLY_REPORT_ENVIRONMENT",
            "Production",
        ),
    )
    notifier = TelegramSalesNotifier.from_environment()
    if notifier is None:
        raise RuntimeError(
            "Telegram configuration is missing: "
            "TELEGRAM_SALES_BOT_TOKEN and TELEGRAM_SALES_CHAT_ID are required"
        )
    notifier.send_report(
        format_weekly_summary(summary),
        environment=summary.environment,
    )


if __name__ == "__main__":
    main()
