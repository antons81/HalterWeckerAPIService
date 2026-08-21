import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from fintraffic_gateway import (  # noqa: E402
    FINTRAFFIC_TRIP_UPDATES_PATH,
    FINTRAFFIC_VEHICLE_POSITIONS_PATH,
    FintrafficProviderContext,
    FintrafficTripUpdatesGateway,
    FintrafficVehiclePositionsGateway,
    parse_vehicle_positions,
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


def trip_update_feed() -> bytes:
    header = varint_field(3, 900)
    trip = bytes_field(1, b"trip-1") + bytes_field(5, b"route-1") + bytes_field(6, b"0")
    event = varint_field(2, 950) + varint_field(1, 30)
    stop_update = (
        bytes_field(1, event)
        + varint_field(3, 4)
        + bytes_field(4, b"stop-1")
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
    )


class FintrafficGatewayTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
