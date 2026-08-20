#!/usr/bin/env python3
"""Read-only static GTFS departure API with automatic SQLite inode reopen."""

from __future__ import annotations

import json
import logging
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

from translink_gateway import TransLinkProxy
from ttc_gateway import TTCProxy
from tfl_gateway import TfLProxy
from bay_area_gateway import BayAreaTripUpdatesProxy, BayAreaVehiclePositionsProxy
from king_county_gateway import KingCountyTripUpdatesProxy, KingCountyVehiclePositionsProxy
from mbta_gateway import (
    MBTAAlertsGateway,
    MBTATripUpdatesGateway,
    MBTAVehiclePositionsGateway,
    MBTA_ALERTS_PATH,
    MBTA_TRIP_UPDATES_PATH,
    MBTA_VEHICLE_POSITIONS_PATH,
)
from wmata_gateway import (
    WMATAAlertsGateway,
    WMATATripUpdatesGateway,
    WMATAVehiclePositionsGateway,
    WMATA_ALERTS_PATH,
    WMATA_TRIP_UPDATES_PATH,
    WMATA_VEHICLE_POSITIONS_PATH,
)
from geofox_gateway import GeofoxProxy
from mta_ny_gateway import (
    MtaNYBusVehiclePositionsGateway,
    MtaNYRegistryCache,
    MtaNYTripUpdatesGateway,
    api_key_from_environment,
)
from kyiv_gateway import KyivVehiclePositionsGateway, KYIV_VEHICLE_POSITIONS_PATH
from apple_store_business_events import normalize_notification
from apple_store_notification_store import (
    AppleStoreNotificationStore,
    AppleStoreNotificationStoreError,
)
from apple_store_notifications import AppleStoreNotificationVerificationError, default_verifier
from telegram_sales_notifier import TelegramSalesNotificationError, TelegramSalesNotifier
from stm_gateway import (
    STMRealtimeGateway,
    STMRealtimePoller,
    STM_ALERTS_PATH,
    STM_TRIP_UPDATES_PATH,
    STM_VEHICLE_POSITIONS_PATH,
    STM_NAMESPACE,
)


DEFAULT_TIMEZONE = "Europe/Berlin"
MBTA_NAMESPACE = "mbta-boston:"
STM_PROVIDER_ID = "stm-montreal"
STM_CITY_ID = "montreal"

def _native_id(value: str) -> str:
    return value[len(MBTA_NAMESPACE):] if value.startswith(MBTA_NAMESPACE) else value

def _native(value: str, namespace: str) -> str:
    return value[len(namespace):] if namespace and value.startswith(namespace) else value
LOGGER = logging.getLogger("haltewecker.static_departures_api")

STATIC_DATA_ROOT = os.environ.get("STATIC_DATA_ROOT", "")
STATIC_DATA_PATH_PREFIXES = (
    "/static-stop-data/",
    "/static-stop-data-dev/",
)
IRELAND_REALTIME_ROOT = Path(
    os.environ.get("IRELAND_REALTIME_ROOT", "/data/ireland/realtime")
)
DEFAULT_APPLE_NOTIFICATION_STORE_PATH = "/data/apple-store-notifications/events.sqlite3"


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

    def provider_trip_registry(
        self,
        provider_id: str,
    ) -> tuple[set[str], dict[str, str]]:
        """Return internal trip ownership for realtime validation only."""
        with self.lock:
            try:
                rows = self._connection().execute(
                    """
                    SELECT owned.key_1, trips.route_id
                    FROM provider_entities AS owned
                    JOIN trips ON trips.trip_id = owned.key_1
                    WHERE owned.entity_type='trips' AND owned.provider_id=?
                    """,
                    (provider_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return set(), {}
        return {str(row[0]) for row in rows}, {
            str(row[0]): str(row[1]) for row in rows if row[1]
        }

    def provider_realtime_registry(
        self,
        provider_id: str,
    ) -> tuple[set[str], set[str], dict[str, str]]:
        """Return internal trip/route ownership without exposing it publicly."""
        with self.lock:
            try:
                rows = self._connection().execute(
                    """
                    SELECT owned.key_1, trips.route_id
                    FROM provider_entities AS owned
                    JOIN trips ON trips.trip_id = owned.key_1
                    WHERE owned.entity_type='trips' AND owned.provider_id=?
                    """,
                    (provider_id,),
                ).fetchall()
                route_rows = self._connection().execute(
                    """
                    SELECT key_1 FROM provider_entities
                    WHERE entity_type='routes' AND provider_id=?
                    """,
                    (provider_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return set(), set(), {}
        trips = {str(row[0]) for row in rows}
        route_by_trip = {str(row[0]): str(row[1]) for row in rows if row[1]}
        routes = {str(row[0]) for row in route_rows}
        return trips, routes, route_by_trip

    def provider_route_type_registry(self, provider_id: str) -> dict[str, str]:
        """Return internal route identities and their static GTFS route types."""
        with self.lock:
            try:
                rows = self._connection().execute(
                    """
                    SELECT owned.key_1, routes.route_type
                    FROM provider_entities AS owned
                    JOIN routes ON routes.route_id = owned.key_1
                    WHERE owned.entity_type='routes' AND owned.provider_id=?
                    """,
                    (provider_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return {}
        return {
            str(route_id): str(route_type)
            for route_id, route_type in rows
            if route_id is not None and route_type is not None
        }

    def provider_route_metadata(self, provider_id: str) -> dict[str, tuple[str, str]]:
        """Return provider-owned route labels and GTFS route types."""
        with self.lock:
            try:
                rows = self._connection().execute(
                    """
                    SELECT owned.key_1, routes.short_name, routes.route_type
                    FROM provider_entities AS owned
                    JOIN routes ON routes.route_id = owned.key_1
                    WHERE owned.entity_type='routes' AND owned.provider_id=?
                    """,
                    (provider_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return {}
        return {
            str(route_id): (str(short_name or ""), str(route_type or ""))
            for route_id, short_name, route_type in rows
            if route_id is not None
        }

    def city_stop_registry(self, city_id: str) -> set[str]:
        with self.lock:
            try:
                rows = self._connection().execute(
                    "SELECT stop_id FROM city_stops WHERE city_id=?",
                    (city_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return set()
        return {str(row[0]) for row in rows if row[0] is not None}

    def city_child_stop_ids(
        self,
        city_id: str,
        stop_ids: set[str],
        namespace: str,
        provider_id: str,
    ) -> set[str]:
        """Expand requested public parent stations to public child platforms."""
        public_ids = self.city_stop_registry(city_id)
        selected = {str(stop_id) for stop_id in stop_ids if str(stop_id) in public_ids}
        if not selected:
            return selected
        parent_ids = tuple(f"{namespace}{stop_id}" for stop_id in selected)
        placeholders = ",".join("?" for _ in parent_ids)
        with self.lock:
            try:
                rows = self._connection().execute(
                    f"""
                    SELECT raw.stop_id
                    FROM raw_stops AS raw
                    JOIN provider_entities AS owned
                      ON owned.entity_type='raw_stops' AND owned.key_1=raw.stop_id
                    WHERE owned.provider_id=?
                      AND raw.parent_station IN ({placeholders})
                    """,
                    (provider_id, *parent_ids),
                ).fetchall()
            except sqlite3.OperationalError:
                return selected
        selected.update(
            str(row[0])[len(namespace):]
            for row in rows
            if row[0] is not None
            and str(row[0]).startswith(namespace)
            and str(row[0])[len(namespace):] in public_ids
        )
        return selected

    def provider_trip_stop_registry(self, provider_id: str, trip_ids: set[str]) -> dict[tuple[str, int], str]:
        if not trip_ids:
            return {}
        placeholders = ",".join("?" for _ in trip_ids)
        with self.lock:
            rows = self._connection().execute(
                f"SELECT stop_times.trip_id, stop_times.stop_sequence, stop_times.raw_stop_id FROM stop_times JOIN provider_entities ON provider_entities.entity_type=\'trips\' AND provider_entities.key_1=stop_times.trip_id WHERE provider_entities.provider_id=? AND stop_times.trip_id IN ({placeholders})",
                (provider_id, *sorted(trip_ids)),
            ).fetchall()
        return {(str(row[0]), int(row[1])): str(row[2]) for row in rows}

    def provider_stop_registry(self, provider_id: str) -> set[str]:
        """Return internal stop identities for realtime ownership checks."""
        with self.lock:
            try:
                rows = self._connection().execute(
                    """
                    SELECT key_1 FROM provider_entities
                    WHERE entity_type='raw_stops' AND provider_id=?
                    """,
                    (provider_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return set()
        return {str(row[0]) for row in rows}

    def city_departure_mode(self, city_id: str) -> tuple[str, str, str, str]:
        with self.lock:
            try:
                cursor = self._connection().execute(
                    "SELECT mode, timezone, stop_id_prefix, identifier_prefix FROM city_departure_modes WHERE city_id=?",
                    (city_id,)
                )
                try:
                    row = cursor.fetchone()
                finally:
                    cursor.close()
                if row is not None:
                    return (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
            except sqlite3.OperationalError:
                pass
            try:
                cursor = self._connection().execute(
                    "SELECT mode, timezone, stop_id_prefix FROM city_departure_modes WHERE city_id=?",
                    (city_id,)
                )
                try:
                    row = cursor.fetchone()
                finally:
                    cursor.close()
            except sqlite3.OperationalError:
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
                    return "canonical", DEFAULT_TIMEZONE, "", ""
        if not row:
            return "canonical", DEFAULT_TIMEZONE, "", ""
        stop_id_prefix = str(row[2]) if len(row) > 2 else ""
        return str(row[0]), str(row[1]), stop_id_prefix, ""

    def city_departure_prefixes(self, city_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return all provider prefixes contributing to a city's merged static board."""
        with self.lock:
            try:
                rows = self._connection().execute(
                    """
                    SELECT stop_id_prefix, identifier_prefix
                    FROM provider_city_modes
                    WHERE city_id=?
                    ORDER BY provider_id
                    """,
                    (city_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            _, _, stop_id_prefix, identifier_prefix = self.city_departure_mode(city_id)
            rows = [(stop_id_prefix, identifier_prefix)]
        stop_prefixes = tuple(dict.fromkeys(str(row[0] or "") for row in rows))
        identifier_prefixes = tuple(dict.fromkeys(str(row[1] or "") for row in rows))
        return stop_prefixes, identifier_prefixes

    @staticmethod
    def _public_identifier_multi(identifier: str | None, prefixes: tuple[str, ...]) -> str:
        value = str(identifier or "")
        for prefix in sorted((prefix for prefix in prefixes if prefix), key=len, reverse=True):
            if value.startswith(prefix):
                return value[len(prefix):]
        return value

    def _public_identifier(self, identifier: str, prefix: str) -> str:
        return self._public_identifier_multi(identifier, (prefix,))

    def _canonical_stop_candidates(self, city_id: str, stop_id: str) -> tuple[str, ...]:
        stop_prefixes, _ = self.city_departure_prefixes(city_id)
        return tuple(dict.fromkeys(
            f"{prefix}{stop_id}" if prefix else stop_id
            for prefix in stop_prefixes
        ))

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
        mode, _, stop_id_prefix, _ = self.city_departure_mode(city_id)
        if mode != "exact-stop-with-parent-fallback":
            return f"{stop_id_prefix}{stop_id}"
        internal_stop_id = f"{stop_id_prefix}{stop_id}" if stop_id_prefix else stop_id
        with self.lock:
            cursor = self._connection().execute(
                "SELECT 1 FROM stop_times WHERE raw_stop_id=? LIMIT 1", (internal_stop_id,)
            )
            try:
                if cursor.fetchone() is not None:
                    return internal_stop_id
            finally:
                cursor.close()
            cursor = self._connection().execute(
                "SELECT parent_station FROM raw_stops WHERE stop_id=?", (internal_stop_id,)
            )
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
        return str(row[0]) if row and row[0] else internal_stop_id

    def lines(self, city_id: str, stop_id: str) -> list[dict[str, str | None]]:
        mode, _, stop_id_prefix, identifier_prefix = self.city_departure_mode(city_id)
        stop_prefixes, identifier_prefixes = self.city_departure_prefixes(city_id)
        query_stop_id = self._query_stop_id(city_id, stop_id)
        if mode == "exact-stop-with-parent-fallback":
            stop_predicate = "s.raw_stop_id=?"
            stop_parameters = (query_stop_id,)
        else:
            canonical_ids = self._canonical_stop_candidates(city_id, stop_id)
            stop_predicate = f"rs.canonical_stop_id IN ({','.join('?' for _ in canonical_ids)})"
            stop_parameters = canonical_ids
        with self.lock:
            cursor = self._connection().execute(
                f"""
                SELECT DISTINCT t.route_id,
                       COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),
                       t.direction_id,
                       COALESCE(NULLIF(t.headsign,''),NULLIF(destination_stops.stop_name,''),'Unbekanntes Ziel'),
                       t.terminal_stop_id
                FROM stop_times s
                JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id
                JOIN trips t ON t.trip_id=s.trip_id
                LEFT JOIN routes r ON r.route_id=t.route_id
                LEFT JOIN raw_stops AS destination_stops ON destination_stops.stop_id=t.terminal_stop_id
                WHERE {stop_predicate}
                ORDER BY 2,1
                """,
                stop_parameters
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [
            {
                "routeID": public_route_id,
                "line": line,
                "directionID": direction or None,
                "direction": destination or None,
                "destination": destination or None,
                "destinationStopID": public_destination_stop_id or None,
                "directionKey": self._direction_key(public_route_id, direction, public_destination_stop_id, destination),
            }
            for route_id, line, direction, destination, destination_stop_id in rows
            for public_route_id in [self._public_identifier_multi(route_id, identifier_prefixes)]
            for public_destination_stop_id in [self._public_identifier_multi(destination_stop_id, stop_prefixes)]
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
        mode, _, stop_id_prefix, identifier_prefix = self.city_departure_mode(city_id)
        stop_prefixes, identifier_prefixes = self.city_departure_prefixes(city_id)
        query_stop_id = self._query_stop_id(city_id, stop_id)
        if mode == "exact-stop-with-parent-fallback":
            stop_predicate = "s.raw_stop_id=?"
            stop_parameters = (query_stop_id,)
        else:
            canonical_ids = self._canonical_stop_candidates(city_id, stop_id)
            stop_predicate = f"rs.canonical_stop_id IN ({','.join('?' for _ in canonical_ids)})"
            stop_parameters = canonical_ids
        with self.lock:
            cursor = self._connection().execute(
                f"""
                SELECT a.service_date,s.departure_time,s.departure_seconds,s.stop_sequence,s.raw_stop_id,
                       t.trip_id,t.route_id,
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
                (*stop_parameters, service_from, service_to, limit)
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [
            {
                "serviceDate": f"{service_date[:4]}-{service_date[4:6]}-{service_date[6:]}",
                "scheduledTime": departure_time,
                "tripID": self._public_identifier_multi(trip_id, identifier_prefixes),
                "routeID": public_route_id,
                "line": line,
                "destination": destination,
                "directionID": direction or None,
                "direction": direction or None,
                "directionKey": self._direction_key(public_route_id, direction, public_destination_stop_id, destination),
                "destinationStopID": public_destination_stop_id or None,
                "platform": platform or None,
                "stopID": self._public_identifier_multi(raw_stop_id, stop_prefixes),
                "stopSequence": stop_sequence,
                "isRealtime": False
            }
            for service_date, departure_time, _seconds, stop_sequence, raw_stop_id, trip_id, route_id, line, destination, direction, destination_stop_id, platform in rows
            for public_route_id in [self._public_identifier_multi(route_id, identifier_prefixes)]
            for public_destination_stop_id in [self._public_identifier_multi(destination_stop_id, stop_prefixes)]
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
    apple_store_notification_verifier = None
    apple_store_notification_store: AppleStoreNotificationStore | None = None
    telegram_sales_notifier: TelegramSalesNotifier | None = None

    def version_string(self) -> str:
        return "HalteWecker"

    database: Database
    tfl_gateway: TfLProxy | None = None
    translink_gateway: TransLinkProxy | None = None
    ttc_gateway: TTCProxy | None = None
    bay_area_trip_updates_gateway: BayAreaTripUpdatesProxy | None = None
    bay_area_vehicle_positions_gateway: BayAreaVehiclePositionsProxy | None = None
    king_county_trip_updates_gateway: KingCountyTripUpdatesProxy | None = None
    king_county_vehicle_positions_gateway: KingCountyVehiclePositionsProxy | None = None
    mta_ny_trip_updates_gateway: MtaNYTripUpdatesGateway | None = None
    mta_ny_bus_vehicle_positions_gateway: MtaNYBusVehiclePositionsGateway | None = None
    mbta_trip_updates_gateway: MBTATripUpdatesGateway | None = None
    mbta_vehicle_positions_gateway: MBTAVehiclePositionsGateway | None = None
    mbta_alerts_gateway: MBTAAlertsGateway | None = None
    wmata_trip_updates_gateway: WMATATripUpdatesGateway | None = None
    wmata_vehicle_positions_gateway: WMATAVehiclePositionsGateway | None = None
    wmata_alerts_gateway: WMATAAlertsGateway | None = None
    geofox_gateway: GeofoxProxy | None = None
    kyiv_vehicle_positions_gateway: KyivVehiclePositionsGateway | None = None
    stm_gateway: STMRealtimeGateway | None = None

    def send_json(
        self,
        status: int,
        payload: object,
        cache_control: str | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)

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
            if parsed.path.startswith("/tfl/"):
                if self.tfl_gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "TfL provider unavailable"})
                response = self.tfl_gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path == "/translink/realtime/trip-updates":
                if self.translink_gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "TransLink provider unavailable"})
                response = self.translink_gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path == "/ttc/realtime/trip-updates":
                if self.ttc_gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "TTC provider unavailable"})
                response = self.ttc_gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path == "/511/realtime/trip-updates":
                if self.bay_area_trip_updates_gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "511 Bay Area provider unavailable"})
                response = self.bay_area_trip_updates_gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path == "/511/realtime/vehicle-positions":
                if self.bay_area_vehicle_positions_gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "511 Bay Area provider unavailable"})
                response = self.bay_area_vehicle_positions_gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path == "/king-county/realtime/trip-updates":
                if self.king_county_trip_updates_gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "King County provider unavailable"})
                response = self.king_county_trip_updates_gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path == "/king-county/realtime/vehicle-positions":
                if self.king_county_vehicle_positions_gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "King County provider unavailable"})
                response = self.king_county_vehicle_positions_gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path == "/mta-ny/realtime/trip-updates":
                if self.mta_ny_trip_updates_gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "MTA New York provider unavailable"})
                response = self.mta_ny_trip_updates_gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path == "/mta-ny/realtime/bus-vehicle-positions":
                if self.mta_ny_bus_vehicle_positions_gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "MTA New York provider unavailable"})
                response = self.mta_ny_bus_vehicle_positions_gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path in {MBTA_TRIP_UPDATES_PATH, MBTA_VEHICLE_POSITIONS_PATH, MBTA_ALERTS_PATH}:
                gateway = {
                    MBTA_TRIP_UPDATES_PATH: self.mbta_trip_updates_gateway,
                    MBTA_VEHICLE_POSITIONS_PATH: self.mbta_vehicle_positions_gateway,
                    MBTA_ALERTS_PATH: self.mbta_alerts_gateway,
                }[parsed.path]
                if gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "MBTA provider unavailable"})
                response = gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path in {WMATA_TRIP_UPDATES_PATH, WMATA_VEHICLE_POSITIONS_PATH, WMATA_ALERTS_PATH}:
                gateway = {
                    WMATA_TRIP_UPDATES_PATH: self.wmata_trip_updates_gateway,
                    WMATA_VEHICLE_POSITIONS_PATH: self.wmata_vehicle_positions_gateway,
                    WMATA_ALERTS_PATH: self.wmata_alerts_gateway,
                }[parsed.path]
                if gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "WMATA provider unavailable"})
                response = gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path in {STM_TRIP_UPDATES_PATH, STM_VEHICLE_POSITIONS_PATH, STM_ALERTS_PATH}:
                if self.stm_gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "STM provider unavailable"})
                response = self.stm_gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path == KYIV_VEHICLE_POSITIONS_PATH:
                if self.kyiv_vehicle_positions_gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Kyiv provider unavailable"})
                response = self.kyiv_vehicle_positions_gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
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
                _, timezone_name, _, _ = self.database.city_departure_mode(resolved_city)
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
        except Exception:
            LOGGER.exception("Unhandled GET request path=%s", parsed.path)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "service temporarily unavailable"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/apple/store-notifications":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request body"})
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
                signed_payload = payload.get("signedPayload") if isinstance(payload, dict) else None
                if not isinstance(signed_payload, str) or not signed_payload:
                    return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "signedPayload is required"})
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, OSError):
                return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request body"})

            try:
                verifier = self.apple_store_notification_verifier or default_verifier()
                verified_notification = verifier.verify(signed_payload)
            except AppleStoreNotificationVerificationError:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid signedPayload"})

            try:
                event = normalize_notification(verified_notification)
                store = self.apple_store_notification_store
                if store is None:
                    raise AppleStoreNotificationStoreError("Apple notification store is not configured")
                inserted = store.insert_once(event)
            except (AppleStoreNotificationStoreError, ValueError):
                LOGGER.exception(
                    "event=apple_store_notification_persistence_failed notificationUUID=%s",
                    verified_notification.notification_uuid,
                )
                return self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "notification persistence unavailable"},
                )

            if not inserted:
                LOGGER.info(
                    "event=apple_store_notification_duplicate notificationUUID=%s",
                    event.notification_uuid,
                )
                return self.send_json(HTTPStatus.OK, {"ok": True})

            notifier = self.telegram_sales_notifier
            if event.is_handled and notifier is not None:
                try:
                    notifier.send(event)
                except TelegramSalesNotificationError as error:
                    LOGGER.warning(
                        "event=telegram_sales_notification_failed app=%s "
                        "notificationType=%s reason=%s",
                        event.app,
                        event.notification_type,
                        error.reason,
                    )
                except Exception as error:
                    LOGGER.warning(
                        "event=telegram_sales_notification_failed app=%s "
                        "notificationType=%s reason=%s",
                        event.app,
                        event.notification_type,
                        type(error).__name__,
                    )
                else:
                    if not (
                        event.environment.casefold() == "sandbox"
                        and not notifier.notify_sandbox
                    ):
                        LOGGER.info(
                            "event=telegram_sales_notification_sent app=%s "
                            "notificationType=%s",
                            event.app,
                            event.notification_type,
                        )

            if event.is_handled:
                LOGGER.info(
                    "event=apple_store_business_event app=%s notificationType=%s "
                    "purchaseKind=%s productId=%s environment=%s notificationUUID=%s",
                    event.app,
                    event.notification_type,
                    event.purchase_kind,
                    event.product_id,
                    event.environment,
                    event.notification_uuid,
                )
            else:
                LOGGER.info(
                    "event=apple_store_notification_unhandled app=%s notificationType=%s "
                    "purchaseKind=%s productId=%s environment=%s notificationUUID=%s",
                    event.app,
                    event.notification_type,
                    event.purchase_kind,
                    event.product_id,
                    event.environment,
                    event.notification_uuid,
                )
            return self.send_json(HTTPStatus.OK, {"ok": True})

        if not parsed.path.startswith("/geofox/"):
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        if self.geofox_gateway is None:
            return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Geofox provider unavailable"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request body"})
            body = self.rfile.read(length)
            response = self.geofox_gateway.handle(parsed.path, body)
            return self.send_json(response.status, response.payload, response.cache_control)
        except (ValueError, OSError):
            return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request body"})


if __name__ == "__main__":
    database = Database(os.environ.get("DEPARTURES_DATABASE", "/data/departures-current.sqlite"))
    Handler.database = database
    Handler.apple_store_notification_store = AppleStoreNotificationStore(
        os.environ.get(
            "APPLE_NOTIFICATION_STORE_PATH",
            DEFAULT_APPLE_NOTIFICATION_STORE_PATH,
        )
    )
    Handler.telegram_sales_notifier = TelegramSalesNotifier.from_environment()
    Handler.geofox_gateway = GeofoxProxy.from_environment()
    Handler.tfl_gateway = TfLProxy.from_environment()
    Handler.translink_gateway = TransLinkProxy.from_environment()
    Handler.ttc_gateway = TTCProxy.from_environment()
    Handler.bay_area_trip_updates_gateway = BayAreaTripUpdatesProxy.from_environment(
        valid_trip_registry=lambda: database.provider_trip_registry("511-bay-area"),
        valid_stop_registry=lambda: database.provider_stop_registry("511-bay-area"),
    )
    Handler.bay_area_vehicle_positions_gateway = BayAreaVehiclePositionsProxy.from_environment(
        valid_registry=lambda: database.provider_realtime_registry("511-bay-area")
    )
    Handler.king_county_trip_updates_gateway = KingCountyTripUpdatesProxy.from_database(
        valid_trip_registry=lambda: database.provider_trip_registry("king-county-metro"),
        valid_stop_registry=lambda: database.provider_stop_registry("king-county-metro"),
    )
    Handler.king_county_vehicle_positions_gateway = KingCountyVehiclePositionsProxy.from_database(
        valid_registry=lambda: database.provider_realtime_registry("king-county-metro")
    )
    mta_ny_registry_cache = MtaNYRegistryCache()
    Handler.mta_ny_trip_updates_gateway = MtaNYTripUpdatesGateway(
        registry=lambda: mta_ny_registry_cache.get(database),
        api_key=api_key_from_environment,
    )
    Handler.mta_ny_bus_vehicle_positions_gateway = MtaNYBusVehiclePositionsGateway(
        registry=lambda: mta_ny_registry_cache.get(database),
        api_key=api_key_from_environment,
    )

    def mbta_native_trip_registry():
        trips, routes, route_by_trip = database.provider_realtime_registry("mbta-boston")
        return ({_native_id(value) for value in trips}, {_native_id(value) for value in routes}, {_native_id(key): _native_id(value) for key, value in route_by_trip.items()})

    def mbta_native_stop_registry():
        return {_native_id(value) for value in database.provider_stop_registry("mbta-boston")}

    def mbta_trip_stop_resolver(trip_ids, sequence_keys):
        internal_trips = {MBTA_NAMESPACE + trip_id for trip_id in trip_ids}
        mapping = database.provider_trip_stop_registry("mbta-boston", internal_trips)
        return {(_native_id(trip_id), sequence): _native_id(stop_id) for (trip_id, sequence), stop_id in mapping.items()}

    def mbta_trip_updates_registry():
        trips, _, route_by_trip = mbta_native_trip_registry()
        return trips, route_by_trip

    Handler.mbta_trip_updates_gateway = MBTATripUpdatesGateway(
        provider_id="mbta-boston", city_id="boston", path=MBTA_TRIP_UPDATES_PATH,
        upstream_url="https://cdn.mbta.com/realtime/TripUpdates.pb",
        trip_stop_resolver=mbta_trip_stop_resolver,
        valid_trip_registry=mbta_trip_updates_registry,
        valid_stop_registry=mbta_native_stop_registry,
        stop_id_mapper=lambda value: value, cache_ttl=15.0, max_stale=300.0,
        user_agent="HalteWecker-MBTA-GTFSRT/1.0",
    )
    Handler.mbta_vehicle_positions_gateway = MBTAVehiclePositionsGateway(
        valid_registry=lambda: (*mbta_native_trip_registry(), lambda value: value)
    )
    Handler.mbta_alerts_gateway = MBTAAlertsGateway()
    wmata_api_key = os.environ.get("WMATA_API_KEY", "").strip()
    if wmata_api_key:
        from wmata_gateway import WMATA_NAMESPACE

        def wmata_native_trip_registry():
            trip_ids: set[str] = set()
            route_ids: set[str] = set()
            routes_by_trip: dict[str, str] = {}
            for provider_id in ("wmata-bus", "wmata-rail"):
                provider_trips, provider_routes, provider_routes_by_trip = database.provider_realtime_registry(provider_id)
                trip_ids.update(provider_trips)
                route_ids.update(provider_routes)
                routes_by_trip.update(provider_routes_by_trip)
            native_trips = {value[len(WMATA_NAMESPACE):] if value.startswith(WMATA_NAMESPACE) else value for value in trip_ids}
            native_routes = {value[len(WMATA_NAMESPACE):] if value.startswith(WMATA_NAMESPACE) else value for value in route_ids}
            native_routes_by_trip = {
                (trip_id[len(WMATA_NAMESPACE):] if trip_id.startswith(WMATA_NAMESPACE) else trip_id):
                (route_id[len(WMATA_NAMESPACE):] if route_id.startswith(WMATA_NAMESPACE) else route_id)
                for trip_id, route_id in routes_by_trip.items()
            }
            return native_trips, native_routes, native_routes_by_trip

        def wmata_native_stop_registry():
            values = set()
            for provider_id in ("wmata-bus", "wmata-rail"):
                values.update(database.provider_stop_registry(provider_id))
            return {value[len(WMATA_NAMESPACE):] if value.startswith(WMATA_NAMESPACE) else value for value in values}

        def wmata_trip_stop_resolver(trip_ids, sequence_keys):
            internal_trips = {WMATA_NAMESPACE + trip_id for trip_id in trip_ids}
            mapping = {}
            for provider_id in ("wmata-bus", "wmata-rail"):
                mapping.update(database.provider_trip_stop_registry(provider_id, internal_trips))
            return {
                ((trip_id[len(WMATA_NAMESPACE):] if trip_id.startswith(WMATA_NAMESPACE) else trip_id), sequence):
                (stop_id[len(WMATA_NAMESPACE):] if stop_id.startswith(WMATA_NAMESPACE) else stop_id)
                for (trip_id, sequence), stop_id in mapping.items()
            }

        def wmata_trip_updates_registry():
            trips, _routes, route_by_trip = wmata_native_trip_registry()
            return trips, route_by_trip

        Handler.wmata_trip_updates_gateway = WMATATripUpdatesGateway(
            api_key=wmata_api_key,
            trip_stop_resolver=wmata_trip_stop_resolver,
            valid_trip_registry=wmata_trip_updates_registry,
            valid_stop_registry=wmata_native_stop_registry,
        )
        Handler.wmata_vehicle_positions_gateway = WMATAVehiclePositionsGateway(
            api_key=wmata_api_key,
            valid_registry=lambda: (*wmata_native_trip_registry(), lambda value: value),
        )
        Handler.wmata_alerts_gateway = WMATAAlertsGateway(api_key=wmata_api_key)
    stm_api_key = os.environ.get("STM_API_KEY", "").strip()
    if stm_api_key:
        def stm_native_trip_registry():
            trips, routes, route_by_trip = database.provider_realtime_registry(STM_PROVIDER_ID)
            route_metadata = database.provider_route_metadata(STM_PROVIDER_ID)
            native_trips = {_native(value, STM_NAMESPACE) for value in trips}
            native_routes = {_native(value, STM_NAMESPACE) for value in routes}
            native_routes_by_trip = {
                _native(trip_id, STM_NAMESPACE): _native(route_id, STM_NAMESPACE)
                for trip_id, route_id in route_by_trip.items()
            }
            route_types = {
                _native(route_id, STM_NAMESPACE): route_type
                for route_id, (_short_name, route_type) in route_metadata.items()
            }
            return native_trips, native_routes, native_routes_by_trip, route_types

        def stm_native_stop_registry():
            return {
                _native(value, STM_NAMESPACE)
                for value in database.provider_stop_registry(STM_PROVIDER_ID)
            }

        def stm_public_stop_registry():
            return database.city_stop_registry(STM_CITY_ID)

        def stm_stop_selector(stop_ids):
            return database.city_child_stop_ids(
                STM_CITY_ID,
                stop_ids,
                STM_NAMESPACE,
                STM_PROVIDER_ID,
            )

        def stm_route_short_registry():
            result = {}
            for route_id, (short_name, _route_type) in database.provider_route_metadata(STM_PROVIDER_ID).items():
                if short_name:
                    result.setdefault(short_name, set()).add(_native(route_id, STM_NAMESPACE))
            return result

        def stm_stop_code_registry():
            path = Path(STATIC_DATA_ROOT) / "stops" / f"{STM_CITY_ID}.json"
            try:
                records = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return {}
            result = {}
            for record in records if isinstance(records, list) else []:
                if not isinstance(record, dict):
                    continue
                stop_code = str(record.get("stopCode") or "").strip()
                stop_id = str(record.get("id") or "").strip()
                if stop_code and stop_id:
                    result.setdefault(stop_code, set()).add(stop_id)
            return result

        stm_poller = STMRealtimePoller(stm_api_key, start=True)
        Handler.stm_gateway = STMRealtimeGateway(
            poller=stm_poller,
            valid_registry=stm_native_trip_registry,
            valid_stop_registry=stm_native_stop_registry,
            public_stop_registry=stm_public_stop_registry,
            stop_selector=stm_stop_selector,
            route_short_registry=stm_route_short_registry,
            stop_code_registry=stm_stop_code_registry,
        )
    Handler.kyiv_vehicle_positions_gateway = KyivVehiclePositionsGateway(
        valid_route_registry=lambda: database.provider_route_type_registry("kyiv"),
        topology_path=(
            str(Path(STATIC_DATA_ROOT) / "radar" / "kyiv.json")
            if STATIC_DATA_ROOT
            else None
        ),
    )
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
