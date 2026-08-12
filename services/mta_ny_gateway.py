"""MTA New York subway and bus GTFS-Realtime gateways."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bay_area_gateway import parse_vehicle_positions
from gtfsrt_gateway import (
    GatewayResponse,
    _iter_fields,
    parse_trip_updates,
)

MTA_PROVIDER_ID = "mta-ny"
MTA_CITY_ID = "new-york"
SUBWAY_PROVIDER_ID = "mta-ny-subway"
NYCT_BUS_PROVIDER_ID = "mta-ny-nyct-bus"
MTA_BUS_PROVIDER_ID = "mta-ny-mta-bus"
SUBWAY_NAMESPACE = "mta-ny-subway:"
NYCT_BUS_NAMESPACE = "mta-ny-nyct-bus:"
MTA_BUS_NAMESPACE = "mta-ny-mta-bus:"
SUBWAY_FEEDS = (
    "gtfs-ace",
    "gtfs-g",
    "gtfs-nqrw",
    "gtfs",
    "gtfs-bdfm",
    "gtfs-jz",
    "gtfs-l",
    "gtfs-si",
)
SUBWAY_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2F{}"
BUS_TRIP_URL = "https://gtfsrt.prod.obanyc.com/tripUpdates"
BUS_VEHICLE_URL = "https://gtfsrt.prod.obanyc.com/vehiclePositions"
BUS_ALERTS_URL = "https://gtfsrt.prod.obanyc.com/alerts"


@dataclass(frozen=True)
class _Registry:
    trips: set[str]
    routes: set[str]
    stops: set[str]
    route_by_trip: dict[str, str]


@dataclass(frozen=True)
class _TripIndex:
    exact: dict[str, tuple[str, ...]]
    subway_suffix: dict[str, tuple[str, ...]]
    route_by_internal_trip: dict[str, str]


def _fetch(url: str, api_key: str | None = None) -> bytes:
    request_url = url
    if api_key:
        request_url = f"{url}?{urlencode({'key': api_key})}"
    request = Request(
        request_url,
        headers={
            "Accept": "application/x-protobuf, application/protobuf",
            "User-Agent": "HalteWecker-MTA-NY/1.0",
        },
    )
    with urlopen(request, timeout=30) as response:
        return response.read()


def _native(identifier: str, namespace: str) -> str:
    return identifier.removeprefix(namespace)


def _trip_index(registries: dict[str, _Registry]) -> _TripIndex:
    exact_candidates: dict[str, list[str]] = {}
    suffixes: dict[str, list[str]] = {}
    route_by_internal_trip: dict[str, str] = {}
    for provider_id, registry in registries.items():
        namespace = {
            SUBWAY_PROVIDER_ID: SUBWAY_NAMESPACE,
            NYCT_BUS_PROVIDER_ID: NYCT_BUS_NAMESPACE,
            MTA_BUS_PROVIDER_ID: MTA_BUS_NAMESPACE,
        }[provider_id]
        for internal_trip in registry.trips:
            exact_candidates.setdefault(_native(internal_trip, namespace), []).append(internal_trip)
            route = registry.route_by_trip.get(internal_trip)
            if route:
                route_by_internal_trip[internal_trip] = route
            if provider_id == SUBWAY_PROVIDER_ID:
                suffixes.setdefault(_native(internal_trip, namespace), []).append(internal_trip)
    return _TripIndex(
        exact={key: tuple(value) for key, value in exact_candidates.items()},
        subway_suffix={key: tuple(value) for key, value in suffixes.items()},
        route_by_internal_trip=route_by_internal_trip,
    )


def _resolve_subway_trip(raw_trip_id: str, index: _TripIndex) -> str | None:
    candidates = [
        internal
        for native, internal_ids in index.subway_suffix.items()
        if native.endswith(raw_trip_id)
        for internal in internal_ids
    ]
    return candidates[0] if len(candidates) == 1 else None


def _resolve_trip(raw_trip_id: str, index: _TripIndex) -> str | None:
    candidates = index.exact.get(raw_trip_id, ())
    if len(candidates) == 1:
        return candidates[0]
    return _resolve_subway_trip(raw_trip_id, index)


def _public_trip(internal_trip_id: str) -> str:
    for namespace in (SUBWAY_NAMESPACE, NYCT_BUS_NAMESPACE, MTA_BUS_NAMESPACE):
        if internal_trip_id.startswith(namespace):
            return internal_trip_id.removeprefix(namespace)
    return internal_trip_id


def _public_route(internal_route_id: str) -> str:
    for namespace in (SUBWAY_NAMESPACE, NYCT_BUS_NAMESPACE, MTA_BUS_NAMESPACE):
        if internal_route_id.startswith(namespace):
            return internal_route_id.removeprefix(namespace)
    return internal_route_id


class MtaNYTripUpdatesGateway:
    def __init__(
        self,
        registry: Callable[[], dict[str, _Registry]],
        api_key: Callable[[], str],
    ) -> None:
        self._registry = registry
        self._api_key = api_key

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != "/mta-ny/realtime/trip-updates":
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        try:
            registries = self._registry()
            index = _trip_index(registries)
            payload_updates: list[dict[str, object]] = []
            timestamps: list[int] = []
            entity_count = 0
            feed_errors = 0
            for feed_name in SUBWAY_FEEDS:
                try:
                    data = _fetch(SUBWAY_URL.format(feed_name))
                    timestamp, entities, updates = parse_trip_updates(data)
                except Exception:
                    feed_errors += 1
                    continue
                entity_count += entities
                if timestamp is not None:
                    timestamps.append(timestamp)
                for update in updates:
                    internal_trip = _resolve_subway_trip(update.trip_id, index)
                    if internal_trip is None:
                        continue
                    route = index.route_by_internal_trip.get(internal_trip)
                    if not route or update.stop_id == "":
                        continue
                    registry = registries[SUBWAY_PROVIDER_ID]
                    if f"{SUBWAY_NAMESPACE}{update.stop_id}" not in registry.stops:
                        continue
                    payload_updates.append({
                        "tripID": _public_trip(internal_trip),
                        "routeID": _public_route(route),
                        "stopID": update.stop_id,
                        "stopSequence": update.stop_sequence,
                        "effectiveTime": _iso(update.effective_time),
                        "delaySeconds": update.delay_seconds,
                        "isCancelled": update.is_cancelled,
                        "mode": "subway",
                    })
            api_key = self._api_key()
            if api_key:
                try:
                    data = _fetch(BUS_TRIP_URL, api_key)
                    timestamp, entities, updates = parse_trip_updates(data)
                    entity_count += entities
                    if timestamp is not None:
                        timestamps.append(timestamp)
                    for update in updates:
                        internal_trip = _resolve_trip(update.trip_id, index)
                        if internal_trip is None:
                            continue
                        provider_id = (
                            NYCT_BUS_PROVIDER_ID
                            if internal_trip.startswith(NYCT_BUS_NAMESPACE)
                            else MTA_BUS_PROVIDER_ID
                        )
                        registry = registries[provider_id]
                        route = index.route_by_internal_trip.get(internal_trip)
                        if not route or update.stop_id == "":
                            continue
                        if f"{NYCT_BUS_NAMESPACE}{update.stop_id}" not in registries[NYCT_BUS_PROVIDER_ID].stops and f"{MTA_BUS_NAMESPACE}{update.stop_id}" not in registries[MTA_BUS_PROVIDER_ID].stops:
                            continue
                        payload_updates.append({
                            "tripID": _public_trip(internal_trip),
                            "routeID": _public_route(route),
                            "stopID": update.stop_id,
                            "stopSequence": update.stop_sequence,
                            "effectiveTime": _iso(update.effective_time),
                            "delaySeconds": update.delay_seconds,
                            "isCancelled": update.is_cancelled,
                            "mode": "bus",
                        })
                except Exception:
                    feed_errors += 1
            return GatewayResponse(
                HTTPStatus.OK,
                {
                    "schemaVersion": 1,
                    "providerID": MTA_PROVIDER_ID,
                    "cityID": MTA_CITY_ID,
                    "feedTimestamp": _iso(max(timestamps) if timestamps else None),
                    "retrievedAt": _iso(int(time.time())),
                    "entityCount": entity_count,
                    "updateCount": len(payload_updates),
                    "feedErrors": feed_errors,
                    "updates": payload_updates,
                },
            )
        except Exception:
            return GatewayResponse(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "MTA realtime source unavailable"},
            )


class MtaNYBusVehiclePositionsGateway:
    def __init__(
        self,
        registry: Callable[[], dict[str, _Registry]],
        api_key: Callable[[], str],
    ) -> None:
        self._registry = registry
        self._api_key = api_key

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != "/mta-ny/realtime/bus-vehicle-positions":
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        api_key = self._api_key()
        if not api_key:
            return GatewayResponse(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "realtime unavailable"})
        try:
            data = _fetch(BUS_VEHICLE_URL, api_key)
            timestamp, entity_count, vehicles = parse_vehicle_positions(data)
            registries = self._registry()
            bus_registries = {
                key: value
                for key, value in registries.items()
                if key in (NYCT_BUS_PROVIDER_ID, MTA_BUS_PROVIDER_ID)
            }
            valid_trips = set().union(*(r.trips for r in bus_registries.values()))
            valid_routes = set().union(*(r.routes for r in bus_registries.values()))
            valid_stops = set().union(*(r.stops for r in bus_registries.values()))
            route_by_trip = {}
            for registry in bus_registries.values():
                route_by_trip.update(registry.route_by_trip)
            output = []
            for vehicle in vehicles:
                internal_trip = f"{NYCT_BUS_NAMESPACE}{vehicle.trip_id}"
                if internal_trip not in valid_trips:
                    internal_trip = f"{MTA_BUS_NAMESPACE}{vehicle.trip_id}"
                if internal_trip not in valid_trips:
                    continue
                internal_route = route_by_trip.get(internal_trip, "")
                namespace = NYCT_BUS_NAMESPACE if internal_trip.startswith(NYCT_BUS_NAMESPACE) else MTA_BUS_NAMESPACE
                if vehicle.route_id and internal_route and _native(internal_route, namespace) != vehicle.route_id:
                    continue
                if vehicle.stop_id and f"{namespace}{vehicle.stop_id}" not in valid_stops:
                    continue
                output.append({
                    "vehicleID": vehicle.vehicle_id,
                    "tripID": _public_trip(internal_trip),
                    "routeID": _public_route(internal_route) if internal_route else vehicle.route_id,
                    "directionID": vehicle.direction_id,
                    "stopID": vehicle.stop_id,
                    "stopSequence": vehicle.stop_sequence,
                    "latitude": vehicle.latitude,
                    "longitude": vehicle.longitude,
                    "bearing": vehicle.bearing,
                    "speed": vehicle.speed,
                    "timestamp": _iso(vehicle.timestamp),
                })
            return GatewayResponse(
                HTTPStatus.OK,
                {
                    "schemaVersion": 1,
                    "providerID": MTA_PROVIDER_ID,
                    "cityID": MTA_CITY_ID,
                    "feedTimestamp": _iso(timestamp),
                    "retrievedAt": _iso(int(time.time())),
                    "entityCount": entity_count,
                    "vehicleCount": len(output),
                    "vehicles": output,
                },
            )
        except Exception:
            return GatewayResponse(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "MTA realtime source unavailable"},
            )


def _iso(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


class MtaNYRegistryCache:
    """Cache static realtime ownership until the active SQLite release changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._identity: tuple[int, int, int] | None = None
        self._registry: dict[str, _Registry] | None = None

    def get(self, database) -> dict[str, _Registry]:
        stat_result = os.stat(database.path)
        identity = (stat_result.st_dev, stat_result.st_ino, stat_result.st_mtime_ns)
        with self._lock:
            if self._registry is None or identity != self._identity:
                self._registry = registry_from_database(database)
                self._identity = identity
            return self._registry


def registry_from_database(database) -> dict[str, _Registry]:
    namespaces = {
        SUBWAY_PROVIDER_ID: SUBWAY_NAMESPACE,
        NYCT_BUS_PROVIDER_ID: NYCT_BUS_NAMESPACE,
        MTA_BUS_PROVIDER_ID: MTA_BUS_NAMESPACE,
    }
    result = {}
    for provider_id in (SUBWAY_PROVIDER_ID, NYCT_BUS_PROVIDER_ID, MTA_BUS_PROVIDER_ID):
        trips, routes, route_by_trip = database.provider_realtime_registry(provider_id)
        namespace = namespaces[provider_id]
        result[provider_id] = _Registry(
            trips=trips,
            routes=routes,
            stops=set(database.provider_stop_registry(provider_id)),
            route_by_trip=route_by_trip,
        )
    return result


def api_key_from_environment() -> str:
    return os.environ.get("MTA_API_KEY", "").strip()
