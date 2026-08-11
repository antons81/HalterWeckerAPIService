#!/usr/bin/env python3
"""Build city-scoped, static German GTFS departure indexes without loading stop_times into memory."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from build_stop_packages import load_gtfs_archive, normalized
from static_departures_ownership import ensure_ownership_schema, register_entities


SCHEMA_VERSION = 1
DEFAULT_TIMEZONE = "Europe/Berlin"
DEFAULT_PROVIDER_ID = "germany"
TRANSFER_KEY_SEPARATOR = "\x1f"


def internal_stop_id(native_stop_id: str, prefix: str = "") -> str:
    """Return the deterministic internal ID for a native GTFS stop ID."""
    value = native_stop_id.strip()
    if not value:
        return ""
    return value if not prefix or value.startswith(prefix) else f"{prefix}{value}"


def prefixed_optional_identifier(native_identifier: str, prefix: str) -> str:
    value = native_identifier.strip()
    return f"{prefix}{value}" if value else ""


def transfer_ownership_key(
    from_stop_id: str,
    to_stop_id: str,
    from_trip_id: str,
    to_trip_id: str,
    from_route_id: str,
    to_route_id: str,
) -> str:
    """Encode the GTFS transfer primary key for internal ownership metadata."""
    return TRANSFER_KEY_SEPARATOR.join(
        (
            from_stop_id,
            to_stop_id,
            from_trip_id,
            to_trip_id,
            from_route_id,
            to_route_id,
        )
    )


def gtfs_rows(archive: zipfile.ZipFile, name: str) -> Iterable[dict[str, str]]:
    if name not in archive.namelist():
        return []
    with archive.open(name) as raw:
        yield from csv.DictReader((line.decode("utf-8-sig") for line in raw))


def parse_gtfs_time(value: str) -> int | None:
    """Return seconds since the service-day start; GTFS permits hours beyond 24."""
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hour, minute, second = (int(part) for part in parts)
    except ValueError:
        return None
    if hour < 0 or not 0 <= minute < 60 or not 0 <= second < 60:
        return None
    return hour * 3_600 + minute * 60 + second


def generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def service_window(timezone_name: str, days: int) -> list[date]:
    if days < 1:
        raise ValueError("The service window must include at least one day.")
    today = datetime.now(ZoneInfo(timezone_name)).date()
    return [today + timedelta(days=offset) for offset in range(days)]


def load_city_aliases(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not all(
        isinstance(alias, str) and isinstance(city_id, str)
        for alias, city_id in payload.items()
    ):
        raise ValueError("City ID aliases must be a JSON object of string aliases to string IDs.")
    return payload


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        PRAGMA cache_size=-65536;

        CREATE TABLE raw_stops (
            stop_id TEXT PRIMARY KEY,
            parent_station TEXT NOT NULL,
            stop_name TEXT NOT NULL,
            platform_code TEXT NOT NULL,
            source_order INTEGER NOT NULL,
            canonical_stop_id TEXT
        ) WITHOUT ROWID;
        CREATE TABLE city_stops (
            city_id TEXT NOT NULL,
            stop_id TEXT NOT NULL,
            PRIMARY KEY (city_id, stop_id)
        ) WITHOUT ROWID;
        CREATE INDEX city_stops_by_stop ON city_stops(stop_id, city_id);
        CREATE TABLE routes (
            route_id TEXT PRIMARY KEY,
            short_name TEXT NOT NULL,
            long_name TEXT NOT NULL,
            route_type TEXT NOT NULL DEFAULT ''
        ) WITHOUT ROWID;
        CREATE TABLE trips (
            trip_id TEXT PRIMARY KEY,
            service_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            headsign TEXT NOT NULL,
            direction_id TEXT NOT NULL,
            terminal_stop_id TEXT NOT NULL DEFAULT ''
        ) WITHOUT ROWID;
        CREATE INDEX trips_by_service ON trips(service_id);
        CREATE TABLE calendar (
            service_id TEXT PRIMARY KEY,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            monday INTEGER NOT NULL,
            tuesday INTEGER NOT NULL,
            wednesday INTEGER NOT NULL,
            thursday INTEGER NOT NULL,
            friday INTEGER NOT NULL,
            saturday INTEGER NOT NULL,
            sunday INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE calendar_dates (
            service_id TEXT NOT NULL,
            service_date TEXT NOT NULL,
            exception_type INTEGER NOT NULL,
            PRIMARY KEY (service_id, service_date)
        ) WITHOUT ROWID;
        CREATE TABLE active_services (
            service_id TEXT NOT NULL,
            service_date TEXT NOT NULL,
            PRIMARY KEY (service_id, service_date)
        ) WITHOUT ROWID;
        CREATE TABLE stop_times (
            trip_id TEXT NOT NULL,
            raw_stop_id TEXT NOT NULL,
            departure_time TEXT NOT NULL,
            departure_seconds INTEGER NOT NULL,
            stop_sequence INTEGER NOT NULL
        );
        CREATE INDEX stop_times_by_trip ON stop_times(trip_id, stop_sequence);
        """
    )
    ensure_ownership_schema(connection)
    return connection


def populate_stop_packages(connection: sqlite3.Connection, output: Path) -> set[str]:
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cities = manifest.get("cities")
    if not isinstance(cities, list):
        raise ValueError("Stop manifest must contain a cities array.")

    city_ids: set[str] = set()
    for city in cities:
        if not isinstance(city, dict) or not isinstance(city.get("id"), str):
            raise ValueError("Stop manifest contains an invalid city entry.")
        city_id = city["id"]
        if city_id in city_ids:
            raise ValueError(f"Conflicting canonical city ID in stop manifest: {city_id}")
        city_ids.add(city_id)
        package_path = output / str(city.get("url", ""))
        if not package_path.is_file():
            raise ValueError(f"Missing stop package for {city_id}: {package_path}")
        package = json.loads(package_path.read_text(encoding="utf-8"))
        if not isinstance(package, list):
            raise ValueError(f"Invalid stop package for {city_id}")
        connection.executemany(
            "INSERT OR IGNORE INTO city_stops(city_id, stop_id) VALUES (?, ?)",
            ((city_id, str(stop["id"])) for stop in package if isinstance(stop, dict) and stop.get("id")),
        )
    connection.commit()
    return city_ids


def populate_gtfs(
    connection: sqlite3.Connection,
    archive: zipfile.ZipFile,
    *,
    identifier_prefix: str = "",
    stop_id_prefix: str = "",
    provider_id: str = DEFAULT_PROVIDER_ID,
) -> None:
    required = {"stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}
    missing = required - set(archive.namelist())
    if missing:
        raise ValueError(f"GTFS archive is missing required files: {', '.join(sorted(missing))}")

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS transfers (
            from_stop_id TEXT NOT NULL,
            to_stop_id TEXT NOT NULL,
            from_trip_id TEXT NOT NULL DEFAULT '',
            to_trip_id TEXT NOT NULL DEFAULT '',
            from_route_id TEXT NOT NULL DEFAULT '',
            to_route_id TEXT NOT NULL DEFAULT '',
            transfer_type INTEGER NOT NULL,
            min_transfer_time INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (
                from_stop_id, to_stop_id,
                from_trip_id, to_trip_id,
                from_route_id, to_route_id
            )
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS transfers_by_stop
            ON transfers(from_stop_id, to_stop_id);
        CREATE TABLE IF NOT EXISTS pathways (
            pathway_id TEXT PRIMARY KEY,
            from_stop_id TEXT NOT NULL,
            to_stop_id TEXT NOT NULL,
            pathway_mode TEXT NOT NULL,
            is_bidirectional INTEGER NOT NULL,
            length TEXT NOT NULL,
            traversal_time INTEGER NOT NULL,
            stair_count INTEGER NOT NULL,
            max_slope TEXT NOT NULL,
            min_width TEXT NOT NULL,
            signposted_as TEXT NOT NULL,
            reversed_signposted_as TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )
    transfer_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(transfers)")
    }
    required_transfer_columns = {
        "from_stop_id",
        "to_stop_id",
        "from_trip_id",
        "to_trip_id",
        "from_route_id",
        "to_route_id",
        "transfer_type",
        "min_transfer_time",
    }
    if not required_transfer_columns.issubset(transfer_columns):
        raise ValueError(
            "The existing transfers table has a legacy schema; "
            "a separate migration is required before importing transfers."
        )

    route_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(routes)")
    }
    if "route_type" not in route_columns:
        connection.execute(
            "ALTER TABLE routes ADD COLUMN route_type TEXT NOT NULL DEFAULT ''"
        )

    stop_ids: list[str] = []
    connection.executemany(
        "INSERT INTO raw_stops(stop_id, parent_station, stop_name, platform_code, source_order) VALUES (?, ?, ?, ?, ?)",
        (
            (
                internal_stop_id(row["stop_id"], stop_id_prefix),
                internal_stop_id(row.get("parent_station", ""), stop_id_prefix),
                row.get("stop_name", "").strip(),
                row.get("platform_code", "").strip(),
                index,
            )
            for index, row in enumerate(gtfs_rows(archive, "stops.txt"))
            if row.get("stop_id", "").strip()
        ),
    )
    stop_ids.extend(
        internal_stop_id(row["stop_id"], stop_id_prefix)
        for row in gtfs_rows(archive, "stops.txt")
        if row.get("stop_id", "").strip()
    )
    register_entities(
        connection,
        provider_id,
        "raw_stops",
        ((stop_id,) for stop_id in stop_ids),
    )

    route_ids: list[str] = []
    connection.executemany(
        "INSERT INTO routes(route_id, short_name, long_name, route_type) VALUES (?, ?, ?, ?)",
        (
            (
                identifier_prefix + row["route_id"].strip(),
                row.get("route_short_name", "").strip(),
                row.get("route_long_name", "").strip(),
                row.get("route_type", "").strip(),
            )
            for row in gtfs_rows(archive, "routes.txt") if row.get("route_id", "").strip()
        ),
    )
    route_ids.extend(
        identifier_prefix + row["route_id"].strip()
        for row in gtfs_rows(archive, "routes.txt")
        if row.get("route_id", "").strip()
    )
    register_entities(
        connection,
        provider_id,
        "routes",
        ((route_id,) for route_id in route_ids),
    )

    trip_ids: list[str] = []
    connection.executemany(
        "INSERT INTO trips(trip_id, service_id, route_id, headsign, direction_id) VALUES (?, ?, ?, ?, ?)",
        (
            (identifier_prefix + row["trip_id"].strip(), identifier_prefix + row.get("service_id", "").strip(), identifier_prefix + row.get("route_id", "").strip(), row.get("trip_headsign", "").strip(), row.get("direction_id", "").strip())
            for row in gtfs_rows(archive, "trips.txt")
            if row.get("trip_id", "").strip() and row.get("service_id", "").strip()
        ),
    )
    trip_ids.extend(
        identifier_prefix + row["trip_id"].strip()
        for row in gtfs_rows(archive, "trips.txt")
        if row.get("trip_id", "").strip() and row.get("service_id", "").strip()
    )
    register_entities(
        connection,
        provider_id,
        "trips",
        ((trip_id,) for trip_id in trip_ids),
    )
    calendar_ids: list[str] = []
    if "calendar.txt" in archive.namelist():
        connection.executemany(
            "INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ((identifier_prefix + row["service_id"].strip(), row.get("start_date", "").strip(), row.get("end_date", "").strip(), int(row.get("monday", "0") or 0), int(row.get("tuesday", "0") or 0), int(row.get("wednesday", "0") or 0), int(row.get("thursday", "0") or 0), int(row.get("friday", "0") or 0), int(row.get("saturday", "0") or 0), int(row.get("sunday", "0") or 0))
             for row in gtfs_rows(archive, "calendar.txt") if row.get("service_id", "").strip()),
        )
        calendar_ids.extend(
            identifier_prefix + row["service_id"].strip()
            for row in gtfs_rows(archive, "calendar.txt")
            if row.get("service_id", "").strip()
        )
        register_entities(
            connection,
            provider_id,
            "calendar",
            ((service_id,) for service_id in calendar_ids),
        )
    calendar_date_keys: list[tuple[str, str, str]] = []
    if "calendar_dates.txt" in archive.namelist():
        connection.executemany(
            "INSERT INTO calendar_dates VALUES (?, ?, ?)",
            ((identifier_prefix + row["service_id"].strip(), row.get("date", "").strip(), int(row.get("exception_type", "0") or 0))
             for row in gtfs_rows(archive, "calendar_dates.txt") if row.get("service_id", "").strip() and row.get("date", "").strip()),
        )
        calendar_date_keys.extend(
            (
                identifier_prefix + row["service_id"].strip(),
                row.get("date", "").strip(),
                row.get("exception_type", "0").strip(),
            )
            for row in gtfs_rows(archive, "calendar_dates.txt")
            if row.get("service_id", "").strip() and row.get("date", "").strip()
        )
        register_entities(
            connection,
            provider_id,
            "calendar_dates",
            calendar_date_keys,
        )

    batch: list[tuple[str, str, str, int, int]] = []
    for row in gtfs_rows(archive, "stop_times.txt"):
        trip_id = row.get("trip_id", "").strip()
        raw_stop_id = row.get("stop_id", "").strip()
        departure_time = row.get("departure_time", "").strip()
        departure_seconds = parse_gtfs_time(departure_time)
        if not trip_id or not raw_stop_id or departure_seconds is None:
            continue
        try:
            sequence = int(row.get("stop_sequence", "0") or 0)
        except ValueError:
            sequence = 0
        batch.append((identifier_prefix + trip_id, internal_stop_id(raw_stop_id, stop_id_prefix), departure_time, departure_seconds, sequence))
        if len(batch) == 20_000:
            connection.executemany("INSERT INTO stop_times VALUES (?, ?, ?, ?, ?)", batch)
            batch.clear()
    if batch:
        connection.executemany("INSERT INTO stop_times VALUES (?, ?, ?, ?, ?)", batch)

    if "transfers.txt" in archive.namelist():
        transfer_rows: list[tuple[str, str, str, str, str, str, int, int]] = []
        seen_transfers: dict[tuple[str, str, str, str, str, str], tuple[int, int]] = {}
        for row in gtfs_rows(archive, "transfers.txt"):
            native_from_stop_id = row.get("from_stop_id", "").strip()
            native_to_stop_id = row.get("to_stop_id", "").strip()
            if not native_from_stop_id or not native_to_stop_id:
                continue
            try:
                transfer_type = int(row.get("transfer_type", "0") or 0)
                min_transfer_time = int(row.get("min_transfer_time", "0") or 0)
            except ValueError as error:
                raise ValueError(
                    f"Invalid transfer numeric field for provider {provider_id}."
                ) from error
            transfer_key = (
                internal_stop_id(native_from_stop_id, stop_id_prefix),
                internal_stop_id(native_to_stop_id, stop_id_prefix),
                prefixed_optional_identifier(row.get("from_trip_id", ""), identifier_prefix),
                prefixed_optional_identifier(row.get("to_trip_id", ""), identifier_prefix),
                prefixed_optional_identifier(row.get("from_route_id", ""), identifier_prefix),
                prefixed_optional_identifier(row.get("to_route_id", ""), identifier_prefix),
            )
            semantic_values = (transfer_type, min_transfer_time)
            previous_values = seen_transfers.get(transfer_key)
            if previous_values is not None:
                if previous_values != semantic_values:
                    raise ValueError(
                        "Conflicting duplicate GTFS transfer rows for provider "
                        f"{provider_id} and transfer key {transfer_key!r}."
                    )
                continue
            seen_transfers[transfer_key] = semantic_values
            transfer_rows.append((*transfer_key, transfer_type, min_transfer_time))
        connection.executemany(
            """
            INSERT INTO transfers(
                from_stop_id, to_stop_id, from_trip_id, to_trip_id,
                from_route_id, to_route_id, transfer_type, min_transfer_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            transfer_rows,
        )
        register_entities(
            connection,
            provider_id,
            "transfers",
            (
                (
                    transfer_ownership_key(
                        from_stop_id,
                        to_stop_id,
                        from_trip_id,
                        to_trip_id,
                        from_route_id,
                        to_route_id,
                    ),
                )
                for (
                    from_stop_id,
                    to_stop_id,
                    from_trip_id,
                    to_trip_id,
                    from_route_id,
                    to_route_id,
                    _transfer_type,
                    _min_transfer_time,
                ) in transfer_rows
            ),
        )

    if "pathways.txt" in archive.namelist():
        pathway_keys: list[tuple[str, str, str]] = []
        connection.executemany(
            """
            INSERT INTO pathways(
                pathway_id, from_stop_id, to_stop_id, pathway_mode,
                is_bidirectional, length, traversal_time, stair_count,
                max_slope, min_width, signposted_as, reversed_signposted_as
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    internal_stop_id(
                        row.get("pathway_id", ""),
                        identifier_prefix or stop_id_prefix,
                    ),
                    internal_stop_id(row.get("from_stop_id", ""), stop_id_prefix),
                    internal_stop_id(row.get("to_stop_id", ""), stop_id_prefix),
                    row.get("pathway_mode", "").strip(),
                    int(row.get("is_bidirectional", "0") or 0),
                    row.get("length", "").strip(),
                    int(row.get("traversal_time", "0") or 0),
                    int(row.get("stair_count", "0") or 0),
                    row.get("max_slope", "").strip(),
                    row.get("min_width", "").strip(),
                    row.get("signposted_as", "").strip(),
                    row.get("reversed_signposted_as", "").strip(),
                )
                for row in gtfs_rows(archive, "pathways.txt")
                if row.get("pathway_id", "").strip()
                and row.get("from_stop_id", "").strip()
                and row.get("to_stop_id", "").strip()
            ),
        )
        pathway_keys.extend(
            (
                internal_stop_id(
                    row.get("pathway_id", ""),
                    identifier_prefix or stop_id_prefix,
                ),
                internal_stop_id(row.get("from_stop_id", ""), stop_id_prefix),
                internal_stop_id(row.get("to_stop_id", ""), stop_id_prefix),
            )
            for row in gtfs_rows(archive, "pathways.txt")
            if row.get("pathway_id", "").strip()
            and row.get("from_stop_id", "").strip()
            and row.get("to_stop_id", "").strip()
        )
        register_entities(connection, provider_id, "pathways", pathway_keys)
    connection.commit()


def _normalized_provider_ids(provider_ids: Iterable[str] | None) -> tuple[str, ...] | None:
    if provider_ids is None:
        return None
    return tuple(sorted({provider_id.strip() for provider_id in provider_ids if provider_id.strip()}))


def resolve_canonical_stops(
    connection: sqlite3.Connection,
    provider_ids: Iterable[str] | None = None,
) -> None:
    selected = _normalized_provider_ids(provider_ids)
    if selected is None:
        connection.execute("""
            UPDATE raw_stops SET canonical_stop_id = COALESCE(
                (SELECT parent.stop_id FROM raw_stops parent WHERE parent.stop_id = raw_stops.parent_station), raw_stops.stop_id
            )
        """)
    elif selected:
        placeholders = ",".join("?" for _ in selected)
        connection.execute(
            f"""
            UPDATE raw_stops SET canonical_stop_id = COALESCE(
                (SELECT parent.stop_id FROM raw_stops parent WHERE parent.stop_id = raw_stops.parent_station), raw_stops.stop_id
            )
            WHERE stop_id IN (
                SELECT key_1 FROM provider_entities
                WHERE entity_type = 'raw_stops' AND provider_id IN ({placeholders})
            )
            """,
            selected,
        )
    connection.commit()


def populate_active_services(
    connection: sqlite3.Connection,
    dates: list[date],
    provider_ids: Iterable[str] | None = None,
) -> None:
    day_columns = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    selected = _normalized_provider_ids(provider_ids)
    if selected == ():
        return
    service_placeholders = "" if selected is None else ",".join("?" for _ in selected)
    calendar_scope = "" if selected is None else f"""
              AND service_id IN (
                  SELECT key_1 FROM provider_entities
                  WHERE entity_type = 'calendar' AND provider_id IN ({service_placeholders})
              )
    """
    calendar_dates_scope = "" if selected is None else f"""
              AND service_id IN (
                  SELECT key_1 FROM provider_entities
                  WHERE entity_type = 'calendar_dates' AND provider_id IN ({service_placeholders})
              )
    """
    for service_date in dates:
        compact_date = service_date.strftime("%Y%m%d")
        day_column = day_columns[service_date.weekday()]
        connection.execute(f"""
            INSERT OR IGNORE INTO active_services(service_id, service_date)
            SELECT service_id, ? FROM calendar
            WHERE start_date <= ? AND end_date >= ? AND {day_column} = 1
              AND NOT EXISTS (SELECT 1 FROM calendar_dates overrides WHERE overrides.service_id = calendar.service_id AND overrides.service_date = ? AND overrides.exception_type = 2)
              {calendar_scope}
        """, (compact_date, compact_date, compact_date, compact_date) + (() if selected is None else selected))
        connection.execute(f"""
            INSERT OR IGNORE INTO active_services(service_id, service_date)
            SELECT service_id, service_date FROM calendar_dates
            WHERE service_date = ? AND exception_type = 1
              {calendar_dates_scope}
        """, (compact_date,) + (() if selected is None else selected))
    connection.commit()


def update_terminal_stops(
    connection: sqlite3.Connection,
    provider_ids: Iterable[str] | None = None,
) -> None:
    selected = _normalized_provider_ids(provider_ids)
    if selected is None:
        connection.execute("""
            UPDATE trips SET terminal_stop_id = COALESCE((
                SELECT stop_times.raw_stop_id FROM stop_times WHERE stop_times.trip_id = trips.trip_id
                ORDER BY stop_times.stop_sequence DESC, stop_times.rowid DESC LIMIT 1
            ), '')
        """)
    elif selected:
        placeholders = ",".join("?" for _ in selected)
        connection.execute(
            f"""
            UPDATE trips SET terminal_stop_id = COALESCE((
                SELECT stop_times.raw_stop_id FROM stop_times WHERE stop_times.trip_id = trips.trip_id
                ORDER BY stop_times.stop_sequence DESC, stop_times.rowid DESC LIMIT 1
            ), '')
            WHERE trip_id IN (
                SELECT key_1 FROM provider_entities
                WHERE entity_type = 'trips' AND provider_id IN ({placeholders})
            )
            """,
            selected,
        )
    connection.commit()


def write_city_indexes(connection: sqlite3.Connection, output: Path, city_ids: set[str], timezone_name: str, dates: list[date]) -> dict[str, dict[str, object]]:
    departures_directory = output / "departures"
    departures_directory.mkdir(parents=True, exist_ok=True)
    temporary_directory = departures_directory / ".german-build"
    shutil.rmtree(temporary_directory, ignore_errors=True)
    temporary_directory.mkdir()
    timestamp = generated_at()
    valid_from, valid_through = dates[0].isoformat(), dates[-1].isoformat()
    metadata: dict[str, dict[str, object]] = {}
    query = connection.execute("""
        SELECT city_stops.city_id, raw_stops.canonical_stop_id, active_services.service_date, stop_times.departure_time,
               trips.trip_id, trips.route_id, COALESCE(NULLIF(routes.short_name, ''), NULLIF(routes.long_name, ''), trips.route_id),
               COALESCE(NULLIF(trips.headsign, ''), NULLIF(destination_stops.stop_name, ''), 'Unbekanntes Ziel'), trips.direction_id, raw_stops.platform_code
        FROM stop_times JOIN trips ON trips.trip_id = stop_times.trip_id
        JOIN active_services ON active_services.service_id = trips.service_id
        JOIN raw_stops ON raw_stops.stop_id = stop_times.raw_stop_id
        JOIN city_stops ON city_stops.stop_id = raw_stops.canonical_stop_id
        LEFT JOIN routes ON routes.route_id = trips.route_id
        LEFT JOIN raw_stops AS destination_stops ON destination_stops.stop_id = trips.terminal_stop_id
        ORDER BY city_stops.city_id, raw_stops.canonical_stop_id, active_services.service_date, stop_times.departure_seconds, trips.trip_id, stop_times.stop_sequence
    """)
    current_city: str | None = None
    current_stop: str | None = None
    handle = None
    first_stop = True
    first_departure = True
    city_stop_count = 0
    city_departure_count = 0

    def open_city(city_id: str) -> None:
        nonlocal handle, first_stop, first_departure, city_stop_count, city_departure_count
        handle = (temporary_directory / f"{city_id}.json").open("w", encoding="utf-8")
        header = {"schemaVersion": SCHEMA_VERSION, "cityID": city_id, "timezone": timezone_name, "generatedAt": timestamp, "validFrom": valid_from, "validThrough": valid_through}
        handle.write(json.dumps(header, ensure_ascii=False, separators=(",", ":"))[:-1] + ',"stops":{')
        first_stop, first_departure, city_stop_count, city_departure_count = True, True, 0, 0

    def close_stop() -> None:
        nonlocal first_departure
        if current_stop is not None and handle is not None:
            handle.write("]")
            first_departure = True

    def close_city() -> None:
        nonlocal handle
        if current_city is None or handle is None:
            return
        close_stop()
        handle.write("}}")
        handle.close()
        path = temporary_directory / f"{current_city}.json"
        if city_departure_count:
            metadata[current_city] = {"cityID": current_city, "url": f"departures/{current_city}.json", "generatedAt": timestamp, "validFrom": valid_from, "validThrough": valid_through, "stopCount": city_stop_count, "departureCount": city_departure_count}
        else:
            path.unlink(missing_ok=True)
        handle = None

    for row in query:
        city_id, stop_id, service_date, departure_time, trip_id, route_id, line, destination, direction_id, platform = row
        if city_id != current_city:
            close_city()
            current_city, current_stop = city_id, None
            open_city(city_id)
        if stop_id != current_stop:
            close_stop()
            if not first_stop:
                handle.write(",")
            handle.write(json.dumps(stop_id, ensure_ascii=False) + ":[")
            first_stop, current_stop, city_stop_count = False, stop_id, city_stop_count + 1
        if not first_departure:
            handle.write(",")
        handle.write(json.dumps({"serviceDate": f"{service_date[:4]}-{service_date[4:6]}-{service_date[6:8]}", "departureTime": departure_time, "tripID": trip_id, "routeID": route_id, "line": line, "destination": destination, "directionID": direction_id or None, "platform": platform or None}, ensure_ascii=False, separators=(",", ":")))
        first_departure = False
        city_departure_count += 1
    close_city()
    for city_id in city_ids:
        old_path, new_path = departures_directory / f"{city_id}.json", temporary_directory / f"{city_id}.json"
        if city_id in metadata:
            new_path.replace(old_path)
        else:
            old_path.unlink(missing_ok=True)
    shutil.rmtree(temporary_directory, ignore_errors=True)
    return metadata


def validate_departure_output(output: Path, aliases: dict[str, str]) -> None:
    stop_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    city_ids = [city["id"] for city in stop_manifest["cities"]]
    city_id_set = set(city_ids)
    if len(city_ids) != len(city_id_set):
        raise ValueError("Stop manifest has conflicting canonical city IDs.")
    for alias, target in aliases.items():
        if alias in city_id_set and alias != target:
            raise ValueError(f"City ID alias conflicts with a canonical city ID: {alias}")
        if target not in city_id_set:
            raise ValueError(f"City ID alias target is absent from stop manifest: {alias} -> {target}")
    payload = json.loads((output / "departures-manifest.json").read_text(encoding="utf-8"))
    entries = payload.get("cities")
    if not isinstance(entries, list):
        raise ValueError("Departures manifest must contain a cities array.")
    seen: set[str] = set()
    stop_urls = {city["id"]: city["url"] for city in stop_manifest["cities"]}
    for entry in entries:
        city_id = entry.get("cityID") if isinstance(entry, dict) else None
        if not isinstance(city_id, str) or city_id in seen or city_id not in city_id_set:
            raise ValueError(f"Invalid or conflicting departure city ID: {city_id!r}")
        seen.add(city_id)
        file_path = output / str(entry.get("url", ""))
        if not file_path.is_file() or file_path.stat().st_size == 0:
            raise ValueError(f"Missing or empty departure file for {city_id}")
        index = json.loads(file_path.read_text(encoding="utf-8"))
        if index.get("cityID") != city_id or not isinstance(index.get("stops"), dict) or not index["stops"]:
            raise ValueError(f"Invalid departure index for {city_id}")
        package = json.loads((output / stop_urls[city_id]).read_text(encoding="utf-8"))
        stop_ids = {str(stop["id"]) for stop in package if isinstance(stop, dict) and stop.get("id")}
        if not set(index["stops"]).issubset(stop_ids):
            raise ValueError(f"Departure index contains stops outside package for {city_id}")
        actual_count = sum(len(items) for items in index["stops"].values() if isinstance(items, list))
        if actual_count == 0 or actual_count != entry.get("departureCount"):
            raise ValueError(f"Invalid departure count for {city_id}")


def build_german_departure_index(archive: zipfile.ZipFile, output: Path, timezone_name: str = DEFAULT_TIMEZONE, days: int = 15, aliases: dict[str, str] | None = None) -> dict[str, dict[str, object]]:
    aliases = aliases or {}
    dates = service_window(timezone_name, days)
    with tempfile.TemporaryDirectory(prefix="haltewecker-german-departures-") as temporary:
        connection = connect(Path(temporary) / "staging.sqlite")
        try:
            city_ids = populate_stop_packages(connection, output)
            populate_gtfs(connection, archive)
            resolve_canonical_stops(connection)
            populate_active_services(connection, dates)
            update_terminal_stops(connection)
            metadata = write_city_indexes(connection, output, city_ids, timezone_name, dates)
        finally:
            connection.close()
    manifest = {"schemaVersion": SCHEMA_VERSION, "generatedAt": generated_at(), "timezone": timezone_name, "validFrom": dates[0].isoformat(), "validThrough": dates[-1].isoformat(), "cityIDAliases": aliases, "cities": sorted(metadata.values(), key=lambda item: item["cityID"])}
    (output / "departures-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    validate_departure_output(output, aliases)
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--days", type=int, default=15)
    parser.add_argument("--city-id-aliases", default="config/city-id-aliases.json")
    args = parser.parse_args()
    with load_gtfs_archive(args.gtfs_url) as archive:
        metadata = build_german_departure_index(archive, Path(args.output), args.timezone, args.days, load_city_aliases(Path(args.city_id_aliases)))
    print(f"Built German departure indexes for {len(metadata)} cities.")


if __name__ == "__main__":
    main()
