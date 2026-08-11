"""Public King County Metro GTFS-Realtime gateways."""

from __future__ import annotations

from dataclasses import replace
from http import HTTPStatus
from typing import Callable

from bay_area_gateway import BayAreaVehiclePositionsProxy
from bay_area_gateway import _VehicleSnapshot
from gtfsrt_gateway import GTFSRealtimeGateway, _HTTPTransport, GatewayResponse

KING_COUNTY_PROVIDER_ID = "king-county-metro"
KING_COUNTY_CITY_ID = "seattle"
KING_COUNTY_NAMESPACE = "kcm:"
KING_COUNTY_TRIP_UPDATES_PATH = "/king-county/realtime/trip-updates"
KING_COUNTY_VEHICLE_POSITIONS_PATH = "/king-county/realtime/vehicle-positions"
KING_COUNTY_TRIP_UPDATES_URL = "https://s3.amazonaws.com/kcm-alerts-realtime-prod/tripupdates.pb"
KING_COUNTY_VEHICLE_POSITIONS_URL = "https://s3.amazonaws.com/kcm-alerts-realtime-prod/vehiclepositions.pb"


def internal_stop_id(native_stop_id: str) -> str:
    value = native_stop_id.strip()
    return value if not value or value.startswith(KING_COUNTY_NAMESPACE) else f"{KING_COUNTY_NAMESPACE}{value}"


class KingCountyTripUpdatesProxy(GTFSRealtimeGateway):
    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        internal_query = {key: list(values) for key, values in query.items()}
        for key in ("stopID", "stopIDs"):
            if key in internal_query:
                internal_query[key] = [
                    ",".join(
                        value if value.startswith(KING_COUNTY_NAMESPACE)
                        else f"{KING_COUNTY_NAMESPACE}{value}"
                        for value in item.split(",")
                    )
                    for item in internal_query[key]
                ]
        response = super().handle(path, internal_query)
        if response.status == HTTPStatus.OK:
            payload = dict(response.payload)
            payload["stopIDs"] = [
                value.removeprefix(KING_COUNTY_NAMESPACE)
                for value in payload.get("stopIDs", [])
            ]
            updates = []
            for update in payload.get("updates", []):
                item = dict(update)
                item["stopID"] = str(item.get("stopID", "")).removeprefix(KING_COUNTY_NAMESPACE)
                updates.append(item)
            payload["updates"] = updates
            response = GatewayResponse(response.status, payload, response.cache_control)
        return response

    @classmethod
    def from_database(cls, valid_trip_registry: Callable[[], tuple[set[str], dict[str, str]]], valid_stop_registry: Callable[[], set[str]]) -> "KingCountyTripUpdatesProxy":
        return cls(
            provider_id=KING_COUNTY_PROVIDER_ID,
            city_id=KING_COUNTY_CITY_ID,
            path=KING_COUNTY_TRIP_UPDATES_PATH,
            upstream_url=KING_COUNTY_TRIP_UPDATES_URL,
            namespace=KING_COUNTY_NAMESPACE,
            valid_trip_registry=valid_trip_registry,
            valid_stop_registry=valid_stop_registry,
            stop_id_mapper=internal_stop_id,
        )


class KingCountyVehiclePositionsProxy(BayAreaVehiclePositionsProxy):
    def _fetch_snapshot(self) -> _VehicleSnapshot:
        snapshot = super()._fetch_snapshot()
        return replace(
            snapshot,
            vehicles=tuple(
                replace(
                    vehicle,
                    trip_id=f"{KING_COUNTY_NAMESPACE}{vehicle.trip_id}" if vehicle.trip_id else "",
                    route_id=f"{KING_COUNTY_NAMESPACE}{vehicle.route_id}" if vehicle.route_id else "",
                )
                for vehicle in snapshot.vehicles
            ),
        )

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        internal_query = dict(query)
        internal_query["cityID"] = ["san-francisco"]
        response = super().handle("/511/realtime/vehicle-positions", internal_query)
        if response.status == HTTPStatus.OK:
            payload = dict(response.payload)
            payload["providerID"] = KING_COUNTY_PROVIDER_ID
            payload["cityID"] = query.get("cityID", [KING_COUNTY_CITY_ID])[0]
            payload["vehicles"] = [
                {
                    **vehicle,
                    "tripID": str(vehicle["tripID"] or "").removeprefix(KING_COUNTY_NAMESPACE) or None,
                    "routeID": str(vehicle["routeID"] or "").removeprefix(KING_COUNTY_NAMESPACE) or None,
                }
                for vehicle in payload.get("vehicles", [])
            ]
            response = GatewayResponse(response.status, payload, response.cache_control)
        return response

    @classmethod
    def from_database(cls, valid_registry: Callable[[], tuple[set[str], set[str], dict[str, str]]]) -> "KingCountyVehiclePositionsProxy":
        return cls(
            transport=_HTTPTransport("HalteWecker-KingCounty-GTFSRT/1.0"),
            upstream_url=KING_COUNTY_VEHICLE_POSITIONS_URL,
            valid_registry=valid_registry,
        )
