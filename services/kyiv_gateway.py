"""Kyiv GTFS-Realtime VehiclePositions gateway with static-route validation."""

from __future__ import annotations

import math
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

from gtfsrt_gateway import (
    GatewayResponse,
    GTFSRealtimeGatewayError,
    _HTTPTransport,
    _iter_fields,
    _text,
)
from kyiv_radar_inference import (
    KyivDirectionInference,
    KyivRadarTopology,
    KyivVehicleSample,
)

KYIV_PROVIDER_ID = "kyiv"
KYIV_CITY_ID = "kyiv"
KYIV_NAMESPACE = "kyiv:"
KYIV_VEHICLE_POSITIONS_PATH = "/kyiv/realtime/vehicle-positions"
KYIV_UPSTREAM_URL = "http://193.23.225.214:732/api/realtime"
KYIV_REGION = (29.8, 50.1, 31.2, 50.9)
KYIV_SUPPORTED_ROUTE_TYPES = frozenset({"0", "3", "11"})
KYIV_DISALLOWED_ROUTE_IDS = frozenset({"255", "256", "257"})
KYIV_CACHE_TTL_SECONDS = 15.0
KYIV_MAX_STALE_SECONDS = 300.0


@dataclass(frozen=True)
class KyivVehiclePosition:
    vehicle_id: str
    trip_id: str | None
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
    vehicles: tuple[KyivVehiclePosition, ...]


def _float32(value: bytes) -> float:
    if len(value) != 4:
        raise GTFSRealtimeGatewayError("malformed protobuf")
    return float(struct.unpack("<f", value)[0])


def _parse_vehicle_descriptor(data: bytes) -> str:
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            return _text(value)
    return ""


def _parse_position(
    data: bytes,
) -> tuple[float | None, float | None, float | None, float | None]:
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


def _normalised_bearing(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value % 360.0


def _positive_finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return value


def _parse_vehicle_position(
    data: bytes,
    fallback_vehicle_id: str,
) -> KyivVehiclePosition | None:
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

    if not vehicle_id or not route_id or latitude is None or longitude is None:
        return None
    return KyivVehiclePosition(
        vehicle_id=vehicle_id,
        trip_id=trip_id or None,
        route_id=route_id,
        direction_id=direction_id,
        stop_id=stop_id,
        stop_sequence=stop_sequence,
        latitude=latitude,
        longitude=longitude,
        bearing=_normalised_bearing(bearing),
        speed=_positive_finite(speed),
        timestamp=timestamp,
    )


def parse_kyiv_vehicle_positions(
    data: bytes,
) -> tuple[int | None, int, tuple[KyivVehiclePosition, ...]]:
    feed_timestamp = None
    entity_count = 0
    vehicles: dict[str, KyivVehiclePosition] = {}
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


class KyivVehiclePositionsGateway:
    def __init__(
        self,
        *,
        valid_route_registry: Callable[[], dict[str, str]],
        transport: Callable[[str], bytes] | None = None,
        upstream_url: str = KYIV_UPSTREAM_URL,
        clock: Callable[[], float] = time.time,
        cache_ttl: float = KYIV_CACHE_TTL_SECONDS,
        max_stale: float = KYIV_MAX_STALE_SECONDS,
        topology_path: str | None = None,
        topology: KyivRadarTopology | None = None,
    ) -> None:
        self._valid_route_registry = valid_route_registry
        self._transport = transport or _HTTPTransport("HalteWecker-Kyiv-GTFSRT/1.0")
        self._upstream_url = upstream_url
        self._clock = clock
        self._cache_ttl = cache_ttl
        self._max_stale = max_stale
        self._snapshot: _VehicleSnapshot | None = None
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._direction_inference = KyivDirectionInference(
            topology or KyivRadarTopology.from_path(topology_path),
            clock=clock,
        )

    def _fetch_snapshot(self) -> _VehicleSnapshot:
        try:
            body = self._transport(self._upstream_url)
            feed_timestamp, entity_count, vehicles = parse_kyiv_vehicle_positions(body)
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

    @staticmethod
    def _native_route_id(value: str) -> str:
        return value.removeprefix(KYIV_NAMESPACE)

    @staticmethod
    def _iso(timestamp: int | None) -> str | None:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp)) if timestamp is not None else None

    def _filtered_vehicles(
        self,
        vehicles: tuple[KyivVehiclePosition, ...],
        now: float,
    ) -> list[dict[str, object]]:
        route_registry = {
            self._native_route_id(str(route_id)): str(route_type)
            for route_id, route_type in self._valid_route_registry().items()
        }
        result: list[dict[str, object]] = []
        for vehicle in vehicles:
            if vehicle.route_id in KYIV_DISALLOWED_ROUTE_IDS:
                continue
            route_type = route_registry.get(vehicle.route_id)
            if route_type not in KYIV_SUPPORTED_ROUTE_TYPES:
                continue
            if not math.isfinite(vehicle.latitude) or not math.isfinite(vehicle.longitude):
                continue
            if not (-90 <= vehicle.latitude <= 90 and -180 <= vehicle.longitude <= 180):
                continue
            if vehicle.latitude == 0 and vehicle.longitude == 0:
                continue
            if not (
                KYIV_REGION[0] <= vehicle.longitude <= KYIV_REGION[2]
                and KYIV_REGION[1] <= vehicle.latitude <= KYIV_REGION[3]
            ):
                continue
            if vehicle.timestamp is None or now - vehicle.timestamp > self._max_stale:
                continue
            decision = self._direction_inference.observe(
                vehicle.vehicle_id,
                vehicle.route_id,
                KyivVehicleSample(
                    timestamp=vehicle.timestamp,
                    latitude=vehicle.latitude,
                    longitude=vehicle.longitude,
                    trip_id=vehicle.trip_id,
                    stop_id=vehicle.stop_id,
                    stop_sequence=vehicle.stop_sequence,
                    bearing=vehicle.bearing,
                ),
            )
            result.append({
                "vehicleID": vehicle.vehicle_id,
                "tripID": vehicle.trip_id,
                "routeID": vehicle.route_id,
                "directionID": decision.direction_id,
                "destination": decision.destination,
                "stopID": vehicle.stop_id,
                "stopSequence": vehicle.stop_sequence,
                "latitude": vehicle.latitude,
                "longitude": vehicle.longitude,
                "bearing": vehicle.bearing,
                "speed": vehicle.speed,
                "timestamp": self._iso(vehicle.timestamp),
            })
        result.sort(key=lambda item: str(item["vehicleID"]))
        return result

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != KYIV_VEHICLE_POSITIONS_PATH:
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        if query.get("cityID", [None])[0] != KYIV_CITY_ID:
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
                "providerID": KYIV_PROVIDER_ID,
                "cityID": KYIV_CITY_ID,
                "feedTimestamp": self._iso(snapshot.feed_timestamp),
                "retrievedAt": self._iso(int(snapshot.retrieved_at)),
                "stale": stale,
                "entityCount": snapshot.entity_count,
                "vehicleCount": len(vehicles),
                "vehicles": vehicles,
            },
        )
