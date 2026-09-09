#!/usr/bin/env python3
"""Read-only static GTFS departure API with automatic SQLite inode reopen."""

from __future__ import annotations

import json
import logging
import math
import os
import resource
import shutil
import sqlite3
import sys
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
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
from fintraffic_gateway import (
    FINTRAFFIC_TRIP_UPDATES_PATH,
    FINTRAFFIC_VEHICLE_POSITIONS_PATH,
    FintrafficProviderContext,
    FintrafficTripUpdatesGateway,
    FintrafficVehiclePositionsGateway,
    GTFSRealtimeProviderContext,
    GTFSRealtimeTripUpdatesGateway,
    GTFSRealtimeVehiclePositionsGateway,
    PublicGTFSRealtimeHTTPTransport,
)
from poland_gateway import (
    GdyniaDelaysGateway,
    PolandGTFSRealtimeGateway,
    _CombinedFeedCache,
)
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
from static_departures_runtime import (
    RuntimeUnavailable,
    hybrid_backend_from_environment,
    hybrid_runtime_enabled,
    shadow_backend_from_environment,
)


DEFAULT_TIMEZONE = "Europe/Berlin"
MBTA_NAMESPACE = "mbta-boston:"
STM_PROVIDER_ID = "stm-montreal"
STM_CITY_ID = "montreal"
AUSTRALIA_SEQ_PROVIDER_ID = "australia-translink-seq"
AUSTRALIA_ADELAIDE_PROVIDER_ID = "australia-adelaide"
AUSTRALIA_SEQ_TRIP_UPDATES_PATH = "/australia/seq/realtime/trip-updates"
AUSTRALIA_SEQ_VEHICLE_POSITIONS_PATH = "/australia/seq/realtime/vehicle-positions"
AUSTRALIA_ADELAIDE_TRIP_UPDATES_PATH = "/australia/adelaide/realtime/trip-updates"
AUSTRALIA_ADELAIDE_VEHICLE_POSITIONS_PATH = "/australia/adelaide/realtime/vehicle-positions"
AUSTRALIA_NSW_PROVIDER_ID = "australia-transport-nsw"
AUSTRALIA_NSW_TRIP_UPDATES_PATH = "/australia/nsw/realtime/trip-updates"
AUSTRALIA_NSW_VEHICLE_POSITIONS_PATH = "/australia/nsw/realtime/vehicle-positions"
AUSTRALIA_CANBERRA_PROVIDER_ID = "australia-transport-canberra"
AUSTRALIA_CANBERRA_TRIP_UPDATES_PATH = "/australia/canberra/realtime/trip-updates"
AUSTRALIA_CANBERRA_VEHICLE_POSITIONS_PATH = "/australia/canberra/realtime/vehicle-positions"
AUSTRALIA_SEQ_TRIP_UPDATES_UPSTREAM = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates"
AUSTRALIA_SEQ_VEHICLE_POSITIONS_UPSTREAM = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions"
AUSTRALIA_ADELAIDE_TRIP_UPDATES_UPSTREAM = "https://gtfs.adelaidemetro.com.au/v1/realtime/trip_updates"
AUSTRALIA_ADELAIDE_VEHICLE_POSITIONS_UPSTREAM = "https://gtfs.adelaidemetro.com.au/v1/realtime/vehicle_positions"
AUSTRALIA_NSW_TRIP_UPDATES_UPSTREAM = "https://api.transport.nsw.gov.au/v1/gtfs/realtime/buses"
AUSTRALIA_NSW_VEHICLE_POSITIONS_UPSTREAM = "https://api.transport.nsw.gov.au/v1/gtfs/vehiclepos/buses"
AUSTRALIA_CANBERRA_TRIP_UPDATES_UPSTREAM = "https://transport.api.act.gov.au/gtfs/data/gtfs/v2/trip-updates.pb"
AUSTRALIA_CANBERRA_VEHICLE_POSITIONS_UPSTREAM = "https://transport.api.act.gov.au/gtfs/data/gtfs/v2/vehicle-positions-mywayplus.pb"
AUSTRALIA_QUEENSLAND_REALTIME = {
    "australia-translink-cairns": {
        "path_prefix": "/australia/cairns/realtime",
        "trip_updates": "https://gtfsrt.api.translink.com.au/api/realtime/CNS/TripUpdates",
        "vehicle_positions": "https://gtfsrt.api.translink.com.au/api/realtime/CNS/VehiclePositions",
    },
    "australia-translink-bowen": {
        "path_prefix": "/australia/bowen/realtime",
        "trip_updates": "https://gtfsrt.api.translink.com.au/api/realtime/BOW/TripUpdates",
        "vehicle_positions": "https://gtfsrt.api.translink.com.au/api/realtime/BOW/VehiclePositions",
    },
    "australia-translink-innisfail": {
        "path_prefix": "/australia/innisfail/realtime",
        "trip_updates": "https://gtfsrt.api.translink.com.au/api/realtime/INN/TripUpdates",
        "vehicle_positions": "https://gtfsrt.api.translink.com.au/api/realtime/INN/VehiclePositions",
    },
    "australia-translink-fraser-coast": {
        "path_prefix": "/australia/fraser-coast/realtime",
        "trip_updates": "https://gtfsrt.api.translink.com.au/api/realtime/MHB/TripUpdates",
        "vehicle_positions": "https://gtfsrt.api.translink.com.au/api/realtime/MHB/VehiclePositions",
    },
}

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
GTFS_ROUTE_TYPE_TO_MODE = {
    "0": "tram",
    "1": "subway",
    "2": "train",
    "3": "bus",
    "4": "ferry",
    "5": "cableCar",
    "6": "gondola",
    "7": "funicular",
    "11": "trolleybus",
    "12": "monorail",
}


def _rss_kib() -> int:
    """Return current RSS in KiB on Linux, or the process peak elsewhere."""
    try:
        with Path("/proc/self/status").open(encoding="ascii") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value / 1024) if sys.platform == "darwin" else int(value)


def log_memory_stage(stage: str, **fields: object) -> None:
    """Log opt-in process memory checkpoints without changing normal startup."""
    enabled = os.environ.get("STATIC_DEPARTURES_MEMORY_INSTRUMENTATION", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return
    details = [f"stage={stage}", f"rss_kib={_rss_kib()}"]
    peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    details.append(
        f"peak_rss_kib={int(peak_rss_kib / 1024) if sys.platform == 'darwin' else int(peak_rss_kib)}"
    )
    details.extend(
        f"{key}={value}"
        for key, value in fields.items()
        if value is not None
    )
    print("[StaticDeparturesMemory] " + " ".join(details), flush=True)


class ExternalStaticData:
    """Read-only JSON service for generic external GTFS static packages."""

    NEARBY_GRID_CELL_DEGREES = 0.02

    def __init__(
        self,
        root: str,
        city_id: str = "israel",
        namespace: str = "israel:",
        timezone_name: str = "Asia/Jerusalem",
        now_provider: Callable[[], datetime] | None = None,
        database: Database | None = None,
    ) -> None:
        self.root = Path(root) if root else None
        self.city_id = city_id
        self.namespace = namespace
        self.timezone_name = timezone_name
        self.timezone = ZoneInfo(timezone_name)
        self.now_provider = now_provider or (lambda: datetime.now(self.timezone))
        self.database = database
        self.lock = threading.RLock()
        self.signature = None
        self.stops: dict[str, dict[str, object]] = {}
        self.routes: dict[str, dict[str, object]] = {}
        self.departures: dict[str, list[dict[str, object]]] = {}
        self.platforms: dict[str, list[str]] = {}
        self.children_by_parent: dict[str, tuple[str, ...]] = {}
        self.visible_stop_ids: frozenset[str] = frozenset()
        self.nearby_cells: dict[tuple[int, int], tuple[str, ...]] = {}

    def _path(self, directory: str, filename: str) -> Path:
        if self.root is None:
            raise FileNotFoundError("external static data root is not configured")
        return self.root / directory / filename

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int] | None:
        try:
            stat_result = path.stat()
        except FileNotFoundError:
            return None
        return stat_result.st_mtime_ns, stat_result.st_size

    def _load_if_needed(self) -> None:
        stops_path = self._path("stops", f"{self.city_id}.json")
        routes_path = (
            self._path("routes", f"{self.city_id}.json")
            if self.database is None
            else None
        )
        departures_path = (
            self._path("departures", f"{self.city_id}.json")
            if self.database is None
            else None
        )
        signature = (
            self._file_signature(stops_path),
            self._file_signature(routes_path) if routes_path is not None else None,
            self._file_signature(departures_path) if departures_path is not None else None,
        )
        if signature == self.signature:
            return
        if signature[0] is None or (self.database is None and signature[2] is None):
            raise FileNotFoundError("external static package is incomplete")
        log_memory_stage(
            "external-before-load",
            city=self.city_id,
            stops_bytes=signature[0][1],
            routes_bytes=signature[1][1] if signature[1] is not None else 0,
            departures_bytes=signature[2][1] if signature[2] is not None else 0,
            departures_source="sqlite" if self.database is not None else "json",
        )
        stops_payload = json.loads(stops_path.read_text(encoding="utf-8"))
        routes_payload = (
            json.loads(routes_path.read_text(encoding="utf-8"))
            if routes_path is not None and signature[1] is not None
            else {}
        )
        departures_payload = (
            json.loads(departures_path.read_text(encoding="utf-8"))
            if departures_path is not None
            else {}
        )
        if not isinstance(stops_payload, list) or not isinstance(departures_payload, dict):
            raise ValueError("invalid external static package")
        self.stops = {}
        for item in stops_payload:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            normalized = dict(item)
            normalized["id"] = self._storage_id(str(item["id"]))
            if item.get("parentStation"):
                normalized["parentStation"] = self._storage_id(str(item["parentStation"]))
            self.stops[normalized["id"]] = normalized
        log_memory_stage(
            "external-after-stops",
            city=self.city_id,
            stop_records=len(self.stops),
            raw_stop_records=len(stops_payload),
        )
        children_by_parent: dict[str, list[str]] = {}
        nearby_cells: dict[tuple[int, int], list[str]] = {}
        for stop_id, stop in self.stops.items():
            if int(stop.get("locationType") or 0) == 0:
                parent_id = self._parent_id(stop)
                if parent_id:
                    children_by_parent.setdefault(parent_id, []).append(stop_id)
            try:
                latitude = float(stop["latitude"])
                longitude = float(stop["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(latitude) or not math.isfinite(longitude):
                continue
            cell = self._nearby_cell(latitude, longitude)
            nearby_cells.setdefault(cell, []).append(stop_id)
        self.children_by_parent = {
            parent_id: tuple(sorted(child_ids))
            for parent_id, child_ids in children_by_parent.items()
        }
        self.visible_stop_ids = frozenset(
            stop_id
            for stop_id, stop in self.stops.items()
            if int(stop.get("locationType") or 0) != 0
            or not self._has_valid_parent(stop)
        )
        self.nearby_cells = {
            cell: tuple(stop_ids)
            for cell, stop_ids in nearby_cells.items()
        }
        log_memory_stage(
            "external-after-station-indexes",
            city=self.city_id,
            parent_indexes=len(self.children_by_parent),
            child_links=sum(len(child_ids) for child_ids in self.children_by_parent.values()),
            visible_stops=len(self.visible_stop_ids),
            nearby_cells=len(self.nearby_cells),
            nearby_indexed_stops=sum(len(stop_ids) for stop_ids in self.nearby_cells.values()),
        )
        self.routes = (
            {str(key): value for key, value in routes_payload.items() if isinstance(value, dict)}
            if isinstance(routes_payload, dict)
            else {}
        )
        log_memory_stage(
            "external-after-routes",
            city=self.city_id,
            route_records=len(self.routes),
            raw_route_records=len(routes_payload) if isinstance(routes_payload, dict) else 0,
            routes_source="sqlite" if self.database is not None else "json",
            status="not-loaded-by-api" if self.database is not None else "completed",
        )
        log_memory_stage(
            "external-after-trips",
            city=self.city_id,
            trip_records=0,
            status="not-loaded-by-api",
        )
        raw_departures = departures_payload.get("stops", {})
        self.departures = {}
        departure_count = 0
        if isinstance(raw_departures, dict):
            for key, value in raw_departures.items():
                if not isinstance(value, list):
                    continue
                normalized_items = []
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    normalized_item = dict(item)
                    if item.get("s"):
                        normalized_item["s"] = self._storage_id(str(item["s"]))
                    if item.get("parentStation"):
                        normalized_item["parentStation"] = self._storage_id(str(item["parentStation"]))
                    normalized_items.append(normalized_item)
                departure_count += len(normalized_items)
                self.departures[self._storage_id(str(key))] = normalized_items
        log_memory_stage(
            "external-after-departures",
            city=self.city_id,
            departure_stop_keys=len(self.departures),
            departure_records=departure_count,
            raw_departure_stop_keys=len(raw_departures) if isinstance(raw_departures, dict) else 0,
            departures_source="sqlite" if self.database is not None else "json",
        )
        raw_platforms = departures_payload.get("platforms", {})
        self.platforms = {}
        platform_link_count = 0
        if isinstance(raw_platforms, dict):
            for key, value in raw_platforms.items():
                if not isinstance(value, list):
                    continue
                parent_id = self._storage_id(str(key))
                child_ids = self.platforms.setdefault(parent_id, [])
                for child in value:
                    if child:
                        child_id = self._storage_id(str(child))
                        if child_id not in child_ids:
                            child_ids.append(child_id)
                            platform_link_count += 1
        log_memory_stage(
            "external-after-hierarchy-indexes",
            city=self.city_id,
            parent_indexes=len(self.platforms),
            platform_links=platform_link_count,
        )
        self.signature = signature

    def _ensure_loaded(self) -> None:
        with self.lock:
            self._load_if_needed()

    def _storage_id(self, value: str) -> str:
        value = str(value or "").strip()
        if not value or value.startswith(self.namespace):
            return value
        return f"{self.namespace}{value}"

    def _public_id(self, value: str) -> str:
        value = str(value or "")
        return value[len(self.namespace):] if value.startswith(self.namespace) else value

    def _parent_id(self, stop: dict[str, object]) -> str:
        return self._storage_id(str(stop.get("parentStation") or ""))

    def _nearby_cell(self, latitude: float, longitude: float) -> tuple[int, int]:
        cell_size = self.NEARBY_GRID_CELL_DEGREES
        return math.floor(latitude / cell_size), math.floor(longitude / cell_size)

    def _has_valid_parent(self, stop: dict[str, object]) -> bool:
        parent_id = self._parent_id(stop)
        parent = self.stops.get(parent_id)
        return bool(parent and int(parent.get("locationType") or 0) == 1)

    def _visible_stops(self) -> list[dict[str, object]]:
        return [self.stops[stop_id] for stop_id in self.visible_stop_ids]

    def _station_payload(self, stop: dict[str, object], include_children: bool = False) -> dict[str, object]:
        stop_id = str(stop["id"])
        payload: dict[str, object] = {
            "id": self._public_id(stop_id),
            "name": stop.get("name", ""),
            "latitude": stop.get("latitude"),
            "longitude": stop.get("longitude"),
            "locationType": int(stop.get("locationType") or 0),
            "parentStation": self._public_id(self._parent_id(stop)) or None,
            "platform": stop.get("platform"),
            "floor": stop.get("floor"),
        }
        children = [
            self.stops[child_id]
            for child_id in self.children_by_parent.get(stop_id, ())
            if child_id in self.stops
        ]
        payload["childPlatformCount"] = len(children)
        if include_children:
            payload["platforms"] = [
                self._station_payload(child)
                for child in sorted(children, key=lambda item: str(item.get("id")))
            ]
        return payload

    def nearby(self, latitude: float, longitude: float, radius_meters: float, limit: int) -> list[dict[str, object]]:
        self._ensure_loaded()
        radius_meters = max(float(radius_meters), 0.0)
        cell_size = self.NEARBY_GRID_CELL_DEGREES
        latitude_delta = radius_meters / 111_000.0
        longitude_scale = max(math.cos(math.radians(latitude)), 0.01)
        longitude_delta = radius_meters / (111_000.0 * longitude_scale)
        min_cell = self._nearby_cell(latitude - latitude_delta, longitude - longitude_delta)
        max_cell = self._nearby_cell(latitude + latitude_delta, longitude + longitude_delta)
        candidate_ids = {
            stop_id
            for latitude_cell in range(min_cell[0], max_cell[0] + 1)
            for longitude_cell in range(min_cell[1], max_cell[1] + 1)
            for stop_id in self.nearby_cells.get((latitude_cell, longitude_cell), ())
        }
        matches = []
        for stop_id in candidate_ids:
            if stop_id not in self.visible_stop_ids:
                continue
            stop = self.stops[stop_id]
            try:
                stop_latitude = float(stop["latitude"])
                stop_longitude = float(stop["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            lat1, lon1, lat2, lon2 = map(math.radians, (latitude, longitude, stop_latitude, stop_longitude))
            distance = 2 * 6371000 * math.asin(math.sqrt(
                math.sin((lat2 - lat1) / 2) ** 2
                + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
            ))
            if distance <= radius_meters:
                item = self._station_payload(stop)
                item["distanceMeters"] = round(distance, 1)
                matches.append(item)
        matches.sort(key=lambda item: (float(item["distanceMeters"]), str(item["id"])))
        return matches[:limit]

    def search(self, query: str, limit: int) -> list[dict[str, object]]:
        self._ensure_loaded()
        normalized_query = " ".join(query.casefold().split())
        matches = [
            self._station_payload(stop)
            for stop in self._visible_stops()
            if normalized_query in " ".join(str(stop.get("name", "")).casefold().split())
        ]
        return sorted(matches, key=lambda item: (str(item.get("name", "")).casefold(), str(item["id"])))[:limit]

    def station(self, stop_id: str) -> dict[str, object] | None:
        self._ensure_loaded()
        stop = self.stops.get(self._storage_id(stop_id))
        return self._station_payload(stop, include_children=True) if stop else None

    @staticmethod
    def _departure_seconds(value: object) -> int:
        try:
            hour, minute, second = (int(part) for part in str(value).split(":"))
            return hour * 3600 + minute * 60 + second
        except (TypeError, ValueError):
            return 2**31 - 1

    @staticmethod
    def _item_service_date(item: dict[str, object], fallback: date) -> date:
        value = str(item.get("serviceDate") or "").strip()
        if value:
            for format_string in ("%Y-%m-%d", "%Y%m%d"):
                try:
                    return datetime.strptime(value, format_string).date()
                except ValueError:
                    continue
        return fallback

    def _departure_datetime(
        self,
        item: dict[str, object],
        fallback_service_date: date,
    ) -> datetime | None:
        parts = str(item.get("p") or "").split(":")
        if len(parts) != 3:
            return None
        try:
            hour, minute, second = (int(part) for part in parts)
        except ValueError:
            return None
        if hour < 0 or minute not in range(60) or second not in range(60):
            return None
        service_date = self._item_service_date(item, fallback_service_date)
        service_start = datetime(
            service_date.year,
            service_date.month,
            service_date.day,
            tzinfo=self.timezone,
        )
        return service_start + timedelta(hours=hour, minutes=minute, seconds=second)

    def _now(self) -> datetime:
        value = self.now_provider()
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)

    def _raw_departures(self, stop_id: str) -> list[dict[str, object]]:
        requested = self._storage_id(stop_id)
        stop = self.stops.get(requested)
        if stop is None:
            return []
        items = list(self.departures.get(requested, []))
        if int(stop.get("locationType") or 0) == 1 and not items:
            child_ids = list(self.platforms.get(requested, []))
            child_ids.extend(
                child_id
                for child_id in self.children_by_parent.get(requested, ())
                if child_id not in child_ids
            )
            for child_id in child_ids:
                for item in self.departures.get(child_id, []):
                    if isinstance(item, dict) and not item.get("s"):
                        item = dict(item)
                        item["s"] = child_id
                    items.append(item)
        return items

    def departures_for(
        self,
        stop_id: str,
        limit: int,
        from_datetime: datetime | None = None,
    ) -> list[dict[str, object]]:
        self._ensure_loaded()
        if self.database is not None:
            return self.database.external_departures_for(
                self.city_id,
                stop_id,
                limit,
                from_datetime,
                self.timezone_name,
                now_provider=self._now,
            )
        requested = self._storage_id(stop_id)
        stop = self.stops.get(requested)
        if stop is None:
            return []
        if from_datetime is not None and from_datetime.tzinfo is None:
            from_datetime = from_datetime.replace(tzinfo=self.timezone)
        lower_bound = (
            from_datetime.astimezone(self.timezone)
            if from_datetime is not None
            else self._now()
        )
        result: list[tuple[datetime, dict[str, object]]] = []
        for item in self._raw_departures(requested):
            if not isinstance(item, dict):
                continue
            service_dates = [lower_bound.date()]
            if not item.get("serviceDate"):
                service_dates.append(lower_bound.date() + timedelta(days=1))
            for service_date in service_dates:
                absolute_departure = self._departure_datetime(item, service_date)
                if absolute_departure is None or absolute_departure < lower_bound:
                    continue
                route_id = str(item.get("r") or "")
                route = self.routes.get(route_id, {})
                actual_stop_id = str(item.get("s") or requested)
                actual_stop = self.stops.get(actual_stop_id, stop)
                route_type = str(item.get("routeType") or route.get("type") or "")
                mode = GTFS_ROUTE_TYPE_TO_MODE.get(route_type)
                result.append((absolute_departure, {
                    "tripID": self._public_id(str(item.get("t") or "")),
                    "routeID": self._public_id(route_id),
                    "line": route.get("short_name") or route.get("shortName") or self._public_id(route_id),
                    "destination": item.get("h") or None,
                    "directionID": item.get("d") or None,
                    "scheduledTime": item.get("p") or None,
                    "scheduledDeparture": item.get("p") or None,
                    "operatorID": item.get("agencyID") or route.get("agency") or None,
                    "operator": item.get("operator") or route.get("agencyName") or None,
                    "stopID": self._public_id(actual_stop_id),
                    "parentStation": self._public_id(str(item.get("parentStation") or actual_stop.get("parentStation") or "")) or None,
                    "platform": item.get("platform") or actual_stop.get("platform") or None,
                    "floor": item.get("floor") or actual_stop.get("floor") or None,
                    "transportMode": mode or "unknown",
                    "routeType": int(route_type) if route_type.isdigit() else None,
                    "isRealtime": False,
                    "source": "scheduled-static",
                }))
        result.sort(key=lambda entry: (
            entry[0],
            str(entry[1].get("tripID") or ""),
            str(entry[1].get("routeID") or ""),
            str(entry[1].get("stopID") or ""),
        ))
        return [item for _, item in result[:limit]]


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
            log_memory_stage("after-db-open", database=self.path)
        self.checked_at = now
        return self.connection

    def _table_columns(self, table: str) -> set[str]:
        return {
            str(row[1])
            for row in self._connection().execute(f"PRAGMA table_info({table})")
        }

    def meta(self) -> dict[str, str]:
        with self.lock:
            cursor = self._connection().execute("SELECT key, value FROM metadata")
            try:
                metadata = dict(cursor.fetchall())
            finally:
                cursor.close()
            log_memory_stage("after-metadata-load", metadata_entries=len(metadata))
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
        trips, routes, route_by_trip, _headsigns = self.provider_realtime_metadata(provider_id)
        return trips, routes, route_by_trip

    def provider_realtime_metadata(
        self,
        provider_id: str,
    ) -> tuple[set[str], set[str], dict[str, str], dict[str, str]]:
        """Return owned trips/routes and static trip display metadata."""
        with self.lock:
            try:
                rows = self._connection().execute(
                    """
                    SELECT owned.key_1, trips.route_id, trips.headsign
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
                return set(), set(), {}, {}
        trips = {str(row[0]) for row in rows}
        route_by_trip = {str(row[0]): str(row[1]) for row in rows if row[1]}
        headsign_by_trip = {
            str(row[0]): str(row[2] or "")
            for row in rows
            if row[0] is not None
        }
        routes = {str(row[0]) for row in route_rows}
        return trips, routes, route_by_trip, headsign_by_trip

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

    def fintraffic_provider_contexts(self, city_id: str) -> tuple[FintrafficProviderContext, ...]:
        """Return Finnish provider ownership used to join one shared RT feed."""
        with self.lock:
            try:
                rows = self._connection().execute(
                    """
                    SELECT provider_id, stop_id_prefix, identifier_prefix
                    FROM provider_city_modes
                    WHERE city_id=? AND provider_id LIKE 'finland-%'
                    ORDER BY provider_id
                    """,
                    (city_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return ()

        contexts: list[FintrafficProviderContext] = []
        for provider_id, stop_id_prefix, identifier_prefix in rows:
            provider = str(provider_id)
            trips, routes, route_by_trip, headsign_by_trip = self.provider_realtime_metadata(provider)
            contexts.append(
                FintrafficProviderContext(
                    provider_id=provider,
                    identifier_prefix=str(identifier_prefix or ""),
                    stop_id_prefix=str(stop_id_prefix or ""),
                    trips=frozenset(trips),
                    routes=frozenset(routes),
                    route_by_trip=dict(route_by_trip),
                    stops=frozenset(self.provider_stop_registry(provider)),
                    trip_headsign_by_trip=dict(headsign_by_trip),
                )
            )
        return tuple(contexts)

    def external_gtfs_provider_contexts(
        self,
        city_id: str,
        provider_id: str,
    ) -> tuple[GTFSRealtimeProviderContext, ...]:
        """Return one external GTFS source's static ownership for a city."""
        with self.lock:
            try:
                rows = self._connection().execute(
                    """
                    SELECT provider_id, stop_id_prefix, identifier_prefix
                    FROM provider_city_modes
                    WHERE city_id=? AND provider_id=?
                    """,
                    (city_id, provider_id),
                ).fetchall()
            except sqlite3.OperationalError:
                return ()

        contexts: list[GTFSRealtimeProviderContext] = []
        for row_provider_id, stop_id_prefix, identifier_prefix in rows:
            source_id = str(row_provider_id)
            trips, routes, route_by_trip, headsign_by_trip = self.provider_realtime_metadata(source_id)
            contexts.append(
                GTFSRealtimeProviderContext(
                    provider_id=source_id,
                    identifier_prefix=str(identifier_prefix or ""),
                    stop_id_prefix=str(stop_id_prefix or ""),
                    trips=frozenset(trips),
                    routes=frozenset(routes),
                    route_by_trip=dict(route_by_trip),
                    stops=frozenset(self.provider_stop_registry(source_id)),
                    trip_headsign_by_trip=dict(headsign_by_trip),
                )
            )
        return tuple(contexts)

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

    def _external_stop_time_ids(self, city_id: str, stop_id: str) -> tuple[str, ...]:
        _, _, stop_id_prefix, _ = self.city_departure_mode(city_id)
        internal_stop_id = (
            stop_id
            if not stop_id_prefix or stop_id.startswith(stop_id_prefix)
            else f"{stop_id_prefix}{stop_id}"
        )
        raw_stop_columns = self._table_columns("raw_stops")
        parent_expression = "parent_station" if "parent_station" in raw_stop_columns else "''"
        location_type_expression = "location_type" if "location_type" in raw_stop_columns else "0"
        with self.lock:
            row = self._connection().execute(
                f"SELECT stop_id, {location_type_expression} FROM raw_stops WHERE stop_id=?",
                (internal_stop_id,),
            ).fetchone()
            if row is None or int(row[1] or 0) != 1:
                return (internal_stop_id,)
            children = self._connection().execute(
                f"""
                SELECT stop_id
                FROM raw_stops
                WHERE {parent_expression}=? AND {location_type_expression}=0
                ORDER BY stop_id
                """,
                (internal_stop_id,),
            ).fetchall()
        return (internal_stop_id, *(str(child[0]) for child in children))

    @staticmethod
    def _external_departure_datetime(
        service_date: object,
        departure_time: object,
        timezone: ZoneInfo,
    ) -> datetime | None:
        try:
            raw_date = str(service_date)
            parsed_date = datetime.strptime(raw_date, "%Y%m%d").date()
            hour, minute, second = (int(part) for part in str(departure_time).split(":"))
        except (TypeError, ValueError):
            return None
        if hour < 0 or minute not in range(60) or second not in range(60):
            return None
        return datetime(
            parsed_date.year,
            parsed_date.month,
            parsed_date.day,
            tzinfo=timezone,
        ) + timedelta(hours=hour, minutes=minute, seconds=second)

    def external_departures_for(
        self,
        city_id: str,
        stop_id: str,
        limit: int,
        from_datetime: datetime | None,
        timezone_name: str,
        now_provider: Callable[[], datetime] | None = None,
    ) -> list[dict[str, object]]:
        timezone = ZoneInfo(timezone_name)
        lower_bound = from_datetime or (now_provider() if now_provider is not None else datetime.now(timezone))
        if lower_bound.tzinfo is None:
            lower_bound = lower_bound.replace(tzinfo=timezone)
        lower_bound = lower_bound.astimezone(timezone)
        stop_ids = self._external_stop_time_ids(city_id, stop_id)
        if not stop_ids:
            return []

        raw_stop_columns = self._table_columns("raw_stops")
        route_columns = self._table_columns("routes")
        stop_time_columns = self._table_columns("stop_times")
        agency_columns = self._table_columns("agencies")
        platform_expression = (
            "COALESCE(NULLIF(rs.platform_display,''),NULLIF(rs.platform_code,''))"
            if "platform_display" in raw_stop_columns
            else "rs.platform_code"
        )
        floor_expression = "rs.floor_display" if "floor_display" in raw_stop_columns else "''"
        parent_expression = "rs.parent_station" if "parent_station" in raw_stop_columns else "''"
        agency_id_expression = "r.agency_id" if "agency_id" in route_columns else "''"
        agency_join = (
            "LEFT JOIN agencies ag ON ag.agency_id=r.agency_id"
            if "agency_name" in agency_columns and "agency_id" in route_columns
            else ""
        )
        agency_name_expression = "ag.agency_name" if agency_join else "''"
        route_type_expression = "r.route_type" if "route_type" in route_columns else "''"
        departure_seconds_expression = (
            "s.departure_seconds"
            if "departure_seconds" in stop_time_columns
            else ""
            "CAST(substr(s.departure_time, 1, instr(s.departure_time, ':') - 1) AS INTEGER) * 3600 "
            "+ CAST(substr(s.departure_time, instr(s.departure_time, ':') + 1, 2) AS INTEGER) * 60 "
            "+ CAST(substr(s.departure_time, length(s.departure_time) - 1, 2) AS INTEGER)"
        )
        service_day_expression = (
            "julianday(substr(a.service_date,1,4) || '-' || "
            "substr(a.service_date,5,2) || '-' || substr(a.service_date,7,2))"
        )
        absolute_key = f"({service_day_expression} + ({departure_seconds_expression}) / 86400.0)"
        placeholders = ",".join("?" for _ in stop_ids)
        service_from = (lower_bound.date() - timedelta(days=1)).strftime("%Y%m%d")
        service_to = (lower_bound.date() + timedelta(days=1)).strftime("%Y%m%d")
        lower_date = lower_bound.date().isoformat()
        lower_seconds = (
            lower_bound.hour * 3600
            + lower_bound.minute * 60
            + lower_bound.second
            + lower_bound.microsecond / 1_000_000
        )
        requested_limit = max(1, int(limit))
        stop_prefixes, identifier_prefixes = self.city_departure_prefixes(city_id)
        query = f"""
            SELECT a.service_date, s.departure_time, s.stop_sequence, s.raw_stop_id,
                   t.trip_id, t.route_id,
                   COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),
                   COALESCE(NULLIF(t.headsign,''),NULLIF(destination_stops.stop_name,''),'Unbekanntes Ziel'),
                   t.direction_id, {platform_expression}, {floor_expression},
                   {parent_expression}, {agency_id_expression}, {agency_name_expression},
                   {route_type_expression}
            FROM stop_times s
            JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id
            JOIN trips t ON t.trip_id=s.trip_id
            JOIN active_services a ON a.service_id=t.service_id
            LEFT JOIN routes r ON r.route_id=t.route_id
            {agency_join}
            LEFT JOIN raw_stops AS destination_stops ON destination_stops.stop_id=t.terminal_stop_id
            WHERE s.raw_stop_id IN ({placeholders})
              AND a.service_date BETWEEN ? AND ?
              AND {absolute_key} >= julianday(?) + ? / 86400.0
            ORDER BY {absolute_key}, t.trip_id, t.route_id, s.raw_stop_id, s.stop_sequence
            LIMIT ?
        """
        parameters = (*stop_ids, service_from, service_to, lower_date, lower_seconds, requested_limit)
        with self.lock:
            rows = self._connection().execute(query, parameters).fetchall()

        result: list[tuple[datetime, dict[str, object]]] = []
        for (
            service_date,
            departure_time,
            stop_sequence,
            raw_stop_id,
            trip_id,
            route_id,
            line,
            destination,
            direction,
            platform,
            floor,
            parent_station,
            agency_id,
            operator,
            route_type,
        ) in rows:
            absolute_departure = self._external_departure_datetime(
                service_date,
                departure_time,
                timezone,
            )
            if absolute_departure is None or absolute_departure < lower_bound:
                continue
            public_route_id = self._public_identifier_multi(route_id, identifier_prefixes)
            public_stop_id = self._public_identifier_multi(raw_stop_id, stop_prefixes)
            route_type_value = str(route_type or "")
            mode = GTFS_ROUTE_TYPE_TO_MODE.get(route_type_value)
            result.append((absolute_departure, {
                "tripID": self._public_identifier_multi(trip_id, identifier_prefixes),
                "routeID": public_route_id,
                "line": line or public_route_id,
                "destination": destination or None,
                "directionID": direction or None,
                "scheduledTime": departure_time or None,
                "scheduledDeparture": departure_time or None,
                "serviceDate": datetime.strptime(str(service_date), "%Y%m%d").date().isoformat(),
                "departureDateTime": absolute_departure.isoformat(),
                "stopSequence": stop_sequence,
                "operatorID": self._public_identifier_multi(agency_id, identifier_prefixes) if agency_id else None,
                "operator": operator or None,
                "stopID": public_stop_id,
                "parentStation": self._public_identifier_multi(parent_station, stop_prefixes) if parent_station else None,
                "platform": platform or None,
                "floor": floor or None,
                "transportMode": mode or "unknown",
                "routeType": int(route_type_value) if route_type_value.isdigit() else None,
                "isRealtime": False,
                "source": "scheduled-static",
            }))
        result.sort(key=lambda entry: (
            entry[0],
            str(entry[1].get("tripID") or ""),
            str(entry[1].get("routeID") or ""),
            str(entry[1].get("stopID") or ""),
            int(entry[1].get("stopSequence") or 0),
        ))
        return [item for _, item in result[:requested_limit]]

    def trip_details(self, city_id: str, trip_id: str, static_root: str, service_date: str | None = None) -> dict | None:
        """Read one trip; coordinates come from the existing city stop catalog."""
        if not city_id or any(part in city_id for part in ("/", "\\", "..")):
            raise ValueError("invalid cityID")
        if service_date:
            service_date = date.fromisoformat(service_date).strftime("%Y%m%d")
        stop_prefixes, prefixes = self.city_departure_prefixes(city_id)
        candidates = tuple(dict.fromkeys(
            trip_id if prefix and trip_id.startswith(prefix) else prefix + trip_id
            for prefix in prefixes
        ))
        if not candidates:
            return None
        with self.lock:
            connection = self._connection()
            if connection.execute("SELECT 1 FROM city_departure_modes WHERE city_id=?", (city_id,)).fetchone() is None:
                return None
            route_columns = self._table_columns("routes")
            agency_join = "LEFT JOIN agencies ag ON ag.agency_id=r.agency_id" if "agency_id" in route_columns and self._table_columns("agencies") else ""
            agency_fields = "r.agency_id,ag.agency_name" if agency_join else "NULL,NULL"
            route_type = "r.route_type" if "route_type" in route_columns else "NULL"
            arrival_time = "s.arrival_time" if "arrival_time" in self._table_columns("stop_times") else "NULL"
            row = connection.execute(f"""
                SELECT t.trip_id,t.route_id,COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),
                       t.headsign,t.direction_id,{agency_fields},{route_type},t.service_id
                FROM trips t LEFT JOIN routes r ON r.route_id=t.route_id {agency_join}
                WHERE t.trip_id IN ({','.join('?' for _ in candidates)}) ORDER BY t.trip_id LIMIT 1
            """, candidates).fetchone()
            if row is None:
                return None
            if service_date and connection.execute("SELECT 1 FROM active_services WHERE service_id=? AND service_date=?", (row[8], service_date)).fetchone() is None:
                return None
            stops = connection.execute(f"""
                SELECT s.raw_stop_id,s.stop_sequence,{arrival_time},s.departure_time,rs.stop_name
                FROM stop_times s JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id
                WHERE s.trip_id=? ORDER BY s.stop_sequence,s.raw_stop_id
            """, (row[0],)).fetchall()
        try:
            catalog_value = json.loads(
                (Path(static_root) / "stops" / f"{city_id}.json").read_text(encoding="utf-8")
            )
            catalog = catalog_value if isinstance(catalog_value, list) else []
        except (OSError, json.JSONDecodeError, TypeError):
            catalog = []
        wanted = {self._public_identifier_multi(s[0], stop_prefixes) for s in stops}
        coordinates = {self._public_identifier_multi(str(s["id"]), stop_prefixes): s for s in catalog
                       if self._public_identifier_multi(str(s.get("id", "")), stop_prefixes) in wanted}
        ordered = []
        for stop_id, sequence, arrival, departure, name in stops:
            public_id = self._public_identifier_multi(stop_id, stop_prefixes)
            metadata = coordinates.get(public_id, {})
            ordered.append({"id": public_id, "name": name, "stopSequence": sequence,
                            "scheduledArrival": arrival or None, "scheduledDeparture": departure or None,
                            "latitude": metadata.get("latitude"), "longitude": metadata.get("longitude"),
                            "platform": metadata.get("platform"), "floor": metadata.get("floor")})
        mode = GTFS_ROUTE_TYPE_TO_MODE.get(str(row[7]), "unknown")
        return {"tripID": self._public_identifier_multi(row[0], prefixes),
                "routeID": self._public_identifier_multi(row[1], prefixes), "line": row[2],
                "destination": row[3] or (ordered[-1]["name"] if ordered else None), "directionID": row[4],
                "operatorID": self._public_identifier_multi(row[5], prefixes) or None, "operator": row[6],
                "transportMode": mode, "timezone": self.city_departure_mode(city_id)[1],
                "serviceDate": datetime.strptime(service_date, "%Y%m%d").date().isoformat() if service_date else None,
                "stops": ordered, "geometry": None, "source": "scheduled-static", "isRealtime": False}

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
            raw_stop_columns = self._table_columns("raw_stops")
            route_columns = self._table_columns("routes")
            has_agencies = bool(self._table_columns("agencies"))
            platform_expression = (
                "COALESCE(NULLIF(rs.platform_display,''),NULLIF(rs.platform_code,''))"
                if "platform_display" in raw_stop_columns
                else "rs.platform_code"
            )
            floor_expression = "rs.floor_display" if "floor_display" in raw_stop_columns else "''"
            location_type_expression = "rs.location_type" if "location_type" in raw_stop_columns else "0"
            stop_desc_expression = "rs.stop_desc" if "stop_desc" in raw_stop_columns else "''"
            agency_id_expression = "r.agency_id" if "agency_id" in route_columns else "''"
            agency_join = "LEFT JOIN agencies a ON a.agency_id=r.agency_id" if has_agencies and "agency_id" in route_columns else ""
            agency_name_expression = "a.agency_name" if agency_join else "''"
            route_type_expression = "r.route_type" if "route_type" in route_columns else "''"
            cursor = self._connection().execute(
                f"""
                SELECT a.service_date,s.departure_time,s.departure_seconds,s.stop_sequence,s.raw_stop_id,
                       t.trip_id,t.route_id,
                       COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),
                       COALESCE(NULLIF(t.headsign,''),NULLIF(destination_stops.stop_name,''),'Unbekanntes Ziel'),
                       t.direction_id, t.terminal_stop_id, {platform_expression},
                       {floor_expression}, rs.parent_station, {location_type_expression},
                       {stop_desc_expression}, {agency_id_expression}, {agency_name_expression},
                       {route_type_expression}
                FROM stop_times s
                JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id
                JOIN trips t ON t.trip_id=s.trip_id
                JOIN active_services a ON a.service_id=t.service_id
                LEFT JOIN routes r ON r.route_id=t.route_id
                {agency_join}
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
                "isRealtime": False,
                "operator": operator or None,
                "agencyID": self._public_identifier_multi(agency_id, identifier_prefixes) if agency_id else None,
                "parentStation": self._public_identifier_multi(parent_station, stop_prefixes) if parent_station else None,
                "platformStopID": self._public_identifier_multi(raw_stop_id, stop_prefixes),
                "floor": floor or None,
                "stopDesc": stop_desc or None,
                "locationType": location_type,
                "routeType": int(route_type) if route_type and str(route_type).isdigit() else None,
            }
            for service_date, departure_time, _seconds, stop_sequence, raw_stop_id, trip_id, route_id, line, destination, direction, destination_stop_id, platform, floor, parent_station, location_type, stop_desc, agency_id, operator, route_type in rows
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
    external_static_data: ExternalStaticData | None = None
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
    fintraffic_trip_updates_gateway: FintrafficTripUpdatesGateway | None = None
    fintraffic_vehicle_positions_gateway: FintrafficVehiclePositionsGateway | None = None
    australia_seq_trip_updates_gateway: GTFSRealtimeTripUpdatesGateway | None = None
    australia_seq_vehicle_positions_gateway: GTFSRealtimeVehiclePositionsGateway | None = None
    australia_adelaide_trip_updates_gateway: GTFSRealtimeTripUpdatesGateway | None = None
    australia_adelaide_vehicle_positions_gateway: GTFSRealtimeVehiclePositionsGateway | None = None
    australia_nsw_trip_updates_gateway: GTFSRealtimeTripUpdatesGateway | None = None
    australia_nsw_vehicle_positions_gateway: GTFSRealtimeVehiclePositionsGateway | None = None
    australia_canberra_trip_updates_gateway: GTFSRealtimeTripUpdatesGateway | None = None
    australia_canberra_vehicle_positions_gateway: GTFSRealtimeVehiclePositionsGateway | None = None
    australia_queensland_trip_updates_gateways: dict[str, GTFSRealtimeTripUpdatesGateway] = {}
    australia_queensland_vehicle_positions_gateways: dict[str, GTFSRealtimeVehiclePositionsGateway] = {}
    stm_gateway: STMRealtimeGateway | None = None
    poland_trip_updates_gateways: dict[str, PolandGTFSRealtimeGateway | GdyniaDelaysGateway] = {}
    poland_vehicle_positions_gateways: dict[str, PolandGTFSRealtimeGateway] = {}
    poland_alerts_gateways: dict[str, PolandGTFSRealtimeGateway] = {}

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

    def send_israel_static(self, parsed_path: str, query: dict[str, list[str]]) -> None:
        store = self.external_static_data
        if store is None:
            return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Israel static data unavailable"})
        segments = [unquote(part) for part in parsed_path.split("/") if part]
        try:
            if segments == ["israel", "stations", "nearby"]:
                latitude = float((query.get("latitude") or query.get("lat") or [""])[0])
                longitude = float((query.get("longitude") or query.get("lon") or [""])[0])
                radius = min(max(float((query.get("radiusMeters") or query.get("radius") or ["3000"])[0]), 1), 50000)
                payload = {
                    "timezone": store.timezone_name,
                    "stations": store.nearby(latitude, longitude, radius, bounded_limit(query.get("limit", [None])[0])),
                }
                return self.send_json(HTTPStatus.OK, payload, "public, max-age=60")
            if segments == ["israel", "stations", "search"]:
                search_query = (query.get("q") or query.get("query") or [""])[0].strip()
                if not search_query:
                    return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "q is required"})
                payload = {
                    "timezone": store.timezone_name,
                    "stations": store.search(search_query, bounded_limit(query.get("limit", [None])[0])),
                }
                return self.send_json(HTTPStatus.OK, payload, "public, max-age=60")
            if len(segments) == 3 and segments[:2] == ["israel", "stations"]:
                station = store.station(segments[2])
                if station is None:
                    return self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown station"})
                return self.send_json(HTTPStatus.OK, station, "public, max-age=300")
            if len(segments) == 4 and segments[:2] == ["israel", "stations"] and segments[3] == "departures":
                station_id = segments[2]
                if store.station(station_id) is None:
                    return self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown station"})
                raw_boundary = (query.get("from") or query.get("at") or [None])[0]
                try:
                    from_datetime = parse_iso_boundary(raw_boundary, store.timezone_name)
                except ValueError:
                    return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid from/at"})
                return self.send_json(HTTPStatus.OK, {
                    "stationID": station_id,
                    "timezone": store.timezone_name,
                    "source": "scheduled-static",
                    "departures": store.departures_for(
                        station_id,
                        bounded_limit(query.get("limit", [None])[0]),
                        from_datetime,
                    ),
                }, "public, max-age=60")
            if len(segments) == 4 and segments[:2] == ["israel", "platforms"] and segments[3] == "departures":
                platform_id = segments[2]
                station = store.station(platform_id)
                if station is None or int(station.get("locationType") or 0) != 0:
                    return self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown platform"})
                raw_boundary = (query.get("from") or query.get("at") or [None])[0]
                try:
                    from_datetime = parse_iso_boundary(raw_boundary, store.timezone_name)
                except ValueError:
                    return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid from/at"})
                return self.send_json(HTTPStatus.OK, {
                    "platformID": platform_id,
                    "timezone": store.timezone_name,
                    "source": "scheduled-static",
                    "departures": store.departures_for(
                        platform_id,
                        bounded_limit(query.get("limit", [None])[0]),
                        from_datetime,
                    ),
                }, "public, max-age=60")
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError, KeyError):
            LOGGER.exception("Invalid Israel static request path=%s", parsed_path)
            return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Israel static data unavailable"})

    def do_GET(self) -> None:
        parsed, query = urlparse(self.path), parse_qs(urlparse(self.path).query)
        try:
            if parsed.path.startswith("/israel/"):
                return self.send_israel_static(parsed.path, query)
            if parsed.path in self.poland_trip_updates_gateways:
                response = self.poland_trip_updates_gateways[parsed.path].handle(
                    parsed.path,
                    query,
                )
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path in self.poland_vehicle_positions_gateways:
                response = self.poland_vehicle_positions_gateways[parsed.path].handle(
                    parsed.path,
                    query,
                )
                return self.send_json(response.status, response.payload, response.cache_control)
            if parsed.path in self.poland_alerts_gateways:
                response = self.poland_alerts_gateways[parsed.path].handle(
                    parsed.path,
                    query,
                )
                return self.send_json(response.status, response.payload, response.cache_control)
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
            if parsed.path in {FINTRAFFIC_TRIP_UPDATES_PATH, FINTRAFFIC_VEHICLE_POSITIONS_PATH}:
                gateway = (
                    self.fintraffic_trip_updates_gateway
                    if parsed.path == FINTRAFFIC_TRIP_UPDATES_PATH
                    else self.fintraffic_vehicle_positions_gateway
                )
                if gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Fintraffic provider unavailable"})
                response = gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            australia_gateways = {
                AUSTRALIA_SEQ_TRIP_UPDATES_PATH: self.australia_seq_trip_updates_gateway,
                AUSTRALIA_SEQ_VEHICLE_POSITIONS_PATH: self.australia_seq_vehicle_positions_gateway,
                AUSTRALIA_ADELAIDE_TRIP_UPDATES_PATH: self.australia_adelaide_trip_updates_gateway,
                AUSTRALIA_ADELAIDE_VEHICLE_POSITIONS_PATH: self.australia_adelaide_vehicle_positions_gateway,
                AUSTRALIA_NSW_TRIP_UPDATES_PATH: self.australia_nsw_trip_updates_gateway,
                AUSTRALIA_NSW_VEHICLE_POSITIONS_PATH: self.australia_nsw_vehicle_positions_gateway,
                AUSTRALIA_CANBERRA_TRIP_UPDATES_PATH: self.australia_canberra_trip_updates_gateway,
                AUSTRALIA_CANBERRA_VEHICLE_POSITIONS_PATH: self.australia_canberra_vehicle_positions_gateway,
            }
            if parsed.path in australia_gateways:
                gateway = australia_gateways[parsed.path]
                if gateway is None:
                    return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Australia GTFS-RT provider unavailable"})
                response = gateway.handle(parsed.path, query)
                return self.send_json(response.status, response.payload, response.cache_control)
            queensland_gateways = {
                **{
                    f"{configuration['path_prefix']}/trip-updates": gateway
                    for provider_id, gateway in self.australia_queensland_trip_updates_gateways.items()
                    if (configuration := AUSTRALIA_QUEENSLAND_REALTIME.get(provider_id)) is not None
                },
                **{
                    f"{configuration['path_prefix']}/vehicle-positions": gateway
                    for provider_id, gateway in self.australia_queensland_vehicle_positions_gateways.items()
                    if (configuration := AUSTRALIA_QUEENSLAND_REALTIME.get(provider_id)) is not None
                },
            }
            if parsed.path in queensland_gateways:
                response = queensland_gateways[parsed.path].handle(parsed.path, query)
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
            if parsed.path == "/static-departures/trip":
                city_id = query.get("cityID", [""])[0]
                trip_id = query.get("tripID", [""])[0]
                if not city_id or not trip_id:
                    return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "cityID and tripID are required"})
                try:
                    details = self.database.trip_details(city_id, trip_id, STATIC_DATA_ROOT, query.get("serviceDate", [None])[0])
                except ValueError:
                    return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid trip request"})
                if details is None:
                    return self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown trip"})
                return self.send_json(HTTPStatus.OK, details)
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
        except RuntimeUnavailable as error:
            LOGGER.error(
                "event=static-departures-runtime status=ERROR path=%s reason=%s",
                parsed.path,
                error,
            )
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "static departures runtime unavailable"},
            )
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


def _load_poland_registry() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    repository_root = Path(__file__).resolve().parents[1]
    configured_path = os.environ.get("POLAND_EXTERNAL_GTFS_SOURCES", "").strip()
    candidates = (
        [Path(configured_path)]
        if configured_path
        else [
            repository_root / "config" / "external-gtfs-sources.json",
            repository_root / "config" / "poland-sources.json",
            Path("/app/config/poland-sources.json"),
        ]
    )
    sources_by_id: dict[str, dict[str, object]] = {}
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue
        for source in payload:
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("id", "")).strip()
            if source_id.startswith("poland-"):
                sources_by_id[source_id] = source

    city_candidates = [
        repository_root / "config" / "poland-cities.json",
        Path("/app/config/poland-cities.json"),
    ]
    cities: list[dict[str, object]] = []
    for candidate in city_candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, list):
            cities = [item for item in payload if isinstance(item, dict)]
            break
    return list(sources_by_id.values()), cities


def _configure_poland_gateways(database: Database) -> None:
    sources, cities = _load_poland_registry()
    if not sources or not cities:
        return

    city_by_source: dict[str, set[str]] = {}
    regions_by_provider: dict[str, dict[str, dict[str, object]]] = {}
    active_kinds_by_provider: dict[str, set[str]] = {}
    public_provider_ids: set[str] = set()
    for city in cities:
        city_id = str(city.get("id", "")).strip()
        if not city_id:
            continue
        external_provider_ids = {
            str(value).strip()
            for value in (
                city.get("externalGTFSProviders")
                or ([city.get("externalGTFSProvider")] if city.get("externalGTFSProvider") else [])
            )
            if str(value).strip()
        }
        for source_id in external_provider_ids:
            city_by_source.setdefault(source_id, set()).add(city_id)
        radar = city.get("transitRadar")
        radar_configurations = radar if isinstance(radar, list) else [radar]
        for configuration in radar_configurations:
            if not isinstance(configuration, dict):
                continue
            provider_id = str(configuration.get("providerID", "")).strip()
            if not provider_id.startswith("poland-"):
                continue
            public_provider_ids.add(provider_id)
            features = {
                str(feature)
                for feature in (configuration.get("features") or [])
                if isinstance(feature, str)
            }
            active_kinds_by_provider[provider_id] = {
                kind
                for kind, feature in (
                    ("tripUpdates", "tripUpdates"),
                    ("vehiclePositions", "vehiclePositions"),
                    ("alerts", "alerts"),
                )
                if feature in features
            }
            region = configuration.get("region")
            if isinstance(region, dict):
                regions_by_provider.setdefault(provider_id, {})[city_id] = region

    source_by_id = {str(source["id"]): source for source in sources}

    def poland_trip_stop_resolver(
        source_id: str,
        sequence_keys: set[tuple[str, int]],
    ) -> dict[tuple[str, int], str]:
        source = source_by_id.get(source_id)
        if source is None:
            return {}
        namespace = str(source.get("namespace") or "")
        internal_trip_ids = {
            f"{namespace}{trip_id}" for trip_id, _sequence in sequence_keys
        }
        mapping = database.provider_trip_stop_registry(source_id, internal_trip_ids)
        return {
            (
                trip_id[len(namespace):]
                if namespace and trip_id.startswith(namespace)
                else trip_id,
                sequence,
            ): (
                stop_id[len(namespace):]
                if namespace and stop_id.startswith(namespace)
                else stop_id
            )
            for (trip_id, sequence), stop_id in mapping.items()
        }

    source_groups: dict[str, list[dict[str, object]]] = {}
    for provider_id in sorted(public_provider_ids):
        if provider_id == "poland-krakow":
            group = [
                source
                for source in sources
                if str(source.get("mergeGroup", "")) == provider_id
            ]
        else:
            source = source_by_id.get(provider_id)
            group = [source] if source is not None else []
        if group:
            source_groups[provider_id] = group

    for provider_id, group in source_groups.items():
        city_ids = set()
        for source in group:
            city_ids.update(city_by_source.get(str(source["id"]), set()))
        if not city_ids:
            continue
        slug = provider_id.removeprefix("poland-")
        common = {
            "provider_id": provider_id,
            "city_ids": city_ids,
            "sources": group,
            "city_regions": regions_by_provider.get(provider_id, {}),
            "trip_stop_resolver": poland_trip_stop_resolver,
        }
        if any(
            isinstance(source.get("realtime"), dict)
            and source["realtime"].get("combinedURL")
            for source in group
        ):
            common["combined_feed_cache"] = _CombinedFeedCache()
        realtime_kinds = {
            "tripUpdates": "trip-updates",
            "vehiclePositions": "vehicle-positions",
            "alerts": "alerts",
        }
        for kind, suffix in realtime_kinds.items():
            if kind not in active_kinds_by_provider.get(provider_id, set()):
                continue
            has_source = any(
                isinstance(source.get("realtime"), dict)
                and (
                    kind == "tripUpdates"
                    and (
                        source["realtime"].get("tripUpdatesURL")
                        or source["realtime"].get("manifestURL")
                        or source["realtime"].get("combinedURL")
                    )
                    or kind == "vehiclePositions"
                    and (
                        source["realtime"].get("vehiclePositionsURL")
                        or source["realtime"].get("manifestURL")
                        or source["realtime"].get("combinedURL")
                    )
                    or kind == "alerts"
                    and (
                        source["realtime"].get("alertsURL")
                        or source["realtime"].get("manifestURL")
                        or source["realtime"].get("combinedURL")
                    )
                )
                for source in group
            )
            if not has_source:
                continue
            gateway = PolandGTFSRealtimeGateway(
                **common,
                path=f"/poland/{slug}/realtime/{suffix}",
                kind=kind,
            )
            if kind == "tripUpdates":
                Handler.poland_trip_updates_gateways[gateway._path] = gateway
            elif kind == "vehiclePositions":
                Handler.poland_vehicle_positions_gateways[gateway._path] = gateway
            else:
                Handler.poland_alerts_gateways[gateway._path] = gateway

    gdynia_source = source_by_id.get("poland-gdynia")
    if gdynia_source is not None:
        city_ids = city_by_source.get("poland-gdynia", set())
        if city_ids:
            gateway = GdyniaDelaysGateway(
                provider_id="poland-gdynia",
                city_ids=city_ids,
                source=gdynia_source,
                path="/poland/gdynia/realtime/trip-updates",
            )
            Handler.poland_trip_updates_gateways[gateway._path] = gateway

    LOGGER.info(
        "event=poland_gateways_configured providers=%d trip=%d vehicle=%d alerts=%d",
        len(source_groups),
        len(Handler.poland_trip_updates_gateways),
        len(Handler.poland_vehicle_positions_gateways),
        len(Handler.poland_alerts_gateways),
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=getattr(
            logging,
            os.environ.get("HALTEWECKER_LOG_LEVEL", "INFO").strip().upper(),
            logging.INFO,
        ),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database_path = os.environ.get("DEPARTURES_DATABASE", "/data/departures-current.sqlite")
    log_memory_stage("before-db-open", database=database_path)
    database = Database(database_path)
    if hybrid_runtime_enabled():
        database = hybrid_backend_from_environment(database)
    else:
        try:
            shadow_database = shadow_backend_from_environment(database)
        except RuntimeUnavailable as error:
            LOGGER.error(
                "event=shard-shadow-runtime status=ERROR reason=%s; legacy path remains authoritative",
                error,
            )
        else:
            if shadow_database is not None:
                database = shadow_database
    Handler.database = database
    Handler.external_static_data = ExternalStaticData(
        STATIC_DATA_ROOT,
        database=database,
    )
    Handler.apple_store_notification_store = AppleStoreNotificationStore(
        os.environ.get(
            "APPLE_NOTIFICATION_STORE_PATH",
            DEFAULT_APPLE_NOTIFICATION_STORE_PATH,
        )
    )
    Handler.telegram_sales_notifier = TelegramSalesNotifier.from_environment()
    _configure_poland_gateways(database)
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
    def fintraffic_city_configuration() -> tuple[set[str], dict[str, dict[str, object]]]:
        candidates = [
            Path(STATIC_DATA_ROOT) / "transit-radar-cities.json",
            Path(__file__).resolve().parents[1] / "config" / "finland-cities.json",
            Path("/app/config/finland-cities.json"),
        ]
        payload = None
        for candidate in candidates:
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                break
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
        city_ids: set[str] = set()
        regions: dict[str, dict[str, object]] = {}
        if isinstance(payload, list):
            payload = {"cities": payload}
        for city in (payload or {}).get("cities", []) if isinstance(payload, dict) else []:
            if not isinstance(city, dict):
                continue
            if "appCityID" in city:
                city_id = str(city.get("appCityID", ""))
                providers = city.get("providers", [])
                for provider in providers if isinstance(providers, list) else []:
                    if isinstance(provider, dict) and provider.get("adapter") == "fintraffic":
                        city_ids.add(city_id)
                        region = provider.get("region")
                        if isinstance(region, dict):
                            regions[city_id] = region
                        break
            elif isinstance(city.get("id"), str):
                radar = city.get("transitRadar")
                if isinstance(radar, dict) and radar.get("adapter") == "fintraffic":
                    city_id = str(city["id"])
                    city_ids.add(city_id)
                    region = radar.get("region")
                    if isinstance(region, dict):
                        regions[city_id] = region
        return city_ids, regions

    fintraffic_city_ids, fintraffic_city_regions = fintraffic_city_configuration()
    Handler.fintraffic_trip_updates_gateway = FintrafficTripUpdatesGateway.from_environment(
        city_ids=fintraffic_city_ids,
        context_registry=lambda city_id: database.fintraffic_provider_contexts(city_id),
    )
    Handler.fintraffic_vehicle_positions_gateway = FintrafficVehiclePositionsGateway.from_environment(
        city_ids=fintraffic_city_ids,
        city_regions=fintraffic_city_regions,
        context_registry=lambda city_id: database.fintraffic_provider_contexts(city_id),
    )

    def australia_city_configuration(
        adapter: str,
        provider_id: str | None = None,
    ) -> tuple[set[str], dict[str, dict[str, object]]]:
        candidates = [
            Path(STATIC_DATA_ROOT) / "transit-radar-cities.json",
            Path("/data/current/transit-radar-cities.json"),
        ]
        payload: object = None
        for candidate in candidates:
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                break
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue

        city_ids: set[str] = set()
        regions: dict[str, dict[str, object]] = {}
        for city in (payload or {}).get("cities", []) if isinstance(payload, dict) else []:
            if not isinstance(city, dict):
                continue
            city_id = str(city.get("appCityID", "")).strip()
            providers = city.get("providers", [])
            for provider in providers if isinstance(providers, list) else []:
                if not isinstance(provider, dict) or provider.get("adapter") != adapter:
                    continue
                if provider_id is not None and str(provider.get("providerID", "")) != provider_id:
                    continue
                region = provider.get("region")
                if city_id and isinstance(region, dict):
                    city_ids.add(city_id)
                    regions[city_id] = region
                break
        return city_ids, regions

    seq_city_ids, seq_city_regions = australia_city_configuration("translinkSEQ")
    if seq_city_ids:
        seq_trip_transport = PublicGTFSRealtimeHTTPTransport(
            "HalteWecker-TranslinkSEQ-GTFSRT/1.0"
        )
        seq_vehicle_transport = PublicGTFSRealtimeHTTPTransport(
            "HalteWecker-TranslinkSEQ-GTFSRT/1.0"
        )
        Handler.australia_seq_trip_updates_gateway = GTFSRealtimeTripUpdatesGateway(
            provider_id=AUSTRALIA_SEQ_PROVIDER_ID,
            city_ids=seq_city_ids,
            context_registry=lambda city_id: database.external_gtfs_provider_contexts(
                city_id,
                AUSTRALIA_SEQ_PROVIDER_ID,
            ),
            transport=seq_trip_transport,
            upstream_url=AUSTRALIA_SEQ_TRIP_UPDATES_UPSTREAM,
            path=AUSTRALIA_SEQ_TRIP_UPDATES_PATH,
            strict_static_join=True,
        )
        Handler.australia_seq_vehicle_positions_gateway = GTFSRealtimeVehiclePositionsGateway(
            provider_id=AUSTRALIA_SEQ_PROVIDER_ID,
            city_ids=seq_city_ids,
            city_regions=seq_city_regions,
            context_registry=lambda city_id: database.external_gtfs_provider_contexts(
                city_id,
                AUSTRALIA_SEQ_PROVIDER_ID,
            ),
            transport=seq_vehicle_transport,
            upstream_url=AUSTRALIA_SEQ_VEHICLE_POSITIONS_UPSTREAM,
            path=AUSTRALIA_SEQ_VEHICLE_POSITIONS_PATH,
            strict_static_join=True,
        )

    adelaide_city_ids, adelaide_city_regions = australia_city_configuration("adelaideMetro")
    if adelaide_city_ids:
        adelaide_trip_transport = PublicGTFSRealtimeHTTPTransport(
            "HalteWecker-AdelaideMetro-GTFSRT/1.0"
        )
        adelaide_vehicle_transport = PublicGTFSRealtimeHTTPTransport(
            "HalteWecker-AdelaideMetro-GTFSRT/1.0"
        )
        Handler.australia_adelaide_trip_updates_gateway = GTFSRealtimeTripUpdatesGateway(
            provider_id=AUSTRALIA_ADELAIDE_PROVIDER_ID,
            city_ids=adelaide_city_ids,
            context_registry=lambda city_id: database.external_gtfs_provider_contexts(
                city_id,
                AUSTRALIA_ADELAIDE_PROVIDER_ID,
            ),
            transport=adelaide_trip_transport,
            upstream_url=AUSTRALIA_ADELAIDE_TRIP_UPDATES_UPSTREAM,
            path=AUSTRALIA_ADELAIDE_TRIP_UPDATES_PATH,
            strict_static_join=True,
        )
        Handler.australia_adelaide_vehicle_positions_gateway = GTFSRealtimeVehiclePositionsGateway(
            provider_id=AUSTRALIA_ADELAIDE_PROVIDER_ID,
            city_ids=adelaide_city_ids,
            city_regions=adelaide_city_regions,
            context_registry=lambda city_id: database.external_gtfs_provider_contexts(
                city_id,
                AUSTRALIA_ADELAIDE_PROVIDER_ID,
            ),
            transport=adelaide_vehicle_transport,
            upstream_url=AUSTRALIA_ADELAIDE_VEHICLE_POSITIONS_UPSTREAM,
            path=AUSTRALIA_ADELAIDE_VEHICLE_POSITIONS_PATH,
            strict_static_join=True,
        )

    nsw_city_ids, nsw_city_regions = australia_city_configuration(
        "externalGTFS",
        provider_id=AUSTRALIA_NSW_PROVIDER_ID,
    )
    if nsw_city_ids:
        nsw_trip_transport = PublicGTFSRealtimeHTTPTransport.from_environment(
            "HalteWecker-TransportNSW-GTFSRT/1.0",
            api_key_env="NSW_API_TOKEN",
            header_name="Authorization",
            header_prefix="apikey ",
        )
        nsw_vehicle_transport = PublicGTFSRealtimeHTTPTransport.from_environment(
            "HalteWecker-TransportNSW-GTFSRT/1.0",
            api_key_env="NSW_API_TOKEN",
            header_name="Authorization",
            header_prefix="apikey ",
        )
        if nsw_trip_transport is not None and nsw_vehicle_transport is not None:
            Handler.australia_nsw_trip_updates_gateway = GTFSRealtimeTripUpdatesGateway(
                provider_id=AUSTRALIA_NSW_PROVIDER_ID,
                city_ids=nsw_city_ids,
                context_registry=lambda city_id: database.external_gtfs_provider_contexts(
                    city_id,
                    AUSTRALIA_NSW_PROVIDER_ID,
                ),
                transport=nsw_trip_transport,
                upstream_url=AUSTRALIA_NSW_TRIP_UPDATES_UPSTREAM,
                path=AUSTRALIA_NSW_TRIP_UPDATES_PATH,
                strict_static_join=True,
            )
            Handler.australia_nsw_vehicle_positions_gateway = GTFSRealtimeVehiclePositionsGateway(
                provider_id=AUSTRALIA_NSW_PROVIDER_ID,
                city_ids=nsw_city_ids,
                city_regions=nsw_city_regions,
                context_registry=lambda city_id: database.external_gtfs_provider_contexts(
                    city_id,
                    AUSTRALIA_NSW_PROVIDER_ID,
                ),
                transport=nsw_vehicle_transport,
                upstream_url=AUSTRALIA_NSW_VEHICLE_POSITIONS_UPSTREAM,
                path=AUSTRALIA_NSW_VEHICLE_POSITIONS_PATH,
                strict_static_join=True,
            )
    canberra_city_ids, canberra_city_regions = australia_city_configuration(
        "externalGTFS",
        provider_id=AUSTRALIA_CANBERRA_PROVIDER_ID,
    )
    if canberra_city_ids:
        canberra_trip_transport = PublicGTFSRealtimeHTTPTransport.from_basic_auth_environment(
            "HalteWecker-TransportCanberra-GTFSRT/1.0",
            client_id_env="CANBERRA_CLIENT_ID",
            client_secret_env="CANBERRA_CLIENT_SECRET",
        )
        canberra_vehicle_transport = PublicGTFSRealtimeHTTPTransport.from_basic_auth_environment(
            "HalteWecker-TransportCanberra-GTFSRT/1.0",
            client_id_env="CANBERRA_CLIENT_ID",
            client_secret_env="CANBERRA_CLIENT_SECRET",
        )
        if canberra_trip_transport is not None and canberra_vehicle_transport is not None:
            Handler.australia_canberra_trip_updates_gateway = GTFSRealtimeTripUpdatesGateway(
                provider_id=AUSTRALIA_CANBERRA_PROVIDER_ID,
                city_ids=canberra_city_ids,
                context_registry=lambda city_id: database.external_gtfs_provider_contexts(
                    city_id,
                    AUSTRALIA_CANBERRA_PROVIDER_ID,
                ),
                transport=canberra_trip_transport,
                upstream_url=AUSTRALIA_CANBERRA_TRIP_UPDATES_UPSTREAM,
                path=AUSTRALIA_CANBERRA_TRIP_UPDATES_PATH,
                strict_static_join=True,
            )
            Handler.australia_canberra_vehicle_positions_gateway = GTFSRealtimeVehiclePositionsGateway(
                provider_id=AUSTRALIA_CANBERRA_PROVIDER_ID,
                city_ids=canberra_city_ids,
                city_regions=canberra_city_regions,
                context_registry=lambda city_id: database.external_gtfs_provider_contexts(
                    city_id,
                    AUSTRALIA_CANBERRA_PROVIDER_ID,
                ),
                transport=canberra_vehicle_transport,
                upstream_url=AUSTRALIA_CANBERRA_VEHICLE_POSITIONS_UPSTREAM,
                path=AUSTRALIA_CANBERRA_VEHICLE_POSITIONS_PATH,
                strict_static_join=True,
            )
    queensland_manifest_payload: object = None
    for candidate in (
        Path(STATIC_DATA_ROOT) / "transit-radar-cities.json",
        Path("/data/current/transit-radar-cities.json"),
    ):
        try:
            queensland_manifest_payload = json.loads(candidate.read_text(encoding="utf-8"))
            break
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    queensland_city_configuration: dict[str, tuple[set[str], dict[str, dict[str, object]]]] = {}
    for city in (queensland_manifest_payload or {}).get("cities", []) if isinstance(queensland_manifest_payload, dict) else []:
        if not isinstance(city, dict):
            continue
        city_id = str(city.get("appCityID", "")).strip()
        providers = city.get("providers", [])
        for provider in providers if isinstance(providers, list) else []:
            if not isinstance(provider, dict) or provider.get("adapter") != "translinkQueensland":
                continue
            provider_id = str(provider.get("providerID", "")).strip()
            if not city_id or provider_id not in AUSTRALIA_QUEENSLAND_REALTIME:
                continue
            city_ids, regions = queensland_city_configuration.setdefault(
                provider_id, (set(), {})
            )
            city_ids.add(city_id)
            region = provider.get("region")
            if isinstance(region, dict):
                regions[city_id] = region
            break

    for provider_id, (city_ids, city_regions) in queensland_city_configuration.items():
        feed = AUSTRALIA_QUEENSLAND_REALTIME[provider_id]
        trip_path = f"{feed['path_prefix']}/trip-updates"
        vehicle_path = f"{feed['path_prefix']}/vehicle-positions"
        user_agent = f"HalteWecker-{provider_id}-GTFSRT/1.0"
        Handler.australia_queensland_trip_updates_gateways[provider_id] = GTFSRealtimeTripUpdatesGateway(
            provider_id=provider_id,
            city_ids=city_ids,
            context_registry=lambda city_id, provider_id=provider_id: database.external_gtfs_provider_contexts(
                city_id,
                provider_id,
            ),
            transport=PublicGTFSRealtimeHTTPTransport(user_agent),
            upstream_url=str(feed["trip_updates"]),
            path=trip_path,
            strict_static_join=True,
        )
        Handler.australia_queensland_vehicle_positions_gateways[provider_id] = GTFSRealtimeVehiclePositionsGateway(
            provider_id=provider_id,
            city_ids=city_ids,
            city_regions=city_regions,
            context_registry=lambda city_id, provider_id=provider_id: database.external_gtfs_provider_contexts(
                city_id,
                provider_id,
            ),
            transport=PublicGTFSRealtimeHTTPTransport(user_agent),
            upstream_url=str(feed["vehicle_positions"]),
            path=vehicle_path,
            strict_static_join=True,
        )
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
