#!/usr/bin/env python3
"""Read-only static GTFS departure API with automatic SQLite inode reopen."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Europe/Berlin"
STATIC_DATA_ROOT = os.environ.get("STATIC_DATA_ROOT", "")
STATIC_DATA_PATH_PREFIXES = (
    "/static-stop-data/",
    "/static-stop-data-dev/",
)
IRELAND_REALTIME_ROOT = Path(
    os.environ.get("IRELAND_REALTIME_ROOT", "/data/ireland/realtime")
)


class Database:
    @staticmethod
    def _direction_key(route_id: str, direction_id: str | None, destination_stop_id: str | None, destination: str) -> str:
        direction = (direction_id or "").strip()
        terminal = (destination_stop_id or "").strip()
        if terminal:
            return f"{route_id}|direction:{direction}|destination-stop:{terminal}"
        normalized = "".join(
            character for character in unicodedata.normalize("NFKD", destination).casefold()
            if not unicodedata.combining(character)
        )
        return f"{route_id}|direction:{direction}|destination:{' '.join(normalized.split())}"

    def __init__(self, path: str, ttl: float = 2.0) -> None:
        self.path, self.ttl, self.connection, self.identity, self.checked_at = path, ttl, None, None, 0.0
        self.lock = threading.Lock()

    def _connection(self) -> sqlite3.Connection:
        now = time.monotonic()
        stat_result = os.stat(self.path)
        identity = (stat_result.st_dev, stat_result.st_ino, stat_result.st_mtime_ns)
        if self.connection is None or (now - self.checked_at >= self.ttl and identity != self.identity):
            if self.connection is not None:
                self.connection.close()
            self.connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self.connection.execute("PRAGMA query_only=ON")
            self.identity = identity
        self.checked_at = now
        return self.connection

    def meta(self) -> dict[str, str]:
        with self.lock:
            cursor = self._connection().execute("SELECT key, value FROM metadata")
            try:
                metadata = dict(cursor.fetchall())
            finally:
                cursor.close()
            stat_result = os.stat(self.path)
            metadata["databasePath"] = self.path
            metadata["databaseDevice"] = str(stat_result.st_dev)
            metadata["databaseInode"] = str(stat_result.st_ino)
            metadata["databaseMTimeNS"] = str(stat_result.st_mtime_ns)
            return metadata

    def close(self) -> None:
        with self.lock:
            if self.connection is not None:
                self.connection.close()
                self.connection = None

    def city_has_stop(self, city_id: str, stop_id: str) -> bool:
        with self.lock:
            cursor = self._connection().execute(
                "SELECT 1 FROM city_stops WHERE city_id=? AND stop_id=?",
                (city_id, stop_id)
            )
            try:
                return cursor.fetchone() is not None
            finally:
                cursor.close()

    def city_departure_mode(self, city_id: str) -> tuple[str, str, str]:
        with self.lock:
            try:
                cursor = self._connection().execute(
                    "SELECT mode, timezone, stop_id_prefix FROM city_departure_modes WHERE city_id=?",
                    (city_id,)
                )
                try:
                    row = cursor.fetchone()
                finally:
                    cursor.close()
                if row is not None:
                    return (str(row[0]), str(row[1]), str(row[2]))
            except sqlite3.OperationalError:
                pass
            try:
                cursor = self._connection().execute(
                    "SELECT mode, timezone FROM city_departure_modes WHERE city_id=?",
                    (city_id,)
                )
                try:
                    row = cursor.fetchone()
                finally:
                    cursor.close()
            except sqlite3.OperationalError:
                return "canonical", DEFAULT_TIMEZONE, ""
        return (str(row[0]), str(row[1]), "") if row else ("canonical", DEFAULT_TIMEZONE, "")

    def resolve_city(self, city_id: str) -> str:
        with self.lock:
            cursor = self._connection().execute(
                "SELECT canonical_city_id FROM city_aliases WHERE alias_city_id=?",
                (city_id,)
            )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
            return str(row[0]) if row else city_id

    def _query_stop_id(self, city_id: str, stop_id: str) -> str:
        mode, _, stop_id_prefix = self.city_departure_mode(city_id)
        if mode != "exact-stop-with-parent-fallback":
            return f"{stop_id_prefix}{stop_id}"
        with self.lock:
            cursor = self._connection().execute(
                "SELECT 1 FROM stop_times WHERE raw_stop_id=? LIMIT 1", (stop_id,)
            )
            try:
                if cursor.fetchone() is not None:
                    return stop_id
            finally:
                cursor.close()
            cursor = self._connection().execute(
                "SELECT parent_station FROM raw_stops WHERE stop_id=?", (stop_id,)
            )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
        return str(row[0]) if row and row[0] else stop_id

    def lines(self, city_id: str, stop_id: str) -> list[dict[str, str | None]]:
        mode, _, _ = self.city_departure_mode(city_id)
        query_stop_id = self._query_stop_id(city_id, stop_id)
        stop_predicate = "s.raw_stop_id=?" if mode == "exact-stop-with-parent-fallback" else "rs.canonical_stop_id=?"
        with self.lock:
            cursor = self._connection().execute(
                f"""
                SELECT DISTINCT t.route_id,
                       COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),
                       t.direction_id,
                       t.headsign,
                       t.terminal_stop_id
                FROM stop_times s
                JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id
                JOIN trips t ON t.trip_id=s.trip_id
                LEFT JOIN routes r ON r.route_id=t.route_id
                WHERE {stop_predicate}
                ORDER BY 2,1
                """,
                (query_stop_id,)
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [
            {
                "routeID": route_id,
                "line": line,
                "directionID": direction or None,
                "direction": destination or None,
                "destination": destination or None,
                "destinationStopID": destination_stop_id or None,
                "directionKey": self._direction_key(route_id, direction, destination_stop_id, destination),
            }
            for route_id, line, direction, destination, destination_stop_id in rows
        ]

    def board(
        self,
        city_id: str,
        stop_id: str,
        limit: int,
        from_date: datetime | None = None,
        to_date: datetime | None = None
    ) -> list[dict[str, object]]:
        service_from = (from_date.date() - timedelta(days=1)).strftime("%Y%m%d") if from_date else "00000000"
        service_to = to_date.date().strftime("%Y%m%d") if to_date else "99999999"
        mode, _, _ = self.city_departure_mode(city_id)
        query_stop_id = self._query_stop_id(city_id, stop_id)
        stop_predicate = "s.raw_stop_id=?" if mode == "exact-stop-with-parent-fallback" else "rs.canonical_stop_id=?"
        with self.lock:
            cursor = self._connection().execute(
                f"""
                SELECT a.service_date,s.departure_time,s.departure_seconds,t.trip_id,t.route_id,
                       COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),
                       COALESCE(NULLIF(t.headsign,''),NULLIF(destination_stops.stop_name,''),'Unbekanntes Ziel'),
                       t.direction_id, t.terminal_stop_id, rs.platform_code
                FROM stop_times s
                JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id
                JOIN trips t ON t.trip_id=s.trip_id
                JOIN active_services a ON a.service_id=t.service_id
                LEFT JOIN routes r ON r.route_id=t.route_id
                LEFT JOIN raw_stops AS destination_stops ON destination_stops.stop_id=t.terminal_stop_id
                WHERE {stop_predicate} AND a.service_date BETWEEN ? AND ?
                ORDER BY a.service_date,s.departure_seconds,t.trip_id,s.stop_sequence
                LIMIT ?
                """,
                (query_stop_id, service_from, service_to, limit)
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [
            {
                "serviceDate": f"{service_date[:4]}-{service_date[4:6]}-{service_date[6:]}",
                "scheduledTime": departure_time,
                "tripID": trip_id,
                "routeID": route_id,
                "line": line,
                "destination": destination,
                "directionID": direction or None,
                "direction": direction or None,
                "directionKey": self._direction_key(route_id, direction, destination_stop_id, destination),
                "destinationStopID": destination_stop_id or None,
                "platform": platform or None,
                "isRealtime": False
            }
            for service_date, departure_time, _seconds, trip_id, route_id, line, destination, direction, destination_stop_id, platform in rows
        ]


def parse_iso_boundary(value: str | None, timezone_name: str = DEFAULT_TIMEZONE) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(ZoneInfo(timezone_name))


def departure_datetime(item: dict[str, object], timezone_name: str = DEFAULT_TIMEZONE) -> datetime:
    service_date = datetime.fromisoformat(str(item["serviceDate"])).replace(tzinfo=ZoneInfo(timezone_name))
    hour, minute, second = (int(part) for part in str(item["scheduledTime"]).split(":"))
    return service_date + timedelta(hours=hour, minutes=minute, seconds=second)


def bounded_limit(raw: str | None) -> int:
    try:
        limit = int(raw or "30")
    except ValueError:
        limit = 30
    return min(max(limit, 1), 100)


class Handler(BaseHTTPRequestHandler):
    database: Database

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def send_ireland_realtime(self, filename: str) -> None:
        """Serve an atomically replaced NTA JSON snapshot without rewriting it."""
        path = IRELAND_REALTIME_ROOT / filename
        try:
            body = path.read_bytes()
            payload = json.loads(body)
            if not isinstance(payload, dict) \
                    or not isinstance(payload.get("header"), dict) \
                    or not isinstance(payload.get("entity"), list):
                raise ValueError("invalid GTFS-Realtime JSON shape")
        except FileNotFoundError:
            return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "realtime source unavailable"})
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "realtime source invalid"})

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def send_static_file(self, relative: str) -> None:
        root = Path(STATIC_DATA_ROOT)
        if not root.is_dir():
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        segments = [part for part in unquote(relative).split("/") if part not in ("", ".")]
        if any(part == ".." for part in segments):
            return self.send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        candidate = root.joinpath(*segments).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return self.send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        if not candidate.is_file() or candidate.suffix != ".json":
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        try:
            size = candidate.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "public, max-age=300, stale-while-revalidate=60")
            self.end_headers()
            with candidate.open("rb") as source:
                shutil.copyfileobj(source, self.wfile)
        except OSError:
            try:
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
            except OSError:
                pass

    def do_GET(self) -> None:
        parsed, query = urlparse(self.path), parse_qs(urlparse(self.path).query)
        try:
            for prefix in STATIC_DATA_PATH_PREFIXES:
                if parsed.path.startswith(prefix):
                    return self.send_static_file(parsed.path[len(prefix):])
            if parsed.path == "/ireland/realtime/vehicles":
                return self.send_ireland_realtime("vehicles.json")
            if parsed.path == "/ireland/realtime/trip-updates":
                return self.send_ireland_realtime("trip_updates.json")
            if parsed.path == "/static-departures/health":
                return self.send_json(HTTPStatus.OK, {"ok": True, "database": self.database.meta()})
            if parsed.path == "/static-departures/meta":
                return self.send_json(HTTPStatus.OK, self.database.meta())
            city, stop = query.get("cityID", [None])[0], query.get("stopID", [None])[0]
            if not city or not stop:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "cityID and stopID are required"})
            resolved_city = self.database.resolve_city(city)
            if not self.database.city_has_stop(resolved_city, stop):
                return self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown cityID/stopID"})
            if parsed.path == "/static-departures/lines":
                payload = {"cityID": resolved_city, "stopID": stop, "lines": self.database.lines(resolved_city, stop)}
                if resolved_city != city:
                    payload["requestedCityID"] = city
                return self.send_json(HTTPStatus.OK, payload)
            if parsed.path == "/static-departures/board":
                limit = bounded_limit(query.get("limit", [None])[0])
                _, timezone_name, _ = self.database.city_departure_mode(resolved_city)
                from_date = parse_iso_boundary(query.get("from", [None])[0], timezone_name)
                to_date = parse_iso_boundary(query.get("to", [None])[0], timezone_name)
                departures = self.database.board(resolved_city, stop, 1000 if from_date or to_date else limit, from_date, to_date)
                if from_date or to_date:
                    departures = [
                        item for item in departures
                        if (from_date is None or departure_datetime(item, timezone_name) >= from_date)
                        and (to_date is None or departure_datetime(item, timezone_name) <= to_date)
                    ][:limit]
                payload = {"cityID": resolved_city, "stopID": stop, "departures": departures}
                if resolved_city != city:
                    payload["requestedCityID"] = city
                return self.send_json(HTTPStatus.OK, payload)
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception as error:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})


if __name__ == "__main__":
    database = Database(os.environ.get("DEPARTURES_DATABASE", "/data/departures-current.sqlite"))
    Handler.database = database
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
