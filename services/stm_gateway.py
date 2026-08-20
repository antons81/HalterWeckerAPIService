"""Server-side Montréal STM GTFS-Realtime and service-status gateway."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import struct
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gtfsrt_gateway import (
    GatewayResponse,
    GTFSRealtimeGatewayError,
    _iter_fields,
    _text,
)


LOGGER = logging.getLogger("haltewecker.stm_gateway")

STM_PROVIDER_ID = "stm-montreal"
STM_CITY_ID = "montreal"
STM_NAMESPACE = "stm-montreal:"
STM_TRIP_UPDATES_PATH = "/stm-montreal/realtime/trip-updates"
STM_VEHICLE_POSITIONS_PATH = "/stm-montreal/realtime/vehicle-positions"
STM_ALERTS_PATH = "/stm-montreal/realtime/alerts"
STM_TRIP_UPDATES_URL = "https://api.stm.info/pub/od/gtfs-rt/ic/v2/tripUpdates"
STM_VEHICLE_POSITIONS_URL = "https://api.stm.info/pub/od/gtfs-rt/ic/v2/vehiclePositions"
STM_ALERTS_URL = "https://api.stm.info/pub/od/i3/v2/messages/etatservice"
STM_POLL_INTERVAL_SECONDS = 30.0
STM_ALERTS_POLL_INTERVAL_SECONDS = 60.0
STM_MAX_STALE_SECONDS = 300.0
STM_USER_AGENT = "HalteWecker-STM-GTFSRT/1.0"

TripRegistry = Callable[
    [],
    tuple[set[str], set[str], dict[str, str], dict[str, str]],
]


def _uint(data: bytes, field: int) -> int | None:
    for number, wire_type, value in _iter_fields(data):
        if number == field and wire_type == 0:
            return int(value)
    return None


def _string(data: bytes, field: int) -> str:
    for number, wire_type, value in _iter_fields(data):
        if number == field and wire_type == 2:
            return _text(value)
    return ""


def _signed_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _iso(timestamp: int | float | None) -> str | None:
    if timestamp is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _native(value: str) -> str:
    return value[len(STM_NAMESPACE):] if value.startswith(STM_NAMESPACE) else value


def _relationship_name(value: int | None, values: dict[int, str]) -> str:
    return values.get(value if value is not None else 0, "UNKNOWN")


TRIP_RELATIONSHIPS = {
    0: "SCHEDULED",
    1: "ADDED",
    2: "UNSCHEDULED",
    3: "CANCELED",
    5: "DUPLICATED",
    6: "DELETED",
}
STOP_RELATIONSHIPS = {
    0: "SCHEDULED",
    1: "SKIPPED",
    2: "NO_DATA",
    3: "UNSCHEDULED",
}


def _parse_stop_event(data: bytes) -> tuple[int | None, int | None]:
    event_time = delay = None
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 0:
            delay = _signed_int32(int(value))
        elif field == 2 and wire_type == 0:
            event_time = int(value)
    return event_time, delay


def _parse_trip_descriptor(data: bytes) -> tuple[str, str, str | None, int]:
    trip_id = route_id = ""
    direction_id: str | None = None
    relationship = 0
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            trip_id = _text(value)
        elif field == 4 and wire_type == 0:
            relationship = int(value)
        elif field == 5 and wire_type == 2:
            route_id = _text(value)
        elif field == 6:
            direction_id = str(int(value)) if wire_type == 0 else _text(value)
    return trip_id, route_id, direction_id, relationship


def _parse_stop_time_update(
    data: bytes,
) -> tuple[str, int | None, int | None, int | None, int]:
    stop_id = ""
    sequence = None
    event_time = delay = None
    relationship = 0
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 0:
            sequence = int(value)
        elif field in (2, 3) and wire_type == 2:
            candidate_time, candidate_delay = _parse_stop_event(value)
            if candidate_time is not None and (event_time is None or field == 3):
                event_time = candidate_time
            if candidate_delay is not None and (delay is None or field == 3):
                delay = candidate_delay
        elif field == 4 and wire_type == 2:
            stop_id = _text(value)
        elif field == 5 and wire_type == 0:
            relationship = int(value)
    return stop_id, sequence, event_time, delay, relationship


def parse_stm_trip_updates(data: bytes, now: float | None = None) -> dict[str, object]:
    """Parse STM's GTFS-RT TripUpdates protobuf without trusting payload IDs."""
    current = time.time() if now is None else now
    feed_timestamp = None
    entity_count = 0
    malformed_entities = 0
    stale_updates = skipped_updates = no_data_updates = unknown_relationships = 0
    updates: list[dict[str, object]] = []

    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            feed_timestamp = _uint(value, 3)
            continue
        elif field != 2 or wire_type != 2:
            continue
        entity_count += 1
        try:
            trip_update_payload = next(
                (
                    nested
                    for entity_field, entity_wire, nested in _iter_fields(value)
                    if entity_field == 3 and entity_wire == 2
                ),
                None,
            )
            if trip_update_payload is None:
                malformed_entities += 1
                continue
            trip_descriptor = None
            stop_updates: list[tuple[str, int | None, int | None, int | None, int]] = []
            trip_update_timestamp = None
            for update_field, update_wire, update_value in _iter_fields(trip_update_payload):
                if update_field == 1 and update_wire == 2:
                    trip_descriptor = _parse_trip_descriptor(update_value)
                elif update_field == 2 and update_wire == 2:
                    stop_updates.append(_parse_stop_time_update(update_value))
                elif update_field == 4 and update_wire == 0:
                    trip_update_timestamp = int(update_value)
            if trip_descriptor is None or not trip_descriptor[0]:
                malformed_entities += 1
                continue
            trip_id, route_id, direction_id, trip_relationship = trip_descriptor
            trip_relationship_name = _relationship_name(
                trip_relationship,
                TRIP_RELATIONSHIPS,
            )
            if trip_relationship_name not in {"SCHEDULED", "CANCELED"}:
                unknown_relationships += len(stop_updates)
                continue
            if trip_update_timestamp is None or current - trip_update_timestamp > STM_MAX_STALE_SECONDS:
                stale_updates += len(stop_updates)
                continue
            for stop_id, sequence, event_time, delay, stop_relationship in stop_updates:
                stop_relationship_name = _relationship_name(
                    stop_relationship,
                    STOP_RELATIONSHIPS,
                )
                if stop_relationship_name == "SKIPPED":
                    skipped_updates += 1
                    continue
                if stop_relationship_name == "NO_DATA":
                    no_data_updates += 1
                    continue
                if stop_relationship_name != "SCHEDULED":
                    unknown_relationships += 1
                    continue
                if not stop_id:
                    continue
                updates.append(
                    {
                        "tripID": trip_id,
                        "routeID": route_id,
                        "directionID": direction_id,
                        "stopID": stop_id,
                        "stopSequence": sequence,
                        "effectiveTime": _iso(event_time),
                        "delaySeconds": delay,
                        "isCancelled": trip_relationship_name == "CANCELED",
                        "scheduleRelationship": trip_relationship_name,
                        "tripUpdateTimestamp": _iso(trip_update_timestamp),
                    }
                )
        except (GTFSRealtimeGatewayError, UnicodeError, ValueError, StopIteration):
            malformed_entities += 1

    return {
        "feedTimestamp": feed_timestamp,
        "entityCount": entity_count,
        "malformedEntityCount": malformed_entities,
        "staleUpdateCount": stale_updates,
        "skippedUpdateCount": skipped_updates,
        "noDataUpdateCount": no_data_updates,
        "unknownRelationshipCount": unknown_relationships,
        "updates": updates,
    }


def _float32(value: bytes) -> float | None:
    if len(value) != 4:
        return None
    return float(struct.unpack("<f", value)[0])


def _parse_position(data: bytes) -> tuple[float | None, float | None, float | None, float | None]:
    values: dict[int, float | None] = {1: None, 2: None, 3: None, 5: None}
    for field, wire_type, value in _iter_fields(data):
        if field in values and wire_type == 5:
            values[field] = _float32(value)
    return values[1], values[2], values[3], values[5]


def _parse_vehicle_descriptor(data: bytes) -> str:
    return _string(data, 1)


def parse_stm_vehicle_positions(data: bytes) -> dict[str, object]:
    """Parse STM VehiclePositions using the standard little-endian float fields."""
    feed_timestamp = None
    entity_count = 0
    malformed_entities = 0
    duplicate_entity_count = 0
    vehicles: dict[str, dict[str, object]] = {}

    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            feed_timestamp = _uint(value, 3)
            continue
        elif field != 2 or wire_type != 2:
            continue
        entity_count += 1
        try:
            entity_id = _string(value, 1)
            vehicle_payload = next(
                (
                    nested
                    for entity_field, entity_wire, nested in _iter_fields(value)
                    if entity_field == 4 and entity_wire == 2
                ),
                None,
            )
            if vehicle_payload is None:
                malformed_entities += 1
                continue
            trip_id = route_id = ""
            stop_id = vehicle_id = ""
            sequence = timestamp = None
            latitude = longitude = bearing = speed = None
            for vehicle_field, vehicle_wire, vehicle_value in _iter_fields(vehicle_payload):
                if vehicle_field == 1 and vehicle_wire == 2:
                    trip_id, route_id, _direction, _relationship = _parse_trip_descriptor(vehicle_value)
                elif vehicle_field == 2 and vehicle_wire == 2:
                    latitude, longitude, bearing, speed = _parse_position(vehicle_value)
                elif vehicle_field == 3 and vehicle_wire == 0:
                    sequence = int(vehicle_value)
                elif vehicle_field == 5 and vehicle_wire == 0:
                    timestamp = int(vehicle_value)
                elif vehicle_field == 7 and vehicle_wire == 2:
                    stop_id = _text(vehicle_value)
                elif vehicle_field == 8 and vehicle_wire == 2:
                    vehicle_id = _parse_vehicle_descriptor(vehicle_value)
            key = vehicle_id or entity_id
            if not key:
                malformed_entities += 1
                continue
            if key in vehicles:
                duplicate_entity_count += 1
                continue
            vehicles[key] = {
                "entityID": entity_id,
                "vehicleID": key,
                "tripID": trip_id,
                "routeID": route_id,
                "stopID": stop_id or None,
                "stopSequence": sequence,
                "latitude": latitude,
                "longitude": longitude,
                "bearing": bearing,
                "speed": speed,
                "timestamp": timestamp,
            }
        except (GTFSRealtimeGatewayError, UnicodeError, ValueError):
            malformed_entities += 1

    return {
        "feedTimestamp": feed_timestamp,
        "entityCount": entity_count,
        "malformedEntityCount": malformed_entities,
        "duplicateEntityCount": duplicate_entity_count,
        "vehicles": list(vehicles.values()),
    }


def parse_stm_alerts(data: bytes) -> dict[str, object]:
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise GTFSRealtimeGatewayError("invalid STM alerts payload")
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise GTFSRealtimeGatewayError("invalid STM alerts list")
    return {
        "feedTimestamp": (payload.get("header") or {}).get("timestamp"),
        "entityCount": len(alerts),
        "alerts": [item for item in alerts if isinstance(item, dict)],
        "malformedAlertCount": len(alerts) - sum(isinstance(item, dict) for item in alerts),
    }


class _STMTransport:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("STM API key is required")
        self._api_key = api_key

    def fetch(
        self,
        url: str,
        accept: str,
        etag: str | None = None,
    ) -> tuple[bytes, str | None, bool]:
        headers = {
            "Accept": accept,
            "User-Agent": STM_USER_AGENT,
            "apiKey": self._api_key,
        }
        if etag:
            headers["If-None-Match"] = etag
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=15) as response:
                return response.read(), response.headers.get("ETag"), False
        except HTTPError as error:
            if error.code == HTTPStatus.NOT_MODIFIED:
                return b"", error.headers.get("ETag") or etag, True
            if error.code == HTTPStatus.TOO_MANY_REQUESTS:
                retry_after = None
                try:
                    retry_after = float(error.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    pass
                raise STMRateLimitError(retry_after) from error
            raise GTFSRealtimeGatewayError("STM upstream unavailable") from error
        except (URLError, TimeoutError, OSError) as error:
            raise GTFSRealtimeGatewayError("STM upstream unavailable") from error


class STMRateLimitError(GTFSRealtimeGatewayError):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("STM upstream rate limited")
        self.retry_after = retry_after


@dataclass(frozen=True)
class _StoredSnapshot:
    payload: dict[str, object]
    retrieved_at: float
    etag: str | None = None


class STMRealtimePoller:
    """Single server-side poller shared by all normalized STM endpoints."""

    def __init__(
        self,
        api_key: str,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        start: bool = False,
        transport: _STMTransport | None = None,
    ) -> None:
        self._clock = clock
        self._monotonic = monotonic
        self._transport = transport or _STMTransport(api_key)
        self._snapshots: dict[str, _StoredSnapshot] = {}
        self._etag: str | None = None
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._backoff_until: dict[str, float] = {}
        self._backoff_seconds: dict[str, float] = {}
        if start:
            self.start()

    def snapshot(self, kind: str) -> _StoredSnapshot | None:
        with self._lock:
            return self._snapshots.get(kind)

    def _store(self, kind: str, payload: dict[str, object], etag: str | None = None) -> None:
        with self._lock:
            self._snapshots[kind] = _StoredSnapshot(payload, self._clock(), etag)

    def refresh_once(self, kind: str) -> bool:
        with self._refresh_lock:
            if kind == "trip":
                body, _etag, _not_modified = self._transport.fetch(
                    STM_TRIP_UPDATES_URL,
                    "application/x-protobuf",
                )
                self._store("trip", parse_stm_trip_updates(body, self._clock()))
                return True
            if kind == "vehicle":
                body, _etag, _not_modified = self._transport.fetch(
                    STM_VEHICLE_POSITIONS_URL,
                    "application/x-protobuf",
                )
                self._store("vehicle", parse_stm_vehicle_positions(body))
                return True
            if kind == "alerts":
                previous = self.snapshot("alerts")
                body, etag, not_modified = self._transport.fetch(
                    STM_ALERTS_URL,
                    "application/json",
                    self._etag,
                )
                if not_modified and previous is not None:
                    self._store("alerts", previous.payload, etag or self._etag)
                    self._etag = etag or self._etag
                    return True
                self._store("alerts", parse_stm_alerts(body), etag)
                self._etag = etag
                return True
            raise ValueError(f"unknown STM snapshot kind: {kind}")

    def _safe_refresh(self, kind: str) -> None:
        now = self._monotonic()
        if now < self._backoff_until.get(kind, 0.0):
            return
        try:
            self.refresh_once(kind)
            self._backoff_seconds.pop(kind, None)
            self._backoff_until.pop(kind, None)
        except STMRateLimitError as error:
            previous = self._backoff_seconds.get(kind, STM_POLL_INTERVAL_SECONDS)
            delay = error.retry_after or previous
            delay = min(max(delay, STM_POLL_INTERVAL_SECONDS), 300.0)
            self._backoff_seconds[kind] = min(delay * 2.0, 300.0)
            self._backoff_until[kind] = now + delay
            LOGGER.warning("STM upstream rate limited kind=%s retry_seconds=%s", kind, int(delay))
        except Exception:
            LOGGER.warning("STM poll failed kind=%s", kind)

    def refresh_all(self) -> None:
        for kind in ("trip", "vehicle", "alerts"):
            self._safe_refresh(kind)

    def _run(self) -> None:
        now = self._monotonic()
        due = {"trip": now, "vehicle": now, "alerts": now}
        intervals = {
            "trip": STM_POLL_INTERVAL_SECONDS,
            "vehicle": STM_POLL_INTERVAL_SECONDS,
            "alerts": STM_ALERTS_POLL_INTERVAL_SECONDS,
        }
        while not self._stop_event.is_set():
            now = self._monotonic()
            for kind, interval in intervals.items():
                if now >= due[kind]:
                    self._safe_refresh(kind)
                    due[kind] = self._monotonic() + interval
            wait = max(0.25, min(1.0, min(due.values()) - self._monotonic()))
            self._stop_event.wait(wait)

    def start(self) -> None:
        if self._thread is not None:
            return
        self.refresh_all()
        self._thread = threading.Thread(
            target=self._run,
            name="stm-realtime-poller",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


class STMRealtimeGateway:
    def __init__(
        self,
        *,
        poller: STMRealtimePoller,
        valid_registry: TripRegistry,
        valid_stop_registry: Callable[[], set[str]],
        public_stop_registry: Callable[[], set[str]],
        stop_selector: Callable[[set[str]], set[str]],
        route_short_registry: Callable[[], dict[str, set[str]]],
        stop_code_registry: Callable[[], dict[str, set[str]]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._poller = poller
        self._valid_registry = valid_registry
        self._valid_stop_registry = valid_stop_registry
        self._public_stop_registry = public_stop_registry
        self._stop_selector = stop_selector
        self._route_short_registry = route_short_registry
        self._stop_code_registry = stop_code_registry
        self._clock = clock

    @staticmethod
    def _requested_stop_ids(query: dict[str, list[str]]) -> list[str]:
        values = query.get("stopIDs", []) + query.get("stopID", [])
        result: list[str] = []
        for value in values:
            result.extend(part.strip() for part in value.split(",") if part.strip())
        result = list(dict.fromkeys(result))
        if not result or len(result) > 64:
            raise GTFSRealtimeGatewayError("invalid stop selection")
        return result

    def _base_payload(
        self,
        snapshot: _StoredSnapshot,
        requested_city_id: str,
    ) -> dict[str, object]:
        payload = dict(snapshot.payload)
        payload.update(
            {
                "schemaVersion": 1,
                "providerID": STM_PROVIDER_ID,
                "cityID": requested_city_id,
                "feedTimestamp": _iso(payload.get("feedTimestamp")),
                "retrievedAt": _iso(snapshot.retrieved_at),
                "stale": self._clock() - snapshot.retrieved_at > STM_MAX_STALE_SECONDS,
            }
        )
        return payload

    def _trip_updates(
        self,
        snapshot: _StoredSnapshot,
        query: dict[str, list[str]],
    ) -> GatewayResponse:
        try:
            requested = self._requested_stop_ids(query)
        except GTFSRealtimeGatewayError:
            return GatewayResponse(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid stop selection"},
            )
        public_stops = self._public_stop_registry()
        unknown_stops = sorted(set(requested) - public_stops)
        if unknown_stops:
            return GatewayResponse(
                HTTPStatus.NOT_FOUND,
                {"error": "unknown cityID/stopID"},
            )
        selected_stops = self._stop_selector(set(requested))
        valid_trips, valid_routes, route_by_trip, route_types = self._valid_registry()
        valid_stops = self._valid_stop_registry()
        payload = self._base_payload(snapshot, STM_CITY_ID)
        updates = []
        unmatched_trip = unmatched_route = unmatched_stop = 0
        metro_excluded = 0
        for item in payload.get("updates", []):
            if not isinstance(item, dict) or str(item.get("stopID", "")) not in selected_stops:
                continue
            trip_id = str(item.get("tripID", ""))
            route_id = str(item.get("routeID", ""))
            stop_id = str(item.get("stopID", ""))
            if trip_id not in valid_trips:
                unmatched_trip += 1
                continue
            expected_route = route_by_trip.get(trip_id)
            if expected_route is None or route_id not in valid_routes:
                unmatched_route += 1
                continue
            if route_id != expected_route:
                unmatched_route += 1
                continue
            if route_types.get(route_id) != "3":
                metro_excluded += 1
                continue
            if stop_id not in valid_stops:
                unmatched_stop += 1
                continue
            normalized = dict(item)
            normalized["stopID"] = _native(stop_id)
            normalized["tripID"] = _native(trip_id)
            normalized["routeID"] = _native(route_id)
            updates.append(normalized)
        updates.sort(
            key=lambda item: (
                str(item.get("effectiveTime") or ""),
                str(item.get("routeID") or ""),
                str(item.get("tripID") or ""),
            )
        )
        payload.update(
            {
                "stopIDs": requested,
                "matchedStopIDs": sorted(selected_stops),
                "routeCount": len({str(item["routeID"]) for item in updates}),
                "updateCount": len(updates),
                "unmatchedTripCount": unmatched_trip,
                "unmatchedRouteCount": unmatched_route,
                "unmatchedStopCount": unmatched_stop,
                "metroExcludedCount": metro_excluded,
                "updates": updates,
            }
        )
        return GatewayResponse(
            HTTPStatus.OK,
            payload,
            "public, max-age=5, stale-while-revalidate=25",
        )

    def _vehicle_positions(self, snapshot: _StoredSnapshot) -> GatewayResponse:
        payload = self._base_payload(snapshot, STM_CITY_ID)
        valid_trips, valid_routes, route_by_trip, route_types = self._valid_registry()
        valid_stops = self._valid_stop_registry()
        now = self._clock()
        vehicles = []
        stale_count = unmatched_trip = unmatched_route = 0
        unmatched_stop = 0
        metro_count = 0
        for item in payload.get("vehicles", []):
            if not isinstance(item, dict):
                continue
            route_id = str(item.get("routeID", ""))
            if route_types.get(route_id) != "3":
                if route_types.get(route_id) in {"1", "2", "4", "5"}:
                    metro_count += 1
                continue
            latitude = item.get("latitude")
            longitude = item.get("longitude")
            if (
                not isinstance(latitude, (int, float))
                or not isinstance(longitude, (int, float))
                or not math.isfinite(float(latitude))
                or not math.isfinite(float(longitude))
                or not (-90 <= latitude <= 90 and -180 <= longitude <= 180)
                or (latitude == 0 and longitude == 0)
            ):
                continue
            trip_id = str(item.get("tripID", ""))
            if trip_id not in valid_trips:
                unmatched_trip += 1
                continue
            if route_id not in valid_routes or route_by_trip.get(trip_id) != route_id:
                unmatched_route += 1
                continue
            timestamp = item.get("timestamp")
            timestamp_value = int(timestamp) if isinstance(timestamp, (int, float)) else None
            if timestamp_value is None or now - timestamp_value > STM_MAX_STALE_SECONDS:
                stale_count += 1
                continue
            stop_id = item.get("stopID")
            if stop_id and str(stop_id) not in valid_stops:
                unmatched_stop += 1
                stop_id = None
            normalized = dict(item)
            normalized.update(
                {
                    "vehicleID": _native(str(item.get("vehicleID", ""))),
                    "tripID": _native(trip_id),
                    "routeID": _native(route_id),
                    "stopID": _native(str(stop_id)) if stop_id else None,
                    "mode": "bus",
                }
            )
            vehicles.append(normalized)
        vehicles.sort(key=lambda item: str(item.get("vehicleID", "")))
        payload.update(
            {
                "vehicleCount": len(vehicles),
                "strictEligibleCount": len(vehicles),
                "staleVehicleCount": stale_count,
                "unmatchedTripCount": unmatched_trip,
                "unmatchedRouteCount": unmatched_route,
                "unmatchedStopCount": unmatched_stop,
                "metroEntityCount": metro_count,
                "interpolation": False,
                "vehicles": vehicles,
            }
        )
        return GatewayResponse(
            HTTPStatus.OK,
            payload,
            "public, max-age=5, stale-while-revalidate=25",
        )

    @staticmethod
    def _localized_texts(values: object) -> tuple[list[dict[str, str]], dict[str, str]]:
        entries: list[dict[str, str]] = []
        by_language: dict[str, str] = {}
        if isinstance(values, list):
            for item in values:
                if not isinstance(item, dict):
                    continue
                language = str(item.get("language", "")).strip()
                text = str(item.get("text", "")).strip()
                if not language or not text:
                    continue
                entry = {"language": language, "text": text}
                entries.append(entry)
                by_language[language] = text
        return entries, by_language

    def _alerts(self, snapshot: _StoredSnapshot) -> GatewayResponse:
        payload = self._base_payload(snapshot, STM_CITY_ID)
        route_map = self._route_short_registry()
        stop_map = self._stop_code_registry()
        normalized_alerts = []
        unmatched_route_selectors = unmatched_stop_selectors = 0
        for raw in payload.get("alerts", []):
            if not isinstance(raw, dict):
                continue
            route_ids: set[str] = set()
            stop_ids: set[str] = set()
            direction_ids: set[str] = set()
            unmapped: list[dict[str, str]] = []
            informed = raw.get("informed_entities")
            for selector in informed if isinstance(informed, list) else []:
                if not isinstance(selector, dict):
                    continue
                if "route_short_name" in selector:
                    value = str(selector.get("route_short_name", "")).strip()
                    mapped = route_map.get(value, set())
                    route_ids.update(mapped)
                    if not mapped:
                        unmatched_route_selectors += 1
                        unmapped.append({"type": "route_short_name", "value": value})
                if "stop_code" in selector:
                    value = str(selector.get("stop_code", "")).strip()
                    mapped = stop_map.get(value, set())
                    stop_ids.update(mapped)
                    if not mapped:
                        unmatched_stop_selectors += 1
                        unmapped.append({"type": "stop_code", "value": value})
                if "direction_id" in selector:
                    direction_ids.add(str(selector.get("direction_id", "")))
            header_texts, title = self._localized_texts(raw.get("header_texts"))
            description_texts, description = self._localized_texts(
                raw.get("description_texts")
            )
            identity = json.dumps(
                {
                    "periods": raw.get("active_periods"),
                    "entities": informed,
                    "title": header_texts,
                    "description": description_texts,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            normalized_alerts.append(
                {
                    "alertID": hashlib.sha256(identity).hexdigest()[:24],
                    "activePeriods": (
                        [raw["active_periods"]]
                        if isinstance(raw.get("active_periods"), dict)
                        else raw.get("active_periods", [])
                    ),
                    "routeIDs": sorted(_native(value) for value in route_ids),
                    "stopIDs": sorted(_native(value) for value in stop_ids),
                    "directionIDs": sorted(direction_ids),
                    "title": title,
                    "description": description,
                    "headerTexts": header_texts,
                    "descriptionTexts": description_texts,
                    "cause": raw.get("cause"),
                    "effect": raw.get("effect"),
                    "providerSelectors": informed if isinstance(informed, list) else [],
                    "unmappedSelectors": unmapped,
                }
            )
        payload.update(
            {
                "alertCount": len(normalized_alerts),
                "matchedRouteSelectorCount": sum(
                    bool(alert.get("routeIDs")) for alert in normalized_alerts
                ),
                "matchedStopSelectorCount": sum(
                    bool(alert.get("stopIDs")) for alert in normalized_alerts
                ),
                "unmatchedRouteSelectorCount": unmatched_route_selectors,
                "unmatchedStopSelectorCount": unmatched_stop_selectors,
                "alerts": normalized_alerts,
            }
        )
        return GatewayResponse(
            HTTPStatus.OK,
            payload,
            "public, max-age=15, stale-while-revalidate=45",
        )

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if query.get("cityID", [None])[0] != STM_CITY_ID:
            return GatewayResponse(
                HTTPStatus.BAD_REQUEST,
                {"error": "unsupported cityID"},
            )
        if path == STM_TRIP_UPDATES_PATH:
            snapshot = self._poller.snapshot("trip")
            if snapshot is None:
                return GatewayResponse(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "STM realtime source unavailable"},
                )
            return self._trip_updates(snapshot, query)
        if path == STM_VEHICLE_POSITIONS_PATH:
            snapshot = self._poller.snapshot("vehicle")
            if snapshot is None:
                return GatewayResponse(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "STM realtime source unavailable"},
                )
            return self._vehicle_positions(snapshot)
        if path == STM_ALERTS_PATH:
            snapshot = self._poller.snapshot("alerts")
            if snapshot is None:
                return GatewayResponse(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "STM alerts source unavailable"},
                )
            return self._alerts(snapshot)
        return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
