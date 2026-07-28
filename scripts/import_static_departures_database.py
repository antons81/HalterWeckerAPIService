#!/usr/bin/env python3
"""Import German GTFS into an isolated, query-oriented SQLite snapshot."""

from __future__ import annotations

import argparse
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from build_german_departure_index import (
    DEFAULT_TIMEZONE, connect, load_gtfs_archive, populate_active_services,
    populate_gtfs, populate_stop_packages, resolve_canonical_stops,
    service_window, update_terminal_stops,
)


def validate(connection: sqlite3.Connection) -> None:
    required = ("raw_stops", "city_stops", "routes", "trips", "stop_times", "active_services")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(required).issubset(tables):
        raise ValueError("Static departures database is incomplete.")
    if connection.execute("SELECT COUNT(*) FROM city_stops").fetchone()[0] == 0:
        raise ValueError("Static departures database contains no city stop memberships.")
    if connection.execute("SELECT COUNT(*) FROM active_services").fetchone()[0] == 0:
        raise ValueError("Static departures database contains no active services.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs-url", required=True)
    parser.add_argument("--stop-data", required=True, help="Read-only current stop dataset")
    parser.add_argument("--next", required=True)
    parser.add_argument("--days", type=int, default=15)
    args = parser.parse_args()
    next_path = Path(args.next)
    next_path.parent.mkdir(parents=True, exist_ok=True)
    next_path.unlink(missing_ok=True)
    dates = service_window(DEFAULT_TIMEZONE, args.days)
    with load_gtfs_archive(args.gtfs_url) as archive:
        connection = connect(next_path)
        try:
            populate_stop_packages(connection, Path(args.stop_data))
            populate_gtfs(connection, archive)
            resolve_canonical_stops(connection)
            populate_active_services(connection, dates)
            update_terminal_stops(connection)
            connection.executescript("""
                CREATE INDEX stop_times_by_stop ON stop_times(raw_stop_id, trip_id, departure_seconds);
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            """)
            version = str(uuid.uuid4())
            connection.executemany("INSERT INTO metadata VALUES (?, ?)", (
                ("schemaVersion", "1"), ("databaseVersion", version),
                ("generatedAt", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
                ("validFrom", dates[0].isoformat()), ("validThrough", dates[-1].isoformat()),
                ("timezone", DEFAULT_TIMEZONE),
            ))
            validate(connection)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.commit()
        finally:
            connection.close()
    print(version)


if __name__ == "__main__":
    main()
