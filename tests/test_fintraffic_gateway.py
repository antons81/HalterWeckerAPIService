import struct
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from fintraffic_gateway import (  # noqa: E402
    FINTRAFFIC_TRIP_UPDATES_PATH,
    FINTRAFFIC_VEHICLE_POSITIONS_PATH,
    FintrafficProviderContext,
    FintrafficTripUpdatesGateway,
    FintrafficVehiclePosition,
    FintrafficVehiclePositionsGateway,
    parse_vehicle_positions,
    _context_identifier_prefix as _context_identifier_prefix_for_test,
)


def varint(value: int) -> bytes:
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def bytes_field(number: int, value: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(value)) + value


def varint_field(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def fixed32_field(number: int, value: float) -> bytes:
    return varint((number << 3) | 5) + struct.pack("<f", value)


def vehicle_feed() -> bytes:
    header = varint_field(3, 900)
    trip = bytes_field(1, b"trip-1") + bytes_field(5, b"route-1") + bytes_field(6, b"0")
    position = (
        fixed32_field(1, 60.4518)
        + fixed32_field(2, 22.2666)
        + fixed32_field(3, 361.0)
        + fixed32_field(5, 4.5)
    )
    vehicle = (
        bytes_field(1, trip)
        + bytes_field(2, position)
        + varint_field(3, 7)
        + varint_field(5, 900)
        + bytes_field(7, b"rt-stop-not-in-static")
        + bytes_field(8, bytes_field(1, b"vehicle-1"))
    )
    entity = bytes_field(1, b"entity-1") + bytes_field(4, vehicle)
    alert_entity = bytes_field(1, b"alert-1") + bytes_field(6, bytes_field(1, b"ignored"))
    return bytes_field(1, header) + bytes_field(2, entity) + bytes_field(2, alert_entity)


def trip_update_feed(
    *,
    trip_id: bytes = b"trip-1",
    route_id: bytes = b"route-1",
    stop_id: bytes = b"stop-1",
) -> bytes:
    header = varint_field(3, 900)
    trip = bytes_field(1, trip_id) + bytes_field(5, route_id) + bytes_field(6, b"0")
    event = varint_field(2, 950) + varint_field(1, 30)
    stop_update = (
        bytes_field(1, event)
        + varint_field(3, 4)
        + bytes_field(4, stop_id)
    )
    trip_update = bytes_field(1, trip) + bytes_field(2, stop_update)
    entity = bytes_field(1, b"trip-entity-1") + bytes_field(3, trip_update)
    return bytes_field(1, header) + bytes_field(2, entity)


def context() -> FintrafficProviderContext:
    return FintrafficProviderContext(
        provider_id="finland-foli",
        identifier_prefix="fi-foli:",
        stop_id_prefix="fi-foli:",
        trips=frozenset({"fi-foli:trip-1"}),
        routes=frozenset({"fi-foli:route-1"}),
        route_by_trip={"fi-foli:trip-1": "fi-foli:route-1"},
        stops=frozenset({"fi-foli:stop-1"}),
        trip_headsign_by_trip={"fi-foli:trip-1": "Turku Centre"},
    )


class FintrafficGatewayTests(unittest.TestCase):
    def _vehicle_gateway(
        self,
        runtime_context: FintrafficProviderContext,
    ) -> FintrafficVehiclePositionsGateway:
        return FintrafficVehiclePositionsGateway(
            city_ids={"turku"},
            city_regions={"turku": {
                "minimumLatitude": 60.0,
                "maximumLatitude": 61.0,
                "minimumLongitude": 21.0,
                "maximumLongitude": 23.0,
            }},
            context_registry=lambda _city_id: (runtime_context,),
            transport=lambda _url: vehicle_feed(),
            clock=lambda: 1000.0,
            strict_static_join=True,
        )

    def test_vehicle_filter_resolves_explicit_inferred_and_empty_prefix_once(self) -> None:
        cases = (
            ("explicit", context()),
            ("inferred", FintrafficProviderContext(
                provider_id="finland-foli",
                identifier_prefix="",
                stop_id_prefix="",
                trips=frozenset({"fi-foli:trip-1"}),
                routes=frozenset({"fi-foli:route-1"}),
                route_by_trip={"fi-foli:trip-1": "fi-foli:route-1"},
                stops=frozenset({"fi-foli:stop-1"}),
            )),
            ("empty", FintrafficProviderContext(
                provider_id="generic-provider",
                identifier_prefix="",
                stop_id_prefix="",
                trips=frozenset({"trip-1"}),
                routes=frozenset({"route-1"}),
                route_by_trip={"trip-1": "route-1"},
                stops=frozenset({"stop-1"}),
            )),
        )

        for name, runtime_context in cases:
            with self.subTest(name=name), mock.patch(
                "fintraffic_gateway._context_identifier_prefix",
                wraps=_context_identifier_prefix_for_test,
            ) as resolve_prefix:
                response = self._vehicle_gateway(runtime_context).handle(
                    FINTRAFFIC_VEHICLE_POSITIONS_PATH,
                    {"cityID": ["turku"]},
                )

            self.assertEqual(response.status, 200)
            self.assertEqual(response.payload["vehicleCount"], 1)
            self.assertEqual(resolve_prefix.call_count, 1)

    def test_vehicle_filter_large_context_does_not_infer_per_vehicle(self) -> None:
        size = 4000
        runtime_context = FintrafficProviderContext(
            provider_id="finland-foli",
            identifier_prefix="",
            stop_id_prefix="",
            trips=frozenset(f"fi-foli:trip-{index}" for index in range(size)),
            routes=frozenset(f"fi-foli:route-{index}" for index in range(size)),
            route_by_trip={
                f"fi-foli:trip-{index}": f"fi-foli:route-{index}"
                for index in range(size)
            },
            stops=frozenset({"fi-foli:stop-1"}),
        )
        vehicles = tuple(
            FintrafficVehiclePosition(
                vehicle_id=f"vehicle-{index}",
                trip_id=f"trip-{index}",
                route_id=f"route-{index}",
                direction_id=None,
                stop_id="stop-1",
                stop_sequence=None,
                latitude=60.45,
                longitude=22.26,
                bearing=None,
                speed=None,
                timestamp=1000,
            )
            for index in range(128)
        )
        gateway = self._vehicle_gateway(runtime_context)

        with mock.patch(
            "fintraffic_gateway._context_identifier_prefix",
            wraps=_context_identifier_prefix_for_test,
        ) as resolve_prefix:
            result = gateway._filtered_vehicles("turku", vehicles, 1000.0)

        self.assertEqual(len(result), len(vehicles))
        self.assertEqual(resolve_prefix.call_count, 1)

    def test_strict_filter_rejects_non_matching_vehicle_without_prefix_rescans(self) -> None:
        runtime_context = context()
        vehicle = FintrafficVehiclePosition(
            vehicle_id="unknown-vehicle",
            trip_id="unknown-trip",
            route_id="unknown-route",
            direction_id=None,
            stop_id="stop-1",
            stop_sequence=None,
            latitude=60.45,
            longitude=22.26,
            bearing=None,
            speed=None,
            timestamp=1000,
        )
        gateway = self._vehicle_gateway(runtime_context)

        with mock.patch(
            "fintraffic_gateway._context_identifier_prefix",
            wraps=_context_identifier_prefix_for_test,
        ) as resolve_prefix:
            result = gateway._filtered_vehicles("turku", (vehicle,), 1000.0)

        self.assertEqual(result, [])
        self.assertEqual(resolve_prefix.call_count, 1)

    def test_runtime_provider_context_infers_finland_namespace(self) -> None:
        runtime_context = FintrafficProviderContext(
            provider_id="finland-foli",
            identifier_prefix="",
            stop_id_prefix="",
            trips=frozenset({"fi-foli:trip-1"}),
            routes=frozenset({"fi-foli:route-1"}),
            route_by_trip={"fi-foli:trip-1": "fi-foli:route-1"},
            stops=frozenset({"fi-foli:stop-1"}),
        )
        gateway = FintrafficVehiclePositionsGateway(
            city_ids={"turku"},
            city_regions={"turku": {
                "minimumLatitude": 60.0,
                "maximumLatitude": 61.0,
                "minimumLongitude": 21.0,
                "maximumLongitude": 23.0,
            }},
            context_registry=lambda _city_id: (runtime_context,),
            transport=lambda _url: vehicle_feed(),
            clock=lambda: 1000.0,
        )

        response = gateway.handle(
            FINTRAFFIC_VEHICLE_POSITIONS_PATH,
            {"cityID": ["turku"]},
        )

        vehicle = response.payload["vehicles"][0]
        self.assertEqual(vehicle["tripID"], "fi-foli:trip-1")
        self.assertEqual(vehicle["routeID"], "fi-foli:route-1")

    def test_vehicle_enriches_destination_from_static_trip_headsign(self) -> None:
        response = self._vehicle_gateway(context()).handle(
            FINTRAFFIC_VEHICLE_POSITIONS_PATH,
            {"cityID": ["turku"]},
        )

        vehicle = response.payload["vehicles"][0]
        self.assertEqual(vehicle["tripID"], "fi-foli:trip-1")
        self.assertEqual(vehicle["destination"], "Turku Centre")

    def test_direction_id_is_preserved_without_becoming_destination(self) -> None:
        runtime_context = replace(context(), trip_headsign_by_trip={"fi-foli:trip-1": ""})
        response = self._vehicle_gateway(runtime_context).handle(
            FINTRAFFIC_VEHICLE_POSITIONS_PATH,
            {"cityID": ["turku"]},
        )

        vehicle = response.payload["vehicles"][0]
        self.assertEqual(vehicle["directionID"], "0")
        self.assertIsNone(vehicle["destination"])

    def test_multiple_vehicles_reuse_static_trip_headsign_map(self) -> None:
        _timestamp, _entity_count, parsed = parse_vehicle_positions(vehicle_feed())
        vehicles = (parsed[0], replace(parsed[0], vehicle_id="vehicle-2"))
        result = self._vehicle_gateway(context())._filtered_vehicles(
            "turku",
            vehicles,
            1000.0,
        )

        self.assertEqual(len(result), 2)
        self.assertEqual({item["destination"] for item in result}, {"Turku Centre"})

    def test_vehicle_parser_keeps_position_and_ignores_alert_entity(self) -> None:
        timestamp, entity_count, vehicles = parse_vehicle_positions(vehicle_feed())

        self.assertEqual(timestamp, 900)
        self.assertEqual(entity_count, 2)
        self.assertEqual(len(vehicles), 1)
        self.assertEqual(vehicles[0].vehicle_id, "vehicle-1")
        self.assertEqual(vehicles[0].trip_id, "trip-1")
        self.assertEqual(vehicles[0].route_id, "route-1")
        self.assertAlmostEqual(vehicles[0].bearing, 1.0, places=4)

    def test_vehicle_stop_mismatch_does_not_discard_position(self) -> None:
        gateway = FintrafficVehiclePositionsGateway(
            city_ids={"turku"},
            city_regions={"turku": {
                "minimumLatitude": 60.0,
                "maximumLatitude": 61.0,
                "minimumLongitude": 21.0,
                "maximumLongitude": 23.0,
            }},
            context_registry=lambda _city_id: (context(),),
            transport=lambda _url: vehicle_feed(),
            clock=lambda: 1000.0,
        )

        response = gateway.handle(
            FINTRAFFIC_VEHICLE_POSITIONS_PATH,
            {"cityID": ["turku"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["vehicleCount"], 1)
        vehicle = response.payload["vehicles"][0]
        self.assertEqual(vehicle["tripID"], "fi-foli:trip-1")
        self.assertEqual(vehicle["routeID"], "fi-foli:route-1")
        self.assertEqual(vehicle["stopID"], "rt-stop-not-in-static")

    def test_vehicle_gateway_shares_cache_between_cities(self) -> None:
        calls = []
        gateway = FintrafficVehiclePositionsGateway(
            city_ids={"turku", "tampere"},
            city_regions={
                "turku": {"minimumLatitude": 60.0, "maximumLatitude": 61.0, "minimumLongitude": 21.0, "maximumLongitude": 23.0},
                "tampere": {"minimumLatitude": 61.0, "maximumLatitude": 62.0, "minimumLongitude": 23.0, "maximumLongitude": 24.0},
            },
            context_registry=lambda _city_id: (context(),),
            transport=lambda _url: calls.append(True) or vehicle_feed(),
            clock=lambda: 1000.0,
        )

        first = gateway.handle(FINTRAFFIC_VEHICLE_POSITIONS_PATH, {"cityID": ["turku"]})
        second = gateway.handle(FINTRAFFIC_VEHICLE_POSITIONS_PATH, {"cityID": ["tampere"]})

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second.payload["vehicleCount"], 0)

    def test_trip_updates_reuse_shared_gateway_cache_and_namespace_joins(self) -> None:
        calls = []
        gateway = FintrafficTripUpdatesGateway(
            city_ids={"turku"},
            context_registry=lambda _city_id: (context(),),
            transport=lambda _url: calls.append(True) or trip_update_feed(),
            clock=lambda: 1000.0,
        )

        response = gateway.handle(
            FINTRAFFIC_TRIP_UPDATES_PATH,
            {"cityID": ["turku"], "stopIDs": ["fi-foli:stop-1"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(response.payload["updates"][0]["tripID"], "fi-foli:trip-1")
        self.assertEqual(response.payload["updates"][0]["routeID"], "fi-foli:route-1")
        self.assertEqual(response.payload["updates"][0]["stopID"], "fi-foli:stop-1")

    def test_trip_updates_accept_fintraffic_source_prefix_on_trip_id(self) -> None:
        gateway = FintrafficTripUpdatesGateway(
            city_ids={"turku"},
            context_registry=lambda _city_id: (context(),),
            transport=lambda _url: trip_update_feed(trip_id=b"12578_trip-1"),
            clock=lambda: 1000.0,
        )

        response = gateway.handle(
            FINTRAFFIC_TRIP_UPDATES_PATH,
            {"cityID": ["turku"], "stopIDs": ["fi-foli:stop-1"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["updates"][0]["tripID"], "fi-foli:trip-1")


if __name__ == "__main__":
    unittest.main()
