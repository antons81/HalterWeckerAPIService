"""Disk-backed staging helpers for large external GTFS merge operations."""

from __future__ import annotations

import json
import resource
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from .gtfs_csv import normalized_dict_reader
except ImportError:
    from gtfs_csv import normalized_dict_reader


def legacy_int_or_none(value: object, default: int = 0) -> int | None:
    """Match the legacy int(value or default) conversion exactly."""
    try:
        return int(value or str(default))
    except (TypeError, ValueError):
        return None


class JSONStream:
    """Incrementally decode generated JSON without reading the whole file."""

    def __init__(self, path: Path, chunk_size: int = 1024 * 1024) -> None:
        self._file = path.open("r", encoding="utf-8")
        self._decoder = json.JSONDecoder()
        self._buffer = ""
        self._eof = False
        self._chunk_size = chunk_size

    def close(self) -> None:
        self._file.close()

    def _fill(self) -> bool:
        if self._eof:
            return False
        chunk = self._file.read(self._chunk_size)
        if chunk:
            self._buffer += chunk
            return True
        self._eof = True
        return False

    def _skip_whitespace(self) -> None:
        while True:
            stripped = self._buffer.lstrip()
            if stripped:
                self._buffer = stripped
                return
            if not self._fill():
                return

    def _consume(self, expected: str) -> None:
        self._skip_whitespace()
        while not self._buffer.startswith(expected):
            if not self._fill():
                raise ValueError(f"Expected JSON token {expected!r}")
        self._buffer = self._buffer[len(expected):]

    def value(self) -> object:
        self._skip_whitespace()
        while True:
            try:
                value, end = self._decoder.raw_decode(self._buffer)
            except json.JSONDecodeError:
                if not self._fill():
                    raise
                continue
            self._buffer = self._buffer[end:]
            return value

    def string(self) -> str:
        value = self.value()
        if not isinstance(value, str):
            raise ValueError("Expected JSON string")
        return value

    def object_items(self) -> Iterator[tuple[str, object]]:
        self._consume("{")
        self._skip_whitespace()
        if self._buffer.startswith("}"):
            self._buffer = self._buffer[1:]
            return
        while True:
            key = self.string()
            self._consume(":")
            yield key, self.value()
            self._skip_whitespace()
            if self._buffer.startswith("}"):
                self._buffer = self._buffer[1:]
                return
            self._consume(",")

    def array_items(self) -> Iterator[object]:
        self._consume("[")
        self._skip_whitespace()
        if self._buffer.startswith("]"):
            self._buffer = self._buffer[1:]
            return
        while True:
            yield self.value()
            self._skip_whitespace()
            if self._buffer.startswith("]"):
                self._buffer = self._buffer[1:]
                return
            self._consume(",")


def iter_json_array(path: Path) -> Iterator[object]:
    stream = JSONStream(path)
    try:
        yield from stream.array_items()
    finally:
        stream.close()


def iter_json_object(path: Path) -> Iterator[tuple[str, object]]:
    stream = JSONStream(path)
    try:
        yield from stream.object_items()
    finally:
        stream.close()


def iter_departure_payload(path: Path) -> Iterator[tuple[str, str, object]]:
    """Yield generated metadata, one stop departure list, or one platform list."""
    stream = JSONStream(path)
    try:
        stream._consume("{")
        stream._skip_whitespace()
        if stream._buffer.startswith("}"):
            stream._buffer = stream._buffer[1:]
            return
        while True:
            key = stream.string()
            stream._consume(":")
            if key in {"generatedAt", "timezone"}:
                value = stream.value()
                yield key, "", value
            elif key == "stops":
                for stop_id, items in stream.object_items():
                    yield "stop", stop_id, items
            elif key == "platforms":
                for parent_id, children in stream.object_items():
                    yield "platform", parent_id, children
            else:
                stream.value()
            stream._skip_whitespace()
            if stream._buffer.startswith("}"):
                stream._buffer = stream._buffer[1:]
                return
            stream._consume(",")
    finally:
        stream.close()


def iter_departure_payload_items(
    path: Path,
    *,
    strict: bool = False,
    on_section: Callable[[str], None] | None = None,
) -> Iterator[tuple[str, str, object]]:
    """Yield generated metadata and individual departure/platform items."""
    stream = JSONStream(path)
    try:
        stream._consume("{")
        stream._skip_whitespace()
        if stream._buffer.startswith("}"):
            stream._buffer = stream._buffer[1:]
            return
        while True:
            key = stream.string()
            stream._consume(":")
            if key in {"generatedAt", "timezone"}:
                yield key, "", stream.value()
            elif key in {"stops", "platforms"}:
                if on_section is not None:
                    on_section(key)
                stream._consume("{")
                stream._skip_whitespace()
                if stream._buffer.startswith("}"):
                    stream._buffer = stream._buffer[1:]
                else:
                    while True:
                        item_key = stream.string()
                        stream._consume(":")
                        stream._skip_whitespace()
                        if stream._buffer.startswith("["):
                            for item in stream.array_items():
                                if strict and (
                                    (key == "stops" and not isinstance(item, dict))
                                    or (key == "platforms" and not isinstance(item, str))
                                ):
                                    raise ValueError(
                                        f"Invalid departure payload item in {key}"
                                    )
                                yield (
                                    "stop" if key == "stops" else "platform",
                                    item_key,
                                    item,
                                )
                        else:
                            if strict:
                                raise ValueError(
                                    f"Departure payload entry in {key} is not an array"
                                )
                            stream.value()
                        stream._skip_whitespace()
                        if stream._buffer.startswith("}"):
                            stream._buffer = stream._buffer[1:]
                            break
                        stream._consume(",")
            else:
                stream.value()
            stream._skip_whitespace()
            if stream._buffer.startswith("}"):
                stream._buffer = stream._buffer[1:]
                return
            stream._consume(",")
    finally:
        stream.close()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rss_kib() -> int:
    """Return current RSS in KiB on Linux, or the process peak elsewhere."""
    try:
        with Path("/proc/self/status").open(encoding="ascii") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return _peak_rss_kib()


def _peak_rss_kib() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value / 1024) if sys.platform == "darwin" else int(value)


def log_memory_stage(
    stage: str,
    *,
    source: str | None = None,
    started: float | None = None,
    status: str = "completed",
) -> None:
    fields = ["[ExternalGTFS]"]
    if source:
        fields.append(f"source={source}")
    fields.append(f"stage={stage}")
    fields.append(f"status={status}")
    if started is not None:
        fields.append(f"duration={time.monotonic() - started:.4f}s")
    fields.append(f"rss_kib={_rss_kib()}")
    fields.append(f"peak_rss_kib={_peak_rss_kib()}")
    print(" ".join(fields), flush=True)


class NormalizedProviderContext:
    """Disk-backed normalized GTFS tables shared by one provider build."""

    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="haltewecker-external-normalized-"
        )
        self.connection = sqlite3.connect(
            Path(self._temporary.name) / "normalized.sqlite"
        )
        self.connection.executescript(
            """
            CREATE TABLE routes (
                route_id TEXT NOT NULL,
                route_short_name TEXT NOT NULL,
                route_long_name TEXT NOT NULL,
                route_type TEXT NOT NULL,
                agency_id TEXT NOT NULL
            );
            CREATE TABLE stops (
                stop_id TEXT NOT NULL,
                stop_name TEXT NOT NULL,
                stop_lat TEXT NOT NULL,
                stop_lon TEXT NOT NULL,
                stop_code TEXT NOT NULL,
                parent_station TEXT NOT NULL,
                location_type INTEGER NOT NULL,
                platform_code TEXT NOT NULL
            );
            CREATE TABLE trips (
                trip_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                service_id TEXT NOT NULL,
                trip_headsign TEXT NOT NULL,
                direction_id TEXT NOT NULL
            );
            CREATE TABLE stop_times (
                trip_id TEXT NOT NULL,
                stop_id TEXT NOT NULL,
                arrival_time TEXT NOT NULL,
                departure_time TEXT NOT NULL,
                arrival_seconds INTEGER,
                departure_seconds INTEGER,
                stop_sequence INTEGER NOT NULL
            );
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
            """
        )

    @classmethod
    def from_archive(cls, archive) -> "NormalizedProviderContext":
        context = cls()
        try:
            context._populate(archive)
        except Exception:
            context.close()
            raise
        return context

    def close(self) -> None:
        self.connection.close()
        self._temporary.cleanup()

    def _populate(self, archive) -> None:
        try:
            from .build_german_departure_index import parse_gtfs_time
        except ImportError:
            from build_german_departure_index import parse_gtfs_time

        self.connection.executemany(
            "INSERT INTO routes VALUES (?, ?, ?, ?, ?)",
            (
                (
                    str(row.get("route_id", "")),
                    str(row.get("route_short_name", "")),
                    str(row.get("route_long_name", "")),
                    str(row.get("route_type", "3")),
                    str(row.get("agency_id", "")),
                )
                for row in _iter_table(archive, "routes.txt")
                if row.get("route_id")
            ),
        )
        self.connection.commit()

        self.connection.executemany(
            "INSERT INTO stops VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    str(row.get("stop_id", "")),
                    str(row.get("stop_name", "") or ""),
                    str(row.get("stop_lat", "") or ""),
                    str(row.get("stop_lon", "") or ""),
                    str(row.get("stop_code", "") or ""),
                    str(row.get("parent_station", "") or ""),
                    int(str(row.get("location_type", "0") or "0"))
                    if str(row.get("location_type", "0") or "0").strip().isdigit()
                    else 0,
                    str(row.get("platform_code", "") or ""),
                )
                for row in _iter_table(archive, "stops.txt")
                if row.get("stop_id")
            ),
        )
        self.connection.commit()

        self.connection.executemany(
            "INSERT INTO trips VALUES (?, ?, ?, ?, ?)",
            (
                (
                    str(row.get("trip_id", "")),
                    str(row.get("route_id", "")),
                    str(row.get("service_id", "")),
                    str(row.get("trip_headsign", "") or ""),
                    str(row.get("direction_id", "0")),
                )
                for row in _iter_table(archive, "trips.txt")
                if str(row.get("trip_id", ""))
            ),
        )
        self.connection.commit()

        self.connection.executemany(
            "INSERT INTO stop_times VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    trip_id,
                    stop_id,
                    arrival_time,
                    departure_time,
                    parse_gtfs_time(arrival_time),
                    parse_gtfs_time(departure_time),
                    sequence,
                )
                for row in _iter_table(archive, "stop_times.txt")
                for trip_id in [str(row.get("trip_id", ""))]
                for stop_id in [str(row.get("stop_id", ""))]
                for arrival_time in [str(row.get("arrival_time", "") or "").strip()]
                for departure_time in [
                    str(row.get("departure_time", "") or "").strip()
                ]
                for sequence_value in [
                    legacy_int_or_none(row.get("stop_sequence", "0"))
                ]
                for sequence in [sequence_value if sequence_value is not None else 0]
                if trip_id and stop_id
            ),
        )
        self.connection.commit()

        self.connection.executemany(
            "INSERT OR REPLACE INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    str(row.get("service_id", "")).strip(),
                    str(row.get("start_date", "00000000")),
                    str(row.get("end_date", "99999999")),
                    *[
                        int(row.get(day, "0") or "0")
                        for day in (
                            "monday",
                            "tuesday",
                            "wednesday",
                            "thursday",
                            "friday",
                            "saturday",
                            "sunday",
                        )
                    ],
                )
                for row in _iter_table(archive, "calendar.txt")
                if str(row.get("service_id", "")).strip()
            ),
        )
        self.connection.commit()

        self.connection.executemany(
            "INSERT OR REPLACE INTO calendar_dates VALUES (?, ?, ?)",
            (
                (
                    str(row.get("service_id", "")).strip(),
                    str(row.get("date", "")).strip(),
                    exception_type,
                )
                for row in _iter_table(archive, "calendar_dates.txt")
                for exception_type in [
                    legacy_int_or_none(row.get("exception_type", "0"))
                ]
                if str(row.get("service_id", "")).strip()
                and str(row.get("date", "")).strip()
                and exception_type is not None
            ),
        )
        self.connection.commit()
        self.connection.executescript(
            """
            CREATE INDEX stop_times_trip_sequence
                ON stop_times(trip_id, stop_sequence);
            CREATE INDEX stop_times_stop_trip
                ON stop_times(stop_id, trip_id);
            CREATE INDEX trips_service
                ON trips(service_id);
            """
        )
        self.connection.commit()

    def iter_table(self, filename: str) -> Iterator[dict[str, object]]:
        definitions = {
            "routes.txt": (
                "routes",
                (
                    "route_id",
                    "route_short_name",
                    "route_long_name",
                    "route_type",
                    "agency_id",
                ),
            ),
            "stops.txt": (
                "stops",
                (
                    "stop_id",
                    "stop_name",
                    "stop_lat",
                    "stop_lon",
                    "stop_code",
                    "parent_station",
                    "location_type",
                    "platform_code",
                ),
            ),
            "trips.txt": (
                "trips",
                ("trip_id", "route_id", "service_id", "trip_headsign", "direction_id"),
            ),
            "stop_times.txt": (
                "stop_times",
                (
                    "trip_id",
                    "stop_id",
                    "arrival_time",
                    "departure_time",
                    "arrival_seconds",
                    "departure_seconds",
                    "stop_sequence",
                ),
            ),
        }
        definition = definitions.get(filename)
        if definition is None:
            return
        table, columns = definition
        query = f"SELECT {', '.join(columns)} FROM {table}"
        if table in {"routes", "stops", "trips"}:
            query += " ORDER BY rowid"
        for row in self.connection.execute(query):
            yield dict(zip(columns, row))

    def load_table(self, filename: str) -> list[dict[str, object]]:
        return list(self.iter_table(filename))

    def service_calendar(self) -> dict[str, dict[str, object]]:
        calendar: dict[str, dict[str, object]] = {}
        for row in self.connection.execute(
            "SELECT service_id, start_date, end_date, monday, tuesday, wednesday, "
            "thursday, friday, saturday, sunday FROM calendar"
        ):
            service_id = str(row[0])
            calendar[service_id] = {
                "startDate": row[1],
                "endDate": row[2],
                "weekdays": list(row[3:]),
                "exceptions": {},
            }
        for service_id, service_date, exception_type in self.connection.execute(
            "SELECT service_id, service_date, exception_type FROM calendar_dates"
        ):
            entry = calendar.setdefault(
                str(service_id),
                {
                    "startDate": "00000000",
                    "endDate": "99999999",
                    "weekdays": [0] * 7,
                    "exceptions": {},
                },
            )
            exceptions = entry["exceptions"]
            if isinstance(exceptions, dict):
                exceptions[str(service_date)] = int(exception_type)
        return calendar


class ExternalMergeStage:
    """SQLite staging database for one namespaced external city merge."""

    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="haltewecker-external-merge-")
        self.connection = sqlite3.connect(Path(self._temporary.name) / "merge.sqlite")
        self.connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            CREATE TABLE stops (
                stop_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                signature TEXT NOT NULL,
                source_id TEXT NOT NULL,
                search_name TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE routes (
                route_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                source_id TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE departures (
                stop_id TEXT NOT NULL,
                identity TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (stop_id, identity)
            ) WITHOUT ROWID;
            CREATE TABLE platforms (
                parent_id TEXT NOT NULL,
                child_id TEXT NOT NULL,
                PRIMARY KEY (parent_id, child_id)
            ) WITHOUT ROWID;
            CREATE TABLE trips (
                trip_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                source_id TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            """
        )
        self.timezones: set[str] = set()
        self.generated_values: list[str] = []

    def close(self) -> None:
        self.connection.close()
        self._temporary.cleanup()

    def add_stops(self, path: Path, source_id: str) -> int:
        inserted = 0
        for item in iter_json_array(path):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            stop_id = str(item["id"])
            payload = canonical_json(item)
            signature = canonical_json({
                key: item.get(key)
                for key in ("id", "name", "latitude", "longitude", "searchName", "stopCode")
            })
            existing = self.connection.execute(
                "SELECT signature, source_id FROM stops WHERE stop_id=?", (stop_id,)
            ).fetchone()
            if existing is not None:
                if existing[0] != signature:
                    raise ValueError(
                        f"Conflicting external stop ID collision for {stop_id}: "
                        f"provider={existing[1]} provider={source_id}"
                    )
                continue
            self.connection.execute(
                "INSERT INTO stops VALUES (?, ?, ?, ?, ?)",
                (stop_id, payload, signature, source_id, str(item.get("searchName", ""))),
            )
            inserted += 1
        return inserted

    def add_object_file(self, path: Path, table: str, source_id: str) -> int:
        inserted = 0
        for key, value in iter_json_object(path):
            if not isinstance(value, dict):
                continue
            payload = canonical_json(value)
            existing = self.connection.execute(
                f"SELECT payload, source_id FROM {table} WHERE {('route_id' if table == 'routes' else 'trip_id')}=?",
                (str(key),),
            ).fetchone()
            if existing is not None:
                if existing[0] != payload:
                    raise ValueError(
                        f"External GTFS namespace collision in {table}: {key} "
                        f"providers={existing[1]},{source_id}"
                    )
                continue
            column = "route_id" if table == "routes" else "trip_id"
            self.connection.execute(
                f"INSERT INTO {table} ({column}, payload, source_id) VALUES (?, ?, ?)",
                (str(key), payload, source_id),
            )
            inserted += 1
        return inserted

    def add_departures(self, path: Path) -> set[str]:
        sections: set[str] = set()

        def record_section(section: str) -> None:
            if section in sections:
                raise ValueError(f"Duplicate departure payload section: {section}")
            sections.add(section)

        for kind, key, value in iter_departure_payload_items(
            path,
            strict=True,
            on_section=record_section,
        ):
            if kind in {"generatedAt", "timezone"}:
                if kind in sections:
                    raise ValueError(f"Duplicate departure payload field: {kind}")
                if not isinstance(value, str) or not value:
                    raise ValueError(f"Invalid departure payload field: {kind}")
                sections.add(kind)
                if kind == "timezone" and isinstance(value, str) and value:
                    self.timezones.add(value)
                if kind == "generatedAt" and isinstance(value, str):
                    self.generated_values.append(value)
                self.connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                    (kind, json.dumps(value, ensure_ascii=False)),
                )
            elif kind == "stop" and isinstance(value, dict):
                payload = canonical_json(value)
                self.connection.execute(
                    "INSERT OR IGNORE INTO departures(stop_id, identity, payload) VALUES (?, ?, ?)",
                    (key, canonical_json(value), payload),
                )
            elif kind == "platform":
                self.connection.execute(
                    "INSERT OR IGNORE INTO platforms(parent_id, child_id) VALUES (?, ?)",
                    (key, str(value)),
                )
        self.connection.commit()
        return sections

    def commit(self) -> None:
        self.connection.commit()

    def metadata(self, key: str, default: object = None) -> object:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row is not None else default

    def write_outputs(self, output: Path, city_id: str) -> tuple[int, dict[str, object]]:
        """Write merged assets directly from SQLite cursors."""
        stops_directory = output / "stops"
        routes_directory = output / "routes"
        departures_directory = output / "departures"
        trips_directory = output / "trips"
        for directory in (stops_directory, routes_directory, departures_directory, trips_directory):
            directory.mkdir(parents=True, exist_ok=True)

        stop_path = stops_directory / f"{city_id}.json"
        stop_count = 0
        with stop_path.open("w", encoding="utf-8") as stream:
            stream.write("[")
            first = True
            for (payload,) in self.connection.execute(
                "SELECT payload FROM stops ORDER BY search_name, stop_id"
            ):
                if not first:
                    stream.write(",")
                stream.write(payload)
                first = False
                stop_count += 1
            stream.write("]")

        route_path = routes_directory / f"{city_id}.json"
        with route_path.open("w", encoding="utf-8") as stream:
            stream.write("{")
            first = True
            for route_id, payload in self.connection.execute(
                "SELECT route_id, payload FROM routes ORDER BY route_id"
            ):
                if not first:
                    stream.write(",")
                stream.write(json.dumps(route_id, ensure_ascii=False))
                stream.write(":")
                stream.write(payload)
                first = False
            stream.write("}")

        departures_path = departures_directory / f"{city_id}.json"
        with departures_path.open("w", encoding="utf-8") as stream:
            stream.write("{\"generatedAt\":")
            stream.write(json.dumps(self.metadata("generatedAt"), ensure_ascii=False))
            stream.write(",\"timezone\":")
            stream.write(json.dumps(self.metadata("timezone", "America/Toronto"), ensure_ascii=False))
            stream.write(",\"stops\":{")
            stop_cursor = self.connection.execute(
                "SELECT DISTINCT stop_id FROM departures ORDER BY stop_id"
            )
            first_stop = True
            for (stop_id,) in stop_cursor:
                if not first_stop:
                    stream.write(",")
                stream.write(json.dumps(stop_id, ensure_ascii=False))
                stream.write(": [".replace(" ", ""))
                first_item = True
                for (payload,) in self.connection.execute(
                    """
                    SELECT payload FROM departures
                    WHERE stop_id=?
                    ORDER BY json_extract(payload, '$.p'),
                             json_extract(payload, '$.t'),
                             json_extract(payload, '$.r')
                    """,
                    (stop_id,),
                ):
                    if not first_item:
                        stream.write(",")
                    stream.write(payload)
                    first_item = False
                stream.write("]")
                first_stop = False
            stream.write("},\"platforms\":{")
            first_platform = True
            for (parent_id,) in self.connection.execute(
                "SELECT DISTINCT parent_id FROM platforms ORDER BY parent_id"
            ):
                if not first_platform:
                    stream.write(",")
                stream.write(json.dumps(parent_id, ensure_ascii=False))
                stream.write(": [".replace(" ", ""))
                children = self.connection.execute(
                    "SELECT child_id FROM platforms WHERE parent_id=? ORDER BY child_id",
                    (parent_id,),
                )
                stream.write(",".join(json.dumps(row[0], ensure_ascii=False) for row in children))
                stream.write("]")
                first_platform = False
            stream.write("}}")

        trip_path = trips_directory / f"{city_id}.json"
        with trip_path.open("w", encoding="utf-8") as stream:
            stream.write("{")
            first = True
            for trip_id, payload in self.connection.execute(
                "SELECT trip_id, payload FROM trips ORDER BY trip_id"
            ):
                if not first:
                    stream.write(",")
                stream.write(json.dumps(trip_id, ensure_ascii=False))
                stream.write(":")
                stream.write(payload)
                first = False
            stream.write("}")

        return stop_count, {
            "generatedAt": self.metadata("generatedAt"),
            "timezone": self.metadata("timezone", "America/Toronto"),
        }


class ExternalDepartureStage:
    """SQLite-backed departure index with no full-feed Python collections."""

    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="haltewecker-external-departures-")
        self.connection = sqlite3.connect(Path(self._temporary.name) / "departures.sqlite")
        self.connection.executescript(
            """
            CREATE TABLE feed_stops (
                stop_id TEXT PRIMARY KEY,
                parent_station TEXT NOT NULL,
                stop_name TEXT NOT NULL,
                platform_code TEXT NOT NULL,
                location_type INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE routes (
                route_id TEXT PRIMARY KEY,
                short_name TEXT NOT NULL,
                long_name TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE active_trips (
                trip_id TEXT PRIMARY KEY,
                route_id TEXT NOT NULL,
                headsign TEXT NOT NULL,
                direction_id TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE resolved_stops (
                stop_id TEXT PRIMARY KEY,
                public_stop_id TEXT
            ) WITHOUT ROWID;
            CREATE TABLE terminal_stops (
                trip_id TEXT PRIMARY KEY,
                sequence INTEGER NOT NULL,
                stop_id TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE departures (
                public_stop_id TEXT NOT NULL,
                departure_time TEXT NOT NULL,
                trip_id TEXT NOT NULL,
                original_stop_id TEXT NOT NULL,
                platform_code TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                PRIMARY KEY (public_stop_id, departure_time, trip_id, original_stop_id, platform_code, sequence)
            ) WITHOUT ROWID;
            CREATE TABLE raw_stop_times (
                trip_id TEXT NOT NULL,
                stop_id TEXT NOT NULL,
                departure_time TEXT NOT NULL,
                sequence INTEGER NOT NULL
            );
            CREATE TABLE platforms (
                parent_id TEXT NOT NULL,
                child_id TEXT NOT NULL,
                PRIMARY KEY (parent_id, child_id)
            ) WITHOUT ROWID;
            """
        )
        self.connection.executescript(
            """
            CREATE INDEX raw_stop_times_trip_sequence
                ON raw_stop_times(trip_id, sequence);
            CREATE INDEX raw_stop_times_stop_trip
                ON raw_stop_times(stop_id, trip_id);
            CREATE INDEX departures_public_stop_time
                ON departures(public_stop_id, departure_time, trip_id, sequence);
            """
        )

    def close(self) -> None:
        self.connection.close()
        self._temporary.cleanup()

    def populate(
        self,
        archive,
        active_by_service: dict[str, list[str]],
        public_stop_ids: set[str],
        context: NormalizedProviderContext | None = None,
    ) -> None:
        try:
            from .build_german_departure_index import parse_gtfs_time
        except ImportError:
            from build_german_departure_index import parse_gtfs_time

        def source_rows(filename: str):
            if context is not None:
                return context.iter_table(filename)
            return _iter_table(archive, filename)

        started = time.monotonic()
        self.connection.executemany(
            "INSERT INTO routes VALUES (?, ?, ?)",
            (
                (
                    str(row.get("route_id", "")).strip(),
                    str(row.get("route_short_name", "")).strip(),
                    str(row.get("route_long_name", "")).strip(),
                )
                for row in source_rows("routes.txt")
                if str(row.get("route_id", "")).strip()
            ),
        )
        self.connection.executemany(
            "INSERT INTO feed_stops VALUES (?, ?, ?, ?, ?)",
            (
                (
                    str(row.get("stop_id", "")).strip(),
                    str(row.get("parent_station", "") or "").strip(),
                    str(row.get("stop_name", "") or "").strip(),
                    str(row.get("platform_code", "") or "").strip(),
                    int(str(row.get("location_type", "0") or "0"))
                    if str(row.get("location_type", "0") or "0").strip().isdigit()
                    else 0,
                )
                for row in source_rows("stops.txt")
                if str(row.get("stop_id", "")).strip()
            ),
        )
        self.connection.executemany(
            "INSERT INTO active_trips VALUES (?, ?, ?, ?)",
            (
                (
                    trip_id,
                    route_id,
                    str(row.get("trip_headsign", "") or "").strip(),
                    str(row.get("direction_id", "0") or "0"),
                )
                for row in source_rows("trips.txt")
                for trip_id in [str(row.get("trip_id", "")).strip()]
                for route_id in [str(row.get("route_id", "")).strip()]
                if trip_id
                and route_id
                and str(row.get("service_id", "")).strip() in active_by_service
            ),
        )
        log_memory_stage("departure-feed-tables", started=started)

        started = time.monotonic()
        for (stop_id,) in self.connection.execute("SELECT stop_id FROM feed_stops"):
            current = stop_id
            chain: list[str] = []
            seen: set[str] = set()
            resolved = None
            while current and current not in seen:
                if current in public_stop_ids:
                    chain.append(current)
                    resolved = current
                    break
                seen.add(current)
                chain.append(current)
                row = self.connection.execute(
                    "SELECT parent_station FROM feed_stops WHERE stop_id=?", (current,)
                ).fetchone()
                current = str(row[0]).strip() if row else ""
            self.connection.executemany(
                "INSERT OR IGNORE INTO resolved_stops VALUES (?, ?)",
                ((node, resolved) for node in chain),
            )
        self.connection.commit()
        log_memory_stage("departure-stop-resolution", started=started)

        started = time.monotonic()
        def valid_stop_times():
            for row in source_rows("stop_times.txt"):
                trip_id = str(row.get("trip_id", "")).strip()
                stop_id = str(row.get("stop_id", "")).strip()
                departure_time = str(row.get("departure_time", "") or "").strip()
                departure_seconds = row.get("departure_seconds")
                if not trip_id or not stop_id or not departure_time:
                    continue
                if departure_seconds is None and parse_gtfs_time(departure_time) is None:
                    continue
                sequence = legacy_int_or_none(row.get("stop_sequence", "0")) or 0
                yield trip_id, stop_id, departure_time, sequence

        self.connection.executemany(
            "INSERT INTO raw_stop_times VALUES (?, ?, ?, ?)",
            valid_stop_times(),
        )
        self.connection.commit()
        log_memory_stage("departure-stop-times-staging", started=started)
        started = time.monotonic()
        self.connection.executescript(
            """
            INSERT INTO terminal_stops(trip_id, sequence, stop_id)
            SELECT raw.trip_id, raw.sequence, raw.stop_id
            FROM raw_stop_times raw
            JOIN (
                SELECT trip_id, max(sequence) AS sequence, max(rowid) AS rowid
                FROM raw_stop_times
                GROUP BY trip_id
            ) maximum ON maximum.trip_id=raw.trip_id
                AND maximum.sequence=raw.sequence
                AND maximum.rowid=raw.rowid;
            INSERT OR IGNORE INTO departures(
                public_stop_id, departure_time, trip_id, original_stop_id, platform_code, sequence
            )
            SELECT resolved.public_stop_id, raw.departure_time, raw.trip_id, raw.stop_id,
                   feed.platform_code, raw.sequence
            FROM raw_stop_times raw
            JOIN active_trips active ON active.trip_id=raw.trip_id
            JOIN resolved_stops resolved ON resolved.stop_id=raw.stop_id
            JOIN feed_stops feed ON feed.stop_id=raw.stop_id
            WHERE resolved.public_stop_id IS NOT NULL;
            """
        )
        log_memory_stage("departure-sql-materialization", started=started)

        started = time.monotonic()
        for child_id, parent_id, location_type in self.connection.execute(
            "SELECT stop_id, parent_station, location_type FROM feed_stops WHERE parent_station!=''"
        ):
            if location_type != 0:
                continue
            child = self.connection.execute(
                "SELECT public_stop_id FROM resolved_stops WHERE stop_id=?", (child_id,)
            ).fetchone()
            if child and child[0] and child[0] != child_id:
                self.connection.execute(
                    "INSERT OR IGNORE INTO platforms VALUES (?, ?)",
                    (child[0], child_id),
                )
        self.connection.commit()
        log_memory_stage("departure-platforms", started=started)

    def write_outputs(
        self,
        output: Path,
        cities: list[dict[str, object]],
        city_stop_ids: dict[str, set[str]],
        namespace: str,
        timezone_name: str,
    ) -> None:
        generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        departures_directory = output / "departures"
        departures_directory.mkdir(parents=True, exist_ok=True)
        for city in cities:
            started = time.monotonic()
            city_id = str(city["id"])
            path = departures_directory / f"{city_id}.json"
            with path.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps({"generatedAt": generated_at, "timezone": timezone_name}, ensure_ascii=False)[:-1])
                stream.write(',"stops":{')
                first_stop = True
                for published_id in sorted(city_stop_ids.get(city_id, set())):
                    raw_id = published_id[len(namespace):] if namespace and published_id.startswith(namespace) else published_id
                    row = self.connection.execute(
                        "SELECT 1 FROM departures WHERE public_stop_id=? LIMIT 1", (raw_id,)
                    ).fetchone()
                    if row is None:
                        continue
                    if not first_stop:
                        stream.write(",")
                    stream.write(json.dumps(published_id, ensure_ascii=False) + ":[")
                    first_item = True
                    for departure_time, trip_id, original_stop_id, platform_code, sequence, route_id, headsign, direction_id, short_name, long_name, terminal_stop_id, terminal_name in self.connection.execute(
                        """
                        SELECT d.departure_time, d.trip_id, d.original_stop_id, d.platform_code, d.sequence,
                               t.route_id, t.headsign, t.direction_id, r.short_name, r.long_name,
                               terminal.stop_id, terminal_feed.stop_name
                        FROM departures d
                        JOIN active_trips t ON t.trip_id=d.trip_id
                        LEFT JOIN routes r ON r.route_id=t.route_id
                        LEFT JOIN terminal_stops terminal ON terminal.trip_id=d.trip_id
                        LEFT JOIN feed_stops terminal_feed ON terminal_feed.stop_id=terminal.stop_id
                        WHERE d.public_stop_id=?
                        ORDER BY d.departure_time, d.trip_id, d.sequence
                        """,
                        (raw_id,),
                    ):
                        destination = headsign or terminal_name or short_name or long_name or route_id
                        item = {
                            "t": f"{namespace}{trip_id}" if namespace else trip_id,
                            "r": f"{namespace}{route_id}" if namespace else route_id,
                            "h": destination,
                            "d": direction_id,
                            "p": departure_time,
                            "q": str(sequence),
                        }
                        if original_stop_id != raw_id:
                            item["s"] = f"{namespace}{original_stop_id}" if namespace else original_stop_id
                            if platform_code:
                                item["platform"] = platform_code
                        if not first_item:
                            stream.write(",")
                        stream.write(canonical_json(item))
                        first_item = False
                    stream.write("]")
                    first_stop = False
                stream.write('},"platforms":{')
                first_platform = True
                raw_city_ids = {
                    published_id[len(namespace):] if namespace and published_id.startswith(namespace) else published_id
                    for published_id in city_stop_ids.get(city_id, set())
                }
                for (parent_id,) in self.connection.execute(
                    "SELECT DISTINCT parent_id FROM platforms WHERE parent_id IN (%s) ORDER BY parent_id" % ",".join("?" for _ in raw_city_ids),
                    tuple(raw_city_ids),
                ) if raw_city_ids else ():
                    if not first_platform:
                        stream.write(",")
                    published_parent = f"{namespace}{parent_id}" if namespace else parent_id
                    stream.write(json.dumps(published_parent, ensure_ascii=False) + ":[")
                    children = self.connection.execute("SELECT child_id FROM platforms WHERE parent_id=? ORDER BY child_id", (parent_id,))
                    stream.write(",".join(json.dumps(f"{namespace}{row[0]}" if namespace else row[0], ensure_ascii=False) for row in children))
                    stream.write("]")
                    first_platform = False
                stream.write("}}")
            log_memory_stage(
                "departures-write",
                source=city_id,
                started=started,
            )


def _iter_table(archive, filename: str) -> Iterator[dict[str, str]]:
    if filename not in archive.namelist():
        return
    with archive.open(filename) as raw:
        yield from normalized_dict_reader(
            (line.decode("utf-8-sig") for line in raw)
        )
