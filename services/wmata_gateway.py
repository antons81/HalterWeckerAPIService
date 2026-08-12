"""WMATA Bus and Metrorail GTFS-Realtime gateways.

The WMATA API exposes separate Bus and Rail feeds. This module combines them
behind one provider-facing contract while keeping the API key server-side.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from http import HTTPStatus
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gtfsrt_gateway import GatewayResponse, GTFSRealtimeGatewayError, RealtimeUpdate, _Snapshot, _iter_fields, _text
from mbta_gateway import MBTAVehicle, parse_mbta_trip_updates, parse_mbta_vehicle_positions

WMATA_PROVIDER_ID = "wmata"
WMATA_CITY_ID = "washington-dc"
WMATA_NAMESPACE = "wmata:"
WMATA_TRIP_UPDATES_PATH = "/wmata/realtime/trip-updates"
WMATA_VEHICLE_POSITIONS_PATH = "/wmata/realtime/vehicle-positions"
WMATA_ALERTS_PATH = "/wmata/realtime/alerts"
WMATA_URLS = {
    "bus": {
        "trip": "https://api.wmata.com/gtfs/bus-gtfsrt-tripupdates.pb",
        "vehicle": "https://api.wmata.com/gtfs/bus-gtfsrt-vehiclepositions.pb",
        "alerts": "https://api.wmata.com/gtfs/bus-gtfsrt-alerts.pb",
    },
    "rail": {
        "trip": "https://api.wmata.com/gtfs/rail-gtfsrt-tripupdates.pb",
        "vehicle": "https://api.wmata.com/gtfs/rail-gtfsrt-vehiclepositions.pb",
        "alerts": "https://api.wmata.com/gtfs/rail-gtfsrt-alerts.pb",
    },
}
WMATA_CACHE_TTL_SECONDS = 30.0
WMATA_MAX_STALE_SECONDS = 300.0


def _transport(api_key: str) -> Callable[[str], bytes]:
    def fetch(url: str) -> bytes:
        request = Request(url, headers={"api_key": api_key, "Accept": "application/octet-stream", "User-Agent": "HalteWecker-WMATA-GTFSRT/1.0"})
        try:
            with urlopen(request, timeout=15) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise GTFSRealtimeGatewayError("upstream unavailable") from error
    return fetch


class WMATATripUpdatesGateway:
    def __init__(self, *, api_key: str, trip_stop_resolver, valid_trip_registry, valid_stop_registry, clock=time.time):
        from mbta_gateway import MBTATripUpdatesGateway
        transport = _transport(api_key)
        common = dict(provider_id=WMATA_PROVIDER_ID, city_id=WMATA_CITY_ID, city_ids={WMATA_CITY_ID}, path=WMATA_TRIP_UPDATES_PATH, trip_stop_resolver=trip_stop_resolver, valid_trip_registry=valid_trip_registry, valid_stop_registry=valid_stop_registry, stop_id_mapper=lambda value: value, cache_ttl=WMATA_CACHE_TTL_SECONDS, max_stale=WMATA_MAX_STALE_SECONDS, user_agent="HalteWecker-WMATA-GTFSRT/1.0", clock=clock)
        self._gateways = tuple(MBTATripUpdatesGateway(upstream_url=WMATA_URLS[mode]["trip"], transport=transport, **common) for mode in ("bus", "rail"))

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        responses = [gateway.handle(path, query) for gateway in self._gateways]
        good = [response for response in responses if response.status == HTTPStatus.OK]
        if not good:
            return responses[0]
        updates = []
        for response in good:
            updates.extend(response.payload.get("updates", []))
        updates.sort(key=lambda item: (str(item.get("effectiveTime") or ""), str(item.get("routeID") or ""), str(item.get("tripID") or "")))
        payload = dict(good[0].payload)
        payload.update({"providerID": WMATA_PROVIDER_ID, "entityCount": sum(int(response.payload.get("entityCount", 0)) for response in good), "routeCount": len({str(item.get("routeID")) for item in updates if item.get("routeID")}), "updates": updates, "partial": len(good) != len(responses), "stale": any(bool(response.payload.get("stale")) for response in good)})
        return GatewayResponse(HTTPStatus.OK, payload)


class WMATAVehiclePositionsGateway:
    def __init__(self, *, api_key: str, valid_registry, clock=time.time):
        self._transport = _transport(api_key)
        self._valid_registry = valid_registry
        self._clock = clock
        self._snapshots: dict[str, tuple[tuple[int | None, int, tuple[MBTAVehicle, ...]], float]] = {}
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    def _snapshot_for_mode(self, mode: str):
        now = self._clock()
        with self._lock:
            cached = self._snapshots.get(mode)
            if cached and now - cached[1] <= WMATA_CACHE_TTL_SECONDS:
                return cached, False
        with self._refresh_lock:
            with self._lock:
                cached = self._snapshots.get(mode)
                if cached and now - cached[1] <= WMATA_CACHE_TTL_SECONDS:
                    return cached, False
            try:
                parsed = parse_mbta_vehicle_positions(self._transport(WMATA_URLS[mode]["vehicle"]))
            except Exception as error:
                if cached and now - cached[1] <= WMATA_MAX_STALE_SECONDS:
                    return cached, True
                raise GTFSRealtimeGatewayError("realtime source unavailable") from error
            snapshot = (parsed, self._clock())
            with self._lock:
                self._snapshots[mode] = snapshot
            return snapshot, False

    @staticmethod
    def _iso(timestamp: int | None) -> str | None:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp)) if timestamp is not None else None

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != WMATA_VEHICLE_POSITIONS_PATH:
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        if query.get("cityID", [None])[0] != WMATA_CITY_ID:
            return GatewayResponse(HTTPStatus.BAD_REQUEST, {"error": "unsupported cityID"})
        try:
            snapshots = [self._snapshot_for_mode(mode) for mode in ("bus", "rail")]
        except GTFSRealtimeGatewayError:
            return GatewayResponse(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "realtime source unavailable"})
        valid_trips, valid_routes, route_by_trip, stop_mapper = self._valid_registry()
        now = self._clock()
        result = []
        entity_count = 0
        stale = False
        feed_timestamps = []
        for (feed_timestamp, entities, vehicles), _retrieved_at in (item[0] for item in snapshots):
            entity_count += entities
            if feed_timestamp is not None:
                feed_timestamps.append(feed_timestamp)
        stale = any(item[1] for item in snapshots)
        for mode, (snapshot, _was_stale) in zip(("bus", "rail"), snapshots):
            parsed, _retrieved_at = snapshot
            _feed_timestamp, _entities, vehicles = parsed
            for vehicle in vehicles:
                if not all(math.isfinite(value) for value in (vehicle.latitude, vehicle.longitude)):
                    continue
                if not (-90 <= vehicle.latitude <= 90 and -180 <= vehicle.longitude <= 180) or (vehicle.latitude == 0 and vehicle.longitude == 0):
                    continue
                if vehicle.timestamp is None or now - vehicle.timestamp > WMATA_MAX_STALE_SECONDS:
                    continue
                if vehicle.trip_id not in valid_trips or vehicle.route_id not in valid_routes or route_by_trip.get(vehicle.trip_id) != vehicle.route_id:
                    continue
                result.append({"vehicleID": vehicle.vehicle_id, "tripID": vehicle.trip_id, "routeID": vehicle.route_id, "directionID": vehicle.direction_id, "stopID": stop_mapper(vehicle.stop_id) if vehicle.stop_id else None, "stopSequence": vehicle.stop_sequence, "latitude": vehicle.latitude, "longitude": vehicle.longitude, "bearing": vehicle.bearing, "speed": vehicle.speed, "timestamp": self._iso(vehicle.timestamp), "mode": mode})
        result.sort(key=lambda item: str(item["vehicleID"]))
        return GatewayResponse(HTTPStatus.OK, {"schemaVersion": 1, "providerID": WMATA_PROVIDER_ID, "cityID": WMATA_CITY_ID, "feedTimestamp": self._iso(max(feed_timestamps) if feed_timestamps else None), "retrievedAt": self._iso(int(self._clock())), "stale": stale, "partial": False, "entityCount": entity_count, "vehicleCount": len(result), "vehicles": result})


def _parse_alert_feed(data: bytes) -> tuple[int | None, int, list[dict[str, object]]]:
    timestamp = entities = 0
    alerts = []
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            for number, nested_wire, nested in _iter_fields(value):
                if number == 3 and nested_wire == 0:
                    timestamp = int(nested)
        elif field == 2 and wire_type == 2:
            entities += 1
            payload = next((nested for number, nested_wire, nested in _iter_fields(value) if number == 5 and nested_wire == 2), None)
            if payload is None:
                continue
            routes = []
            for number, nested_wire, informed in _iter_fields(payload):
                if number != 5 or nested_wire != 2:
                    continue
                route = next((nested for field_number, field_wire, nested in _iter_fields(informed) if field_number == 2 and field_wire == 2), b"")
                if route:
                    routes.append(_text(route))
            alerts.append({"routeIDs": sorted(set(routes)), "stopIDs": [], "tripIDs": []})
    return timestamp, entities, alerts


class WMATAAlertsGateway:
    def __init__(self, *, api_key: str):
        self._transport = _transport(api_key)
        self._snapshot: tuple[dict[str, object], float] | None = None
        self._lock = threading.Lock()

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != WMATA_ALERTS_PATH:
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        if query.get("cityID", [None])[0] != WMATA_CITY_ID:
            return GatewayResponse(HTTPStatus.BAD_REQUEST, {"error": "unsupported cityID"})
        now = time.time()
        with self._lock:
            cached = self._snapshot
            if cached and now - cached[1] <= WMATA_CACHE_TTL_SECONDS:
                return GatewayResponse(HTTPStatus.OK, cached[0])
        try:
            feeds = [_parse_alert_feed(self._transport(WMATA_URLS[mode]["alerts"])) for mode in ("bus", "rail")]
        except Exception as error:
            if cached and now - cached[1] <= WMATA_MAX_STALE_SECONDS:
                payload = dict(cached[0])
                payload["stale"] = True
                return GatewayResponse(HTTPStatus.OK, payload)
            raise GTFSRealtimeGatewayError("realtime source unavailable") from error
        alerts = [item for _timestamp, _entities, values in feeds for item in values]
        payload = {"schemaVersion": 1, "providerID": WMATA_PROVIDER_ID, "cityID": WMATA_CITY_ID, "feedTimestamp": self._iso(max((timestamp for timestamp, _entities, _values in feeds if timestamp), default=None)), "entityCount": sum(entities for _timestamp, entities, _values in feeds), "affectedRouteCount": len({route for alert in alerts for route in alert["routeIDs"]}), "alerts": alerts, "stale": False}
        with self._lock:
            self._snapshot = (payload, now)
        return GatewayResponse(HTTPStatus.OK, payload)

    @staticmethod
    def _iso(timestamp: int | None) -> str | None:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp)) if timestamp is not None else None
