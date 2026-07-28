#!/usr/bin/env python3
"""Import German GTFS into an isolated, query-oriented SQLite snapshot."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from build_german_departure_index import (
    DEFAULT_TIMEZONE, connect, load_gtfs_archive, populate_active_services,
    populate_gtfs, resolve_canonical_stops, service_window,
    update_terminal_stops,
)
from build_stop_packages import (
    load_cities, nl_city_ids,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def configured_external_city_ids(cities_path: Path, swiss_cities_path: Path) -> set[str]:
    excluded = nl_city_ids(load_cities(cities_path))
    if swiss_cities_path.exists():
        excluded.update(str(city["id"]) for city in load_cities(swiss_cities_path))
    return excluded


def available_canonical_stop_ids(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT canonical_stop_id
            FROM raw_stops
            WHERE canonical_stop_id IS NOT NULL AND canonical_stop_id <> ''
            """
        )
    }


def populate_german_city_memberships(
    connection: sqlite3.Connection,
    stop_data: Path,
    excluded_city_ids: set[str]
) -> set[str]:
    manifest_path = stop_data / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cities = manifest.get("cities")
    if not isinstance(cities, list):
        raise ValueError("Stop manifest must contain a cities array.")

    available_stop_ids = available_canonical_stop_ids(connection)
    city_ids: set[str] = set()
    for city in cities:
        if not isinstance(city, dict) or not isinstance(city.get("id"), str):
            raise ValueError("Stop manifest contains an invalid city entry.")
        city_id = city["id"]
        if city_id in excluded_city_ids:
            continue
        if city_id in city_ids:
            raise ValueError(f"Conflicting canonical German city ID in stop manifest: {city_id}")
        package_path = stop_data / str(city.get("url", ""))
        if not package_path.is_file():
            raise ValueError(f"Missing stop package for {city_id}: {package_path}")
        package = json.loads(package_path.read_text(encoding="utf-8"))
        if not isinstance(package, list):
            raise ValueError(f"Invalid stop package for {city_id}")
        stop_ids = sorted(
            {
                str(stop["id"])
                for stop in package
                if isinstance(stop, dict) and stop.get("id") and str(stop["id"]) in available_stop_ids
            }
        )
        if not stop_ids:
            continue
        connection.executemany(
            "INSERT OR IGNORE INTO city_stops(city_id, stop_id) VALUES (?, ?)",
            ((city_id, stop_id) for stop_id in stop_ids),
        )
        city_ids.add(city_id)
    connection.commit()
    return city_ids


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
    parser.add_argument("--cities", default=str(REPOSITORY_ROOT / "config" / "cities.json"))
    parser.add_argument("--swiss-cities", default=str(REPOSITORY_ROOT / "config" / "swiss-cities.json"))
    args = parser.parse_args()
    next_path = Path(args.next)
    next_path.parent.mkdir(parents=True, exist_ok=True)
    next_path.unlink(missing_ok=True)
    dates = service_window(DEFAULT_TIMEZONE, args.days)
    with load_gtfs_archive(args.gtfs_url) as archive:
        connection = connect(next_path)
        try:
            populate_gtfs(connection, archive)
            resolve_canonical_stops(connection)
            city_ids = populate_german_city_memberships(
                connection,
                Path(args.stop_data),
                configured_external_city_ids(Path(args.cities), Path(args.swiss_cities))
            )
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
            connection.commit()
        finally:
            connection.close()
    print(json.dumps({"databaseVersion": version, "cityCount": len(city_ids)}, separators=(",", ":")))


if __name__ == "__main__":
    main()
