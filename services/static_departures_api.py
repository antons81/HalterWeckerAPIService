#!/usr/bin/env python3
"""Read-only static GTFS departure API with automatic SQLite inode reopen."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Europe/Berlin"


class Database:
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

    def lines(self, stop_id: str) -> list[dict[str, str | None]]:
        with self.lock:
            cursor = self._connection().execute(
                """
                SELECT DISTINCT t.route_id,
                       COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),
                       t.direction_id
                FROM stop_times s
                JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id
                JOIN trips t ON t.trip_id=s.trip_id
                LEFT JOIN routes r ON r.route_id=t.route_id
                WHERE rs.canonical_stop_id=?
                ORDER BY 2,1
                """,
                (stop_id,)
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [{"routeID": route_id, "line": line, "direction": direction or None} for route_id, line, direction in rows]

    def board(
        self,
        stop_id: str,
        limit: int,
        from_date: datetime | None = None,
        to_date: datetime | None = None
    ) -> list[dict[str, object]]:
        service_from = (from_date.date() - timedelta(days=1)).strftime("%Y%m%d") if from_date else "00000000"
        service_to = to_date.date().strftime("%Y%m%d") if to_date else "99999999"
        with self.lock:
            cursor = self._connection().execute(
                """
                SELECT a.service_date,s.departure_time,s.departure_seconds,t.trip_id,t.route_id,
                       COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),
                       COALESCE(NULLIF(t.headsign,''),NULLIF(destination_stops.stop_name,''),'Unbekanntes Ziel'),
                       t.direction_id,rs.platform_code
                FROM stop_times s
                JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id
                JOIN trips t ON t.trip_id=s.trip_id
                JOIN active_services a ON a.service_id=t.service_id
                LEFT JOIN routes r ON r.route_id=t.route_id
                LEFT JOIN raw_stops AS destination_stops ON destination_stops.stop_id=t.terminal_stop_id
                WHERE rs.canonical_stop_id=? AND a.service_date BETWEEN ? AND ?
                ORDER BY a.service_date,s.departure_seconds,t.trip_id,s.stop_sequence
                LIMIT ?
                """,
                (stop_id, service_from, service_to, limit)
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
                "direction": direction or None,
                "platform": platform or None,
                "isRealtime": False
            }
            for service_date, departure_time, _seconds, trip_id, route_id, line, destination, direction, platform in rows
        ]


def parse_iso_boundary(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
    return parsed.astimezone(ZoneInfo(DEFAULT_TIMEZONE))


def departure_datetime(item: dict[str, object]) -> datetime:
    service_date = datetime.fromisoformat(str(item["serviceDate"])).replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
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

    def do_GET(self) -> None:
        parsed, query = urlparse(self.path), parse_qs(urlparse(self.path).query)
        try:
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
                payload = {"cityID": resolved_city, "stopID": stop, "lines": self.database.lines(stop)}
                if resolved_city != city:
                    payload["requestedCityID"] = city
                return self.send_json(HTTPStatus.OK, payload)
            if parsed.path == "/static-departures/board":
                limit = bounded_limit(query.get("limit", [None])[0])
                from_date = parse_iso_boundary(query.get("from", [None])[0])
                to_date = parse_iso_boundary(query.get("to", [None])[0])
                departures = self.database.board(stop, 1000 if from_date or to_date else limit, from_date, to_date)
                if from_date or to_date:
                    departures = [
                        item for item in departures
                        if (from_date is None or departure_datetime(item) >= from_date)
                        and (to_date is None or departure_datetime(item) <= to_date)
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
