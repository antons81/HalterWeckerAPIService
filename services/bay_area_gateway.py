"""Backend-only 511 Bay Area GTFS-RT gateways with bounded shared caches."""

from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Callable
from urllib.parse import urlencode

from gtfsrt_gateway import (
    GatewayResponse,
    GTFSRealtimeGateway,
    GTFSRealtimeGatewayError,
    _HTTPTransport,
    _iter_fields,
    _text,
)


BAY_AREA_PROVIDER_ID = "511-bay-area"
BAY_AREA_CITY_IDS = frozenset({"san-francisco", "oakland", "berkeley", "san-jose"})
BAY_AREA_TRIP_UPDATES_PATH = "/511/realtime/trip-updates"
BAY_AREA_VEHICLE_POSITIONS_PATH = "/511/realtime/vehicle-positions"
BAY_AREA_CACHE_TTL_SECONDS = 60.0
BAY_AREA_MAX_STALE_SECONDS = 300.0


def _upstream_url(api_key: str, endpoint: str) -> str:
    query = urlencode({"api_key": api_key, "agency": "RG"})
    return f"https://api.511.org/Transit/{endpoint}?{query}"


class BayAreaTripUpdatesProxy(GTFSRealtimeGateway):
    @classmethod
    def from_environment(
        cls,
        valid_trip_registry: Callable[[], tuple[set[str], dict[str, str]]] | None = None,
    ) -> "BayAreaTripUpdatesProxy | None":
        import os

        api_key = os.environ.get("API_511_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            provider_id=BAY_AREA_PROVIDER_ID,
            city_id="san-francisco",
            city_ids=set(BAY_AREA_CITY_IDS),
            path=BAY_AREA_TRIP_UPDATES_PATH,
            upstream_url=_upstream_url(api_key, "TripUpdates"),
            cache_ttl=BAY_AREA_CACHE_TTL_SECONDS,
            max_stale=BAY_AREA_MAX_STALE_SECONDS,
            valid_trip_registry=valid_trip_registry,
        )


@dataclass(frozen=True)
class BayAreaVehiclePosition:
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


@dataclass(frozen=True)
class _VehicleSnapshot:
    feed_timestamp: int | None
    retrieved_at: float
    entity_count: int
    vehicles: tuple[BayAreaVehiclePosition, ...]


def _float32(value: bytes) -> float:
    if len(value) != 4:
        raise GTFSRealtimeGatewayError("malformed protobuf")
    return float(struct.unpack("<f", value)[0])


def _parse_vehicle_descriptor(data: bytes) -> str:
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            return _text(value)
    return ""


def _parse_position(data: bytes) -> tuple[float | None, float | None, float | None, float | None]:
    latitude = longitude = bearing = speed = None
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 5:
            latitude = _float32(value)
        elif field == 2 and wire_type == 5:
            longitude = _float32(value)
        elif field == 3 and wire_type == 5:
            bearing = _float32(value)
        elif field == 5 and wire_type == 5:
            speed = _float32(value)
    return latitude, longitude, bearing, speed


def _parse_trip_descriptor(data: bytes) -> tuple[str, str, str | None]:
    trip_id = route_id = ""
    direction_id = None
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            trip_id = _text(value)
        elif field == 5 and wire_type == 2:
            route_id = _text(value)
        elif field == 6 and wire_type == 2:
            direction_id = _text(value)
    return trip_id, route_id, direction_id


def _parse_vehicle_position(data: bytes, fallback_vehicle_id: str) -> BayAreaVehiclePosition | None:
    trip_id = route_id = ""
    direction_id = stop_id = None
    vehicle_id = fallback_vehicle_id
    stop_sequence = timestamp = None
    latitude = longitude = bearing = speed = None
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            trip_id, route_id, direction_id = _parse_trip_descriptor(value)
        elif field == 2 and wire_type == 2:
            latitude, longitude, bearing, speed = _parse_position(value)
        elif field == 3 and wire_type == 0:
            stop_sequence = int(value)
        elif field == 5 and wire_type == 0:
            timestamp = int(value)
        elif field == 7 and wire_type == 2:
            stop_id = _text(value)
        elif field == 8 and wire_type == 2:
            candidate = _parse_vehicle_descriptor(value)
            if candidate:
                vehicle_id = candidate
    if not vehicle_id or not (trip_id or route_id):
        return None
    if latitude is None or longitude is None:
        return None
    return BayAreaVehiclePosition(
        vehicle_id=vehicle_id,
        trip_id=trip_id,
        route_id=route_id,
        direction_id=direction_id,
        stop_id=stop_id,
        stop_sequence=stop_sequence,
        latitude=latitude,
        longitude=longitude,
        bearing=bearing,
        speed=speed,
        timestamp=timestamp,
    )


def parse_vehicle_positions(data: bytes) -> tuple[int | None, int, tuple[BayAreaVehiclePosition, ...]]:
    feed_timestamp = None
    entity_count = 0
    vehicles: dict[str, BayAreaVehiclePosition] = {}
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            for header_field, header_wire_type, header_value in _iter_fields(value):
                if header_field in (3, 4) and header_wire_type == 0:
                    feed_timestamp = int(header_value)
        elif field == 2 and wire_type == 2:
            entity_count += 1
            entity_id = ""
            vehicle_payload = None
            for entity_field, entity_wire_type, entity_value in _iter_fields(value):
                if entity_field == 1 and entity_wire_type == 2:
                    entity_id = _text(entity_value)
                elif entity_field == 4 and entity_wire_type == 2:
                    vehicle_payload = entity_value
            if vehicle_payload is None:
                continue
            vehicle = _parse_vehicle_position(vehicle_payload, entity_id)
            if vehicle is not None:
                vehicles[vehicle.vehicle_id] = vehicle
    return feed_timestamp, entity_count, tuple(vehicles.values())


class BayAreaVehiclePositionsProxy:
    def __init__(
        self,
        *,
        transport: Callable[[str], bytes],
        upstream_url: str,
        valid_registry: Callable[[], tuple[set[str], set[str], dict[str, str]]] | None = None,
        clock: Callable[[], float] = time.time,
        cache_ttl: float = BAY_AREA_CACHE_TTL_SECONDS,
        max_stale: float = BAY_AREA_MAX_STALE_SECONDS,
    ) -> None:
        self._transport = transport
        self._upstream_url = upstream_url
        self._valid_registry = valid_registry
        self._clock = clock
        self._cache_ttl = cache_ttl
        self._max_stale = max_stale
        self._snapshot: _VehicleSnapshot | None = None
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    @classmethod
    def from_environment(
        cls,
        valid_registry: Callable[[], tuple[set[str], set[str], dict[str, str]]] | None = None,
    ) -> "BayAreaVehiclePositionsProxy | None":
        import os

        api_key = os.environ.get("API_511_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            transport=_HTTPTransport("HalteWecker-511-GTFSRT/1.0"),
            upstream_url=_upstream_url(api_key, "VehiclePositions"),
            valid_registry=valid_registry,
        )

    def _fetch_snapshot(self) -> _VehicleSnapshot:
        body = self._transport(self._upstream_url)
        try:
            feed_timestamp, entity_count, vehicles = parse_vehicle_positions(body)
        except GTFSRealtimeGatewayError:
            raise
        except Exception as error:
            raise GTFSRealtimeGatewayError("realtime source invalid") from error
        return _VehicleSnapshot(
            feed_timestamp=feed_timestamp,
            retrieved_at=self._clock(),
            entity_count=entity_count,
            vehicles=vehicles,
        )

    def _snapshot_for_request(self) -> tuple[_VehicleSnapshot, bool]:
        now = self._clock()
        with self._lock:
            cached = self._snapshot
            if cached is not None and now - cached.retrieved_at <= self._cache_ttl:
                return cached, False
        with self._refresh_lock:
            now = self._clock()
            with self._lock:
                cached = self._snapshot
                if cached is not None and now - cached.retrieved_at <= self._cache_ttl:
                    return cached, False
            try:
                fresh = self._fetch_snapshot()
            except Exception as error:
                if cached is not None and now - cached.retrieved_at <= self._max_stale:
                    return cached, True
                if isinstance(error, GTFSRealtimeGatewayError):
                    raise
                raise GTFSRealtimeGatewayError("realtime source unavailable") from error
            with self._lock:
                self._snapshot = fresh
            return fresh, False

    def _filtered_vehicles(self, vehicles: tuple[BayAreaVehiclePosition, ...], now: float) -> list[dict[str, object]]:
        valid_trips: set[str] | None = None
        valid_routes: set[str] | None = None
        route_by_trip: dict[str, str] = {}
        if self._valid_registry is not None:
            valid_trips, valid_routes, route_by_trip = self._valid_registry()
        result = []
        for vehicle in vehicles:
            if not (-90 <= vehicle.latitude <= 90 and -180 <= vehicle.longitude <= 180):
                continue
            if vehicle.latitude == 0 and vehicle.longitude == 0:
                continue
            if vehicle.timestamp is not None and now - vehicle.timestamp > self._max_stale:
                continue
            if valid_trips is not None and vehicle.trip_id and vehicle.trip_id not in valid_trips:
                continue
            if valid_routes is not None and vehicle.route_id and vehicle.route_id not in valid_routes:
                continue
            expected_route = route_by_trip.get(vehicle.trip_id)
            if expected_route and vehicle.route_id and expected_route != vehicle.route_id:
                continue
            result.append({
                "vehicleID": vehicle.vehicle_id,
                "tripID": vehicle.trip_id or None,
                "routeID": vehicle.route_id or None,
                "directionID": vehicle.direction_id,
                "stopID": vehicle.stop_id,
                "stopSequence": vehicle.stop_sequence,
                "latitude": vehicle.latitude,
                "longitude": vehicle.longitude,
                "bearing": vehicle.bearing,
                "speed": vehicle.speed,
                "timestamp": self._iso_time(vehicle.timestamp),
            })
        result.sort(key=lambda item: str(item["vehicleID"]))
        return result

    @staticmethod
    def _iso_time(timestamp: int | None) -> str | None:
        if timestamp is None:
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != BAY_AREA_VEHICLE_POSITIONS_PATH:
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        city_id = query.get("cityID", [None])[0]
        if city_id not in BAY_AREA_CITY_IDS:
            return GatewayResponse(HTTPStatus.BAD_REQUEST, {"error": "unsupported cityID"})
        try:
            snapshot, stale = self._snapshot_for_request()
        except GTFSRealtimeGatewayError:
            return GatewayResponse(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "realtime source unavailable"},
            )
        vehicles = self._filtered_vehicles(snapshot.vehicles, self._clock())
        return GatewayResponse(
            HTTPStatus.OK,
            {
                "schemaVersion": 1,
                "providerID": BAY_AREA_PROVIDER_ID,
                "cityID": city_id,
                "feedTimestamp": self._iso_time(snapshot.feed_timestamp),
                "retrievedAt": self._iso_time(int(snapshot.retrieved_at)),
                "stale": stale,
                "entityCount": snapshot.entity_count,
                "vehicleCount": len(vehicles),
                "vehicles": vehicles,
            },
        )
