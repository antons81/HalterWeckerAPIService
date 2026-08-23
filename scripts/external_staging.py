"""Disk-backed staging helpers for large external GTFS merge operations."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

try:
    from .gtfs_csv import normalized_dict_reader
except ImportError:
    from gtfs_csv import normalized_dict_reader


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


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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

    def add_stops(self, path: Path, source_id: str) -> None:
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

    def add_object_file(self, path: Path, table: str, source_id: str) -> None:
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

    def add_departures(self, path: Path) -> None:
        for kind, key, value in iter_departure_payload(path):
            if kind in {"generatedAt", "timezone"}:
                if kind == "timezone" and isinstance(value, str) and value:
                    self.timezones.add(value)
                if kind == "generatedAt" and isinstance(value, str):
                    self.generated_values.append(value)
                self.connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                    (kind, json.dumps(value, ensure_ascii=False)),
                )
            elif kind == "stop" and isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    payload = canonical_json(item)
                    self.connection.execute(
                        "INSERT OR IGNORE INTO departures(stop_id, identity, payload) VALUES (?, ?, ?)",
                        (key, canonical_json(item), payload),
                    )
            elif kind == "platform" and isinstance(value, list):
                for child_id in value:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO platforms(parent_id, child_id) VALUES (?, ?)",
                        (key, str(child_id)),
                    )
        self.connection.commit()

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
    ) -> None:
        from build_german_departure_index import parse_gtfs_time

        started = time.perf_counter()
        self.connection.executemany(
            "INSERT INTO routes VALUES (?, ?, ?)",
            (
                (
                    str(row.get("route_id", "")).strip(),
                    str(row.get("route_short_name", "")).strip(),
                    str(row.get("route_long_name", "")).strip(),
                )
                for row in _iter_table(archive, "routes.txt")
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
                for row in _iter_table(archive, "stops.txt")
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
                for row in _iter_table(archive, "trips.txt")
                for trip_id in [str(row.get("trip_id", "")).strip()]
                for route_id in [str(row.get("route_id", "")).strip()]
                if trip_id
                and route_id
                and str(row.get("service_id", "")).strip() in active_by_service
            ),
        )
        print(f"[ExternalGTFS] departure stage feed tables duration={time.perf_counter() - started:.2f}s", flush=True)

        started = time.perf_counter()
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
        print(f"[ExternalGTFS] departure stage stop resolution duration={time.perf_counter() - started:.2f}s", flush=True)

        started = time.perf_counter()
        def valid_stop_times():
            for row in _iter_table(archive, "stop_times.txt"):
                trip_id = str(row.get("trip_id", "")).strip()
                stop_id = str(row.get("stop_id", "")).strip()
                departure_time = str(row.get("departure_time", "") or "").strip()
                if not trip_id or not stop_id or not departure_time:
                    continue
                if parse_gtfs_time(departure_time) is None:
                    continue
                try:
                    sequence = int(str(row.get("stop_sequence", "0") or "0"))
                except ValueError:
                    sequence = 0
                yield trip_id, stop_id, departure_time, sequence

        self.connection.executemany(
            "INSERT INTO raw_stop_times VALUES (?, ?, ?, ?)",
            valid_stop_times(),
        )
        self.connection.commit()
        print(f"[ExternalGTFS] departure stage stop_times staging duration={time.perf_counter() - started:.2f}s", flush=True)
        started = time.perf_counter()
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
        print(f"[ExternalGTFS] departure stage SQL materialization duration={time.perf_counter() - started:.2f}s", flush=True)

        started = time.perf_counter()
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
        print(f"[ExternalGTFS] departure stage platforms duration={time.perf_counter() - started:.2f}s", flush=True)

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


def _iter_table(archive, filename: str) -> Iterator[dict[str, str]]:
    if filename not in archive.namelist():
        return
    with archive.open(filename) as raw:
        yield from normalized_dict_reader(
            (line.decode("utf-8-sig") for line in raw)
        )
