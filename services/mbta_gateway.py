"""MBTA GTFS-Realtime gateways with strict static joins and no interpolation."""

from __future__ import annotations

import math
import os
import struct
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Callable

from gtfsrt_gateway import (
    GatewayResponse,
    GTFSRealtimeGateway,
    GTFSRealtimeGatewayError,
    RealtimeUpdate,
    _HTTPTransport,
    _iter_fields,
    _read_varint,
    _text,
)


MBTA_PROVIDER_ID = "mbta-boston"
MBTA_CITY_ID = "boston"
MBTA_NAMESPACE = "mbta-boston:"
MBTA_TRIP_UPDATES_PATH = "/mbta/realtime/trip-updates"
MBTA_VEHICLE_POSITIONS_PATH = "/mbta/realtime/vehicle-positions"
MBTA_ALERTS_PATH = "/mbta/realtime/alerts"
MBTA_TRIP_UPDATES_URL = "https://cdn.mbta.com/realtime/TripUpdates.pb"
MBTA_VEHICLE_POSITIONS_URL = "https://cdn.mbta.com/realtime/VehiclePositions.pb"
MBTA_ALERTS_URL = "https://cdn.mbta.com/realtime/Alerts.pb"
MBTA_MAX_STALE_SECONDS = 300.0
MBTA_CACHE_TTL_SECONDS = 15.0
MBTA_REGION = (-71.35, 42.15, -70.75, 42.60)


def _native(value: str) -> str:
    return value[len(MBTA_NAMESPACE):] if value.startswith(MBTA_NAMESPACE) else value


def _uint(data: bytes, field: int) -> int | None:
    for number, wire_type, value in _iter_fields(data):
        if number == field and wire_type == 0:
            return int(value)
    return None


def _float32(data: bytes) -> float | None:
    if len(data) != 4:
        return None
    return float(struct.unpack("<f", data)[0])


def _parse_stop_event(data: bytes) -> tuple[int | None, int | None]:
    event_time = delay = None
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 0:
            delay = int(value) & 0xFFFFFFFF
            if delay & 0x80000000:
                delay -= 0x100000000
        elif field == 2 and wire_type == 0:
            event_time = int(value)
    return event_time, delay


def _parse_trip(data: bytes) -> tuple[str, str, str | None, int]:
    trip_id = route_id = ""
    direction_id = None
    relationship = 0
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            trip_id = _text(value)
        elif field == 4 and wire_type == 0:
            relationship = int(value)
        elif field == 5 and wire_type == 2:
            route_id = _text(value)
        elif field == 6 and wire_type == 2:
            direction_id = _text(value)
    return trip_id, route_id, direction_id, relationship


def _parse_stop_update(data: bytes) -> tuple[str, int | None, int | None, int | None, int]:
    stop_id = ""
    sequence = None
    event_time = delay = None
    relationship = 0
    for field, wire_type, value in _iter_fields(data):
        if field == 2 and wire_type == 2:
            event_time, delay = _parse_stop_event(value)
        elif field == 3 and wire_type == 2:
            event_time, delay = _parse_stop_event(value)
        elif field == 4 and wire_type == 0:
            sequence = int(value)
        elif field == 5 and wire_type == 2:
            stop_id = _text(value)
        elif field == 6 and wire_type == 0:
            relationship = int(value)
    return stop_id, sequence, event_time, delay, relationship


def parse_mbta_trip_updates(
    data: bytes,
    stop_resolver: Callable[[set[str], set[tuple[str, int]]], dict[tuple[str, int], str]],
) -> tuple[int | None, int, tuple[RealtimeUpdate, ...]]:
    feed_timestamp = None
    entities = 0
    pending: list[tuple[str, str, str | None, int, str, int | None, int | None, int | None, int]] = []
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            feed_timestamp = _uint(value, 3)
        elif field == 2 and wire_type == 2:
            entities += 1
            trip_payload = stop_payloads = None
            for entity_field, entity_wire_type, entity_value in _iter_fields(value):
                if entity_field == 3 and entity_wire_type == 2:
                    trip_payload = entity_value
            if trip_payload is None:
                continue
            trip = None
            updates = []
            for trip_field, trip_wire_type, trip_value in _iter_fields(trip_payload):
                if trip_field == 1 and trip_wire_type == 2:
                    trip = _parse_trip(trip_value)
                elif trip_field == 2 and trip_wire_type == 2:
                    updates.append(_parse_stop_update(trip_value))
            if trip is None:
                continue
            trip_id, route_id, direction_id, relationship = trip
            if not trip_id:
                continue
            for stop_id, sequence, event_time, delay, stop_relationship in updates:
                pending.append((trip_id, route_id, direction_id, relationship, stop_id, sequence, event_time, delay, stop_relationship))
    trip_ids = {item[0] for item in pending}
    sequence_keys = {
        (item[0], item[5])
        for item in pending
        if item[1] and item[5] is not None and not item[4]
    }
    resolved = stop_resolver(trip_ids, sequence_keys)
    updates: list[RealtimeUpdate] = []
    for trip_id, route_id, direction_id, relationship, stop_id, sequence, event_time, delay, stop_relationship in pending:
        if stop_relationship in {1, 2, 3}:
            continue
        if not stop_id and sequence is not None:
            stop_id = resolved.get((trip_id, sequence), "")
        if not stop_id:
            continue
        updates.append(RealtimeUpdate(
            trip_id=trip_id,
            route_id=route_id,
            direction_id=direction_id,
            stop_id=stop_id,
            stop_sequence=sequence,
            effective_time=event_time,
            delay_seconds=delay,
            is_cancelled=relationship == 3,
        ))
    return feed_timestamp, entities, tuple(updates)


@dataclass(frozen=True)
class MBTAVehicle:
    vehicle_id: str
    trip_id: str
    route_id: str
    direction_id: str | None
    stop_id: str | None
    stop_sequence: int | None
    latitude: float
    longitude: float
    bearing: float | None
    speed: float | None
    timestamp: int | None


def _parse_vehicle_descriptor(data: bytes) -> str:
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            return _text(value)
    return ""


def _parse_position(data: bytes) -> tuple[float | None, float | None, float | None, float | None]:
    values: list[float | None] = [None, None, None, None]
    for field, wire_type, value in _iter_fields(data):
        if wire_type == 5 and field in {1, 2, 3, 5}:
            values[{1: 0, 2: 1, 3: 2, 5: 3}[field]] = _float32(value)
    return tuple(values)  # type: ignore[return-value]


def parse_mbta_vehicle_positions(data: bytes) -> tuple[int | None, int, tuple[MBTAVehicle, ...]]:
    feed_timestamp = None
    entity_count = 0
    vehicles: dict[str, MBTAVehicle] = {}
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            feed_timestamp = _uint(value, 3)
        elif field == 2 and wire_type == 2:
            entity_count += 1
            entity_id = vehicle_payload = ""
            for entity_field, entity_wire_type, entity_value in _iter_fields(value):
                if entity_field == 1 and entity_wire_type == 2:
                    entity_id = _text(entity_value)
                elif entity_field == 4 and entity_wire_type == 2:
                    vehicle_payload = entity_value
            if not vehicle_payload:
                continue
            trip_id = route_id = ""
            direction_id = stop_id = None
            sequence = timestamp = None
            latitude = longitude = bearing = speed = None
            vehicle_id = entity_id
            for vehicle_field, vehicle_wire_type, vehicle_value in _iter_fields(vehicle_payload):
                if vehicle_field == 1 and vehicle_wire_type == 2:
                    trip_id, route_id, direction_id, _ = _parse_trip(vehicle_value)
                elif vehicle_field == 2 and vehicle_wire_type == 2:
                    latitude, longitude, bearing, speed = _parse_position(vehicle_value)
                elif vehicle_field == 3 and vehicle_wire_type == 0:
                    sequence = int(vehicle_value)
                elif vehicle_field == 5 and vehicle_wire_type == 0:
                    timestamp = int(vehicle_value)
                elif vehicle_field == 7 and vehicle_wire_type == 2:
                    stop_id = _text(vehicle_value)
                elif vehicle_field == 8 and vehicle_wire_type == 2:
                    vehicle_id = _parse_vehicle_descriptor(vehicle_value) or vehicle_id
            if not vehicle_id or not trip_id or latitude is None or longitude is None:
                continue
            vehicles[vehicle_id] = MBTAVehicle(vehicle_id, trip_id, route_id, direction_id, stop_id, sequence, latitude, longitude, bearing, speed, timestamp)
    return feed_timestamp, entity_count, tuple(vehicles.values())


class MBTATripUpdatesGateway(GTFSRealtimeGateway):
    def __init__(self, *, trip_stop_resolver, **kwargs):
        self._trip_stop_resolver = trip_stop_resolver
        super().__init__(**kwargs)

    def _fetch_snapshot(self):
        url = self._upstream_url() if callable(self._upstream_url) else self._upstream_url
        try:
            body = self._transport(url)
            timestamp, entities, updates = parse_mbta_trip_updates(body, self._trip_stop_resolver)
        except GTFSRealtimeGatewayError:
            raise
        except Exception as error:
            raise GTFSRealtimeGatewayError("realtime source invalid") from error
        from gtfsrt_gateway import _Snapshot
        return _Snapshot(timestamp, self._clock(), entities, updates)


class MBTAVehiclePositionsGateway:
    def __init__(self, *, valid_registry, transport=None, clock=time.time):
        self._valid_registry = valid_registry
        self._transport = transport or _HTTPTransport("HalteWecker-MBTA-GTFSRT/1.0")
        self._clock = clock
        self._snapshot = None
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    def _snapshot_for_request(self):
        now = self._clock()
        with self._lock:
            if self._snapshot and now - self._snapshot[1] <= MBTA_CACHE_TTL_SECONDS:
                return self._snapshot, False
        with self._refresh_lock:
            with self._lock:
                if self._snapshot and now - self._snapshot[1] <= MBTA_CACHE_TTL_SECONDS:
                    return self._snapshot, False
            try:
                timestamp, entities, vehicles = parse_mbta_vehicle_positions(self._transport(MBTA_VEHICLE_POSITIONS_URL))
            except Exception as error:
                if self._snapshot and now - self._snapshot[1] <= MBTA_MAX_STALE_SECONDS:
                    return self._snapshot, True
                raise GTFSRealtimeGatewayError("realtime source unavailable") from error
            self._snapshot = ((timestamp, entities, vehicles), self._clock())
            return self._snapshot, False

    @staticmethod
    def _iso(timestamp: int | None) -> str | None:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp)) if timestamp is not None else None

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != MBTA_VEHICLE_POSITIONS_PATH:
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        if query.get("cityID", [None])[0] != MBTA_CITY_ID:
            return GatewayResponse(HTTPStatus.BAD_REQUEST, {"error": "unsupported cityID"})
        try:
            snapshot, stale = self._snapshot_for_request()
        except GTFSRealtimeGatewayError:
            return GatewayResponse(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "realtime source unavailable"})
        (feed_timestamp, entity_count, vehicles), retrieved_at = snapshot
        valid_trips, valid_routes, route_by_trip, stop_mapper = self._valid_registry()
        now = self._clock()
        result = []
        for vehicle in vehicles:
            if not math.isfinite(vehicle.latitude) or not math.isfinite(vehicle.longitude):
                continue
            if not (-90 <= vehicle.latitude <= 90 and -180 <= vehicle.longitude <= 180):
                continue
            if vehicle.latitude == 0 and vehicle.longitude == 0:
                continue
            if not (MBTA_REGION[0] <= vehicle.longitude <= MBTA_REGION[2] and MBTA_REGION[1] <= vehicle.latitude <= MBTA_REGION[3]):
                continue
            if vehicle.timestamp is not None and now - vehicle.timestamp > MBTA_MAX_STALE_SECONDS:
                continue
            if vehicle.trip_id not in valid_trips or vehicle.route_id not in valid_routes:
                continue
            if route_by_trip.get(vehicle.trip_id) != vehicle.route_id:
                continue
            result.append({
                "vehicleID": vehicle.vehicle_id,
                "tripID": vehicle.trip_id,
                "routeID": vehicle.route_id,
                "directionID": vehicle.direction_id,
                "stopID": stop_mapper(vehicle.stop_id) if vehicle.stop_id else None,
                "stopSequence": vehicle.stop_sequence,
                "latitude": vehicle.latitude,
                "longitude": vehicle.longitude,
                "bearing": vehicle.bearing,
                "speed": vehicle.speed,
                "timestamp": self._iso(vehicle.timestamp),
            })
        result.sort(key=lambda item: str(item["vehicleID"]))
        return GatewayResponse(HTTPStatus.OK, {"schemaVersion": 1, "providerID": MBTA_PROVIDER_ID, "cityID": MBTA_CITY_ID, "feedTimestamp": self._iso(feed_timestamp), "retrievedAt": self._iso(int(retrieved_at)), "stale": stale, "entityCount": entity_count, "vehicleCount": len(result), "vehicles": result})


class MBTAAlertsGateway:
    def __init__(self, transport=None):
        self._transport = transport or _HTTPTransport("HalteWecker-MBTA-GTFSRT/1.0")

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != MBTA_ALERTS_PATH:
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        if query.get("cityID", [None])[0] != MBTA_CITY_ID:
            return GatewayResponse(HTTPStatus.BAD_REQUEST, {"error": "unsupported cityID"})
        try:
            body = self._transport(MBTA_ALERTS_URL)
            header = entities = None
            alerts = []
            for field, wire_type, value in _iter_fields(body):
                if field == 1 and wire_type == 2:
                    header = _uint(value, 3)
                elif field == 2 and wire_type == 2:
                    entities = (entities or 0) + 1
                    alert_payload = next((v for n, w, v in _iter_fields(value) if n == 5 and w == 2), None)
                    if alert_payload is None:
                        continue
                    routes = []
                    for n, w, informed in _iter_fields(alert_payload):
                        if n != 5 or w != 2:
                            continue
                        route = next((v for nn, ww, v in _iter_fields(informed) if nn == 2 and ww == 2), b"")
                        if route:
                            routes.append(_text(route))
                    alerts.append({"routeIDs": sorted(set(routes)), "stopIDs": []})
            return GatewayResponse(HTTPStatus.OK, {"schemaVersion": 1, "providerID": MBTA_PROVIDER_ID, "cityID": MBTA_CITY_ID, "feedTimestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(header)) if header else None, "entityCount": entities or 0, "alerts": alerts})
        except Exception as error:
            raise GTFSRealtimeGatewayError("realtime source unavailable") from error
