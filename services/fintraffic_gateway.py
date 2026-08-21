"""Fintraffic GTFS-Realtime gateways for Finland vehicle positions and TripUpdates."""

from __future__ import annotations

import gzip
import math
import os
import struct
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gtfsrt_gateway import (
    GatewayResponse,
    GTFSRealtimeGateway,
    GTFSRealtimeGatewayError,
    RealtimeUpdate,
    _iter_fields,
    _text,
)


FINTRAFFIC_PROVIDER_ID = "fintraffic"
FINTRAFFIC_TRIP_UPDATES_PATH = "/finland/realtime/trip-updates"
FINTRAFFIC_VEHICLE_POSITIONS_PATH = "/finland/realtime/vehicle-positions"
FINTRAFFIC_UPSTREAM_URL = "https://mobility-api.mobility-database.fintraffic.fi/gtfs-realtime/v2/"
FINTRAFFIC_CACHE_TTL_SECONDS = 15.0
FINTRAFFIC_MAX_STALE_SECONDS = 180.0


@dataclass(frozen=True)
class FintrafficProviderContext:
    """Static ownership data for one Finnish GTFS source in one app city."""

    provider_id: str
    identifier_prefix: str
    stop_id_prefix: str
    trips: frozenset[str]
    routes: frozenset[str]
    route_by_trip: dict[str, str]
    stops: frozenset[str]


def _native_id(value: str, prefix: str) -> str:
    return value[len(prefix):] if prefix and value.startswith(prefix) else value


def _context_identifier_prefix(context: FintrafficProviderContext) -> str:
    """Resolve the static namespace when older release metadata omitted it."""
    if context.identifier_prefix:
        return context.identifier_prefix
    if context.provider_id.startswith("finland-"):
        return f"fi-{context.provider_id.removeprefix('finland-')}:"
    return ""


def _context_stop_prefix(context: FintrafficProviderContext) -> str:
    return context.stop_id_prefix or _context_identifier_prefix(context)


def _trip_candidates_for_raw_id(
    trip_candidates: dict[str, list[tuple[FintrafficProviderContext, str, str]]],
    raw_trip_id: str,
) -> list[tuple[FintrafficProviderContext, str, str]]:
    candidates = trip_candidates.get(raw_trip_id, [])
    if candidates or "_" not in raw_trip_id:
        return candidates
    # Fintraffic may prepend an operator/source identifier to a static trip ID.
    return trip_candidates.get(raw_trip_id.split("_", 1)[1], [])


def _normalised_bearing(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value % 360.0


def _positive_finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return value


class _FintrafficHTTPTransport:
    def __init__(self, api_key: str, user_agent: str) -> None:
        self._api_key = api_key
        self._user_agent = user_agent

    def __call__(self, url: str) -> bytes:
        request = Request(
            url,
            headers={
                "Accept": "application/octet-stream, application/protobuf",
                "Accept-Encoding": "gzip",
                "User-Agent": self._user_agent,
                "x-api-key": self._api_key,
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                body = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
                return body
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise GTFSRealtimeGatewayError("upstream unavailable") from error


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


@dataclass(frozen=True)
class FintrafficVehiclePosition:
    vehicle_id: str
    trip_id: str | None
    route_id: str | None
    direction_id: str | None
    stop_id: str | None
    stop_sequence: int | None
    latitude: float
    longitude: float
    bearing: float | None
    speed: float | None
    timestamp: int | None


def _parse_vehicle_position(
    data: bytes,
    fallback_vehicle_id: str,
) -> FintrafficVehiclePosition | None:
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

    if not vehicle_id or latitude is None or longitude is None:
        return None
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return None
    return FintrafficVehiclePosition(
        vehicle_id=vehicle_id,
        trip_id=trip_id or None,
        route_id=route_id or None,
        direction_id=direction_id,
        stop_id=stop_id,
        stop_sequence=stop_sequence,
        latitude=latitude,
        longitude=longitude,
        bearing=_normalised_bearing(bearing),
        speed=_positive_finite(speed),
        timestamp=timestamp,
    )


@dataclass(frozen=True)
class _VehicleSnapshot:
    feed_timestamp: int | None
    retrieved_at: float
    entity_count: int
    vehicles: tuple[FintrafficVehiclePosition, ...]


def parse_vehicle_positions(
    data: bytes,
) -> tuple[int | None, int, tuple[FintrafficVehiclePosition, ...]]:
    """Decode VehiclePosition entities and ignore unrelated GTFS-RT entities."""
    feed_timestamp = None
    entity_count = 0
    vehicles: dict[str, FintrafficVehiclePosition] = {}
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


def _in_region(latitude: float, longitude: float, region: dict[str, object]) -> bool:
    try:
        return (
            float(region["minimumLatitude"]) <= latitude <= float(region["maximumLatitude"])
            and float(region["minimumLongitude"]) <= longitude <= float(region["maximumLongitude"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def _candidate_contexts(
    contexts: Iterable[FintrafficProviderContext],
) -> tuple[
    dict[str, list[tuple[FintrafficProviderContext, str, str]]],
    dict[str, list[tuple[FintrafficProviderContext, str]]],
]:
    trips: dict[str, list[tuple[FintrafficProviderContext, str, str]]] = {}
    routes: dict[str, list[tuple[FintrafficProviderContext, str]]] = {}
    for context in contexts:
        identifier_prefix = _context_identifier_prefix(context)
        for internal_trip in context.trips:
            raw_trip = _native_id(internal_trip, identifier_prefix)
            internal_route = context.route_by_trip.get(internal_trip, "")
            trips.setdefault(raw_trip, []).append((context, internal_trip, internal_route))
        for internal_route in context.routes:
            raw_route = _native_id(internal_route, identifier_prefix)
            routes.setdefault(raw_route, []).append((context, internal_route))
    return trips, routes


class FintrafficVehiclePositionsGateway:
    def __init__(
        self,
        *,
        city_ids: set[str],
        city_regions: dict[str, dict[str, object]],
        context_registry: Callable[[str], Iterable[FintrafficProviderContext]],
        transport: Callable[[str], bytes],
        upstream_url: str = FINTRAFFIC_UPSTREAM_URL,
        clock: Callable[[], float] = time.time,
        cache_ttl: float = FINTRAFFIC_CACHE_TTL_SECONDS,
        max_stale: float = FINTRAFFIC_MAX_STALE_SECONDS,
    ) -> None:
        self._city_ids = frozenset(city_ids)
        self._city_regions = city_regions
        self._context_registry = context_registry
        self._transport = transport
        self._upstream_url = upstream_url
        self._clock = clock
        self._cache_ttl = cache_ttl
        self._max_stale = max_stale
        self._snapshot: _VehicleSnapshot | None = None
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    @classmethod
    def from_environment(
        cls,
        *,
        city_ids: set[str],
        city_regions: dict[str, dict[str, object]],
        context_registry: Callable[[str], Iterable[FintrafficProviderContext]],
    ) -> "FintrafficVehiclePositionsGateway | None":
        api_key = os.environ.get("FINTRAFFIC_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            city_ids=city_ids,
            city_regions=city_regions,
            context_registry=context_registry,
            transport=_FintrafficHTTPTransport(api_key, "HalteWecker-Fintraffic-GTFSRT/1.0"),
        )

    def _fetch_snapshot(self) -> _VehicleSnapshot:
        try:
            feed_timestamp, entity_count, vehicles = parse_vehicle_positions(
                self._transport(self._upstream_url)
            )
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
            except GTFSRealtimeGatewayError:
                if cached is not None and now - cached.retrieved_at <= self._max_stale:
                    return cached, True
                raise
            with self._lock:
                self._snapshot = fresh
            return fresh, False

    @staticmethod
    def _iso_time(timestamp: int | None) -> str | None:
        if timestamp is None:
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))

    def _filtered_vehicles(
        self,
        city_id: str,
        vehicles: tuple[FintrafficVehiclePosition, ...],
        now: float,
    ) -> list[dict[str, object]]:
        contexts = tuple(self._context_registry(city_id))
        trip_candidates, route_candidates = _candidate_contexts(contexts)
        region = self._city_regions.get(city_id, {})
        result: list[dict[str, object]] = []
        for vehicle in vehicles:
            if not (-90 <= vehicle.latitude <= 90 and -180 <= vehicle.longitude <= 180):
                continue
            if vehicle.latitude == 0 and vehicle.longitude == 0:
                continue
            if not _in_region(vehicle.latitude, vehicle.longitude, region):
                continue
            if vehicle.timestamp is not None and now - vehicle.timestamp > self._max_stale:
                continue

            context: FintrafficProviderContext | None = None
            published_trip = vehicle.trip_id
            published_route = vehicle.route_id
            if vehicle.trip_id:
                candidates = _trip_candidates_for_raw_id(trip_candidates, vehicle.trip_id)
                if vehicle.route_id:
                    candidates = [
                        candidate
                        for candidate in candidates
                        if _native_id(
                            candidate[2], _context_identifier_prefix(candidate[0])
                        ) == vehicle.route_id
                    ] or candidates
                if candidates:
                    context, published_trip, expected_route = candidates[0]
                    published_route = expected_route or published_route
            if context is None and vehicle.route_id:
                candidates = route_candidates.get(vehicle.route_id, [])
                if candidates:
                    context, published_route = candidates[0]

            published_stop = vehicle.stop_id
            if context is not None and vehicle.stop_id:
                candidate_stop = f"{_context_stop_prefix(context)}{vehicle.stop_id}"
                if candidate_stop in context.stops:
                    published_stop = candidate_stop

            result.append({
                "vehicleID": vehicle.vehicle_id,
                "tripID": published_trip,
                "routeID": published_route,
                "directionID": vehicle.direction_id,
                "stopID": published_stop,
                "stopSequence": vehicle.stop_sequence,
                "latitude": vehicle.latitude,
                "longitude": vehicle.longitude,
                "bearing": vehicle.bearing,
                "speed": vehicle.speed,
                "timestamp": self._iso_time(vehicle.timestamp),
            })
        result.sort(key=lambda item: str(item["vehicleID"]))
        return result

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != FINTRAFFIC_VEHICLE_POSITIONS_PATH:
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        city_id = query.get("cityID", [None])[0]
        if city_id not in self._city_ids:
            return GatewayResponse(HTTPStatus.BAD_REQUEST, {"error": "unsupported cityID"})
        try:
            snapshot, stale = self._snapshot_for_request()
        except GTFSRealtimeGatewayError:
            return GatewayResponse(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "realtime source unavailable"},
            )
        vehicles = self._filtered_vehicles(city_id, snapshot.vehicles, self._clock())
        return GatewayResponse(
            HTTPStatus.OK,
            {
                "schemaVersion": 1,
                "providerID": FINTRAFFIC_PROVIDER_ID,
                "cityID": city_id,
                "feedTimestamp": self._iso_time(snapshot.feed_timestamp),
                "retrievedAt": self._iso_time(int(snapshot.retrieved_at)),
                "stale": stale,
                "entityCount": snapshot.entity_count,
                "vehicleCount": len(vehicles),
                "vehicles": vehicles,
            },
        )


class FintrafficTripUpdatesGateway(GTFSRealtimeGateway):
    """Use the shared TripUpdates parser/cache with Finland-specific ID mapping."""

    def __init__(
        self,
        *,
        city_ids: set[str],
        context_registry: Callable[[str], Iterable[FintrafficProviderContext]],
        transport: Callable[[str], bytes],
        upstream_url: str = FINTRAFFIC_UPSTREAM_URL,
        clock: Callable[[], float] = time.time,
        cache_ttl: float = FINTRAFFIC_CACHE_TTL_SECONDS,
        max_stale: float = FINTRAFFIC_MAX_STALE_SECONDS,
    ) -> None:
        first_city = next(iter(city_ids), "helsinki")
        self._context_registry = context_registry
        super().__init__(
            provider_id=FINTRAFFIC_PROVIDER_ID,
            city_id=first_city,
            city_ids=city_ids,
            path=FINTRAFFIC_TRIP_UPDATES_PATH,
            upstream_url=upstream_url,
            transport=transport,
            clock=clock,
            cache_ttl=cache_ttl,
            max_stale=max_stale,
            user_agent="HalteWecker-Fintraffic-GTFSRT/1.0",
        )

    @classmethod
    def from_environment(
        cls,
        *,
        city_ids: set[str],
        context_registry: Callable[[str], Iterable[FintrafficProviderContext]],
    ) -> "FintrafficTripUpdatesGateway | None":
        api_key = os.environ.get("FINTRAFFIC_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            city_ids=city_ids,
            context_registry=context_registry,
            transport=_FintrafficHTTPTransport(api_key, "HalteWecker-Fintraffic-GTFSRT/1.0"),
        )

    def _filter_updates_for_city(
        self,
        updates: tuple[RealtimeUpdate, ...],
        requested_stop_ids: set[str],
        city_id: str,
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        contexts = tuple(self._context_registry(city_id))
        trip_candidates, _ = _candidate_contexts(contexts)
        for update in updates:
            candidates = _trip_candidates_for_raw_id(trip_candidates, update.trip_id)
            if update.route_id:
                candidates = [
                    candidate
                    for candidate in candidates
                    if _native_id(
                        candidate[2], _context_identifier_prefix(candidate[0])
                    ) == update.route_id
                ] or candidates
            if not candidates:
                continue
            context, internal_trip, internal_route = candidates[0]
            internal_stop = f"{_context_stop_prefix(context)}{update.stop_id}"
            if internal_stop not in context.stops:
                continue
            if internal_stop not in requested_stop_ids and update.stop_id not in requested_stop_ids:
                continue
            result.append({
                "tripID": internal_trip,
                "routeID": internal_route or f"{_context_identifier_prefix(context)}{update.route_id}",
                "directionID": update.direction_id,
                "stopID": internal_stop,
                "stopSequence": update.stop_sequence,
                "effectiveTime": self._iso_time(update.effective_time),
                "delaySeconds": update.delay_seconds,
                "isCancelled": update.is_cancelled,
            })
        result.sort(key=lambda item: (
            str(item["effectiveTime"] or ""),
            str(item["routeID"] or ""),
            str(item["tripID"]),
        ))
        return result

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != FINTRAFFIC_TRIP_UPDATES_PATH:
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        requested_city_id = query.get("cityID", [None])[0]
        if requested_city_id not in self._city_ids:
            return GatewayResponse(HTTPStatus.BAD_REQUEST, {"error": "unsupported cityID"})
        try:
            requested_stop_ids = self._requested_stop_ids(query)
            snapshot, stale = self._snapshot_for_request()
        except GTFSRealtimeGatewayError as error:
            message = str(error)
            status = (
                HTTPStatus.BAD_REQUEST
                if message == "invalid stop selection"
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            return GatewayResponse(
                status,
                {"error": message if status == HTTPStatus.BAD_REQUEST else "realtime source unavailable"},
            )

        updates = self._filter_updates_for_city(
            snapshot.updates,
            set(requested_stop_ids),
            str(requested_city_id),
        )
        return GatewayResponse(
            HTTPStatus.OK,
            {
                "schemaVersion": 1,
                "providerID": FINTRAFFIC_PROVIDER_ID,
                "cityID": requested_city_id,
                "stopIDs": requested_stop_ids,
                "feedTimestamp": self._iso_time(snapshot.feed_timestamp),
                "retrievedAt": self._iso_time(int(snapshot.retrieved_at)),
                "stale": stale,
                "entityCount": snapshot.entity_count,
                "routeCount": len({str(update["routeID"]) for update in updates if update["routeID"]}),
                "updates": updates,
            },
        )
