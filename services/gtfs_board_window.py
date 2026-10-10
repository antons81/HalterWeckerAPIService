"""Indexed board predicates using absolute GTFS service-day time.

Created by Anton on 2026-10-10.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

BOARD_EPOCH_SQL = "(gtfs_board_epoch(a.service_date) + s.departure_seconds)"


@lru_cache(maxsize=4096)
def service_day_epoch(service_date: str, timezone_name: str) -> int:
    """GTFS time starts twelve elapsed hours before local service-day noon."""
    day = datetime.strptime(service_date.replace("-", ""), "%Y%m%d").date()
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(timezone_name))
    return int(noon.timestamp()) - 12 * 3600


def boundary_epoch(value: datetime, timezone_name: str) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(timezone_name))
    return value.timestamp()


def departure_instant(service_date: object, departure_time: object, timezone_name: str) -> datetime:
    hour, minute, second = (int(part) for part in str(departure_time).split(":"))
    if hour < 0 or minute not in range(60) or second not in range(60):
        raise ValueError("invalid GTFS departure time")
    epoch = service_day_epoch(str(service_date), timezone_name) + hour * 3600 + minute * 60 + second
    return datetime.fromtimestamp(epoch, ZoneInfo(timezone_name))


def board_predicate(connection: sqlite3.Connection, timezone_name: str,
                    from_date: datetime | None, to_date: datetime | None) -> tuple[str, tuple[float, ...]]:
    """Filter before LIMIT while retaining indexed stop and service joins.

    Do not discard old service dates: departures beyond 24:00, including
    multi-day trips, can still lie inside the requested absolute interval.
    The bounded cache contains only immutable date/timezone epoch values.
    """
    connection.create_function(
        "gtfs_board_epoch", 1,
        lambda service_date: service_day_epoch(str(service_date), timezone_name),
        deterministic=True,
    )
    clauses, parameters = [], []
    if from_date is not None:
        clauses.append(f"{BOARD_EPOCH_SQL} >= ?")
        parameters.append(boundary_epoch(from_date, timezone_name))
    if to_date is not None:
        clauses.append(f"{BOARD_EPOCH_SQL} <= ?")
        parameters.append(boundary_epoch(to_date, timezone_name))
    return " AND ".join(clauses) or "1", tuple(parameters)
