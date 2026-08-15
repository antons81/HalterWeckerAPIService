import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))


class KyivDockerPackagingTests(unittest.TestCase):
    def test_static_departures_image_copies_kyiv_gateway(self) -> None:
        dockerfile = (
            Path(__file__).resolve().parents[1]
            / "services"
            / "static-departures.Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "COPY services/kyiv_gateway.py /app/kyiv_gateway.py",
            dockerfile,
        )
        self.assertIn(
            "COPY services/kyiv_radar_inference.py /app/kyiv_radar_inference.py",
            dockerfile,
        )

from kyiv_gateway import (  # noqa: E402
    KYIV_VEHICLE_POSITIONS_PATH,
    KyivVehiclePositionsGateway,
    parse_kyiv_vehicle_positions,
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


def vehicle_entity(
    *,
    entity_id: str,
    vehicle_id: str,
    route_id: str,
    latitude: float,
    longitude: float,
    timestamp: int = 1_000,
    trip_id: str | None = None,
) -> bytes:
    trip = bytes_field(5, route_id.encode())
    if trip_id is not None:
        trip = bytes_field(1, trip_id) + trip
    position = fixed32_field(1, latitude) + fixed32_field(2, longitude)
    vehicle = (
        bytes_field(1, trip)
        + bytes_field(2, position)
        + varint_field(5, timestamp)
        + bytes_field(8, bytes_field(1, vehicle_id.encode()))
    )
    return bytes_field(1, entity_id.encode()) + bytes_field(4, vehicle)


def vehicle_feed() -> bytes:
    header = varint_field(3, 1_000)
    valid = vehicle_entity(
        entity_id="entity-valid",
        vehicle_id="vehicle-valid",
        route_id="3_6",
        latitude=50.4501,
        longitude=30.5234,
    )
    unknown_route = vehicle_entity(
        entity_id="entity-unknown",
        vehicle_id="vehicle-unknown",
        route_id="257",
        latitude=50.4501,
        longitude=30.5234,
    )
    unknown_route_255 = vehicle_entity(
        entity_id="entity-unknown-255",
        vehicle_id="vehicle-unknown-255",
        route_id="255",
        latitude=50.4501,
        longitude=30.5234,
    )
    unknown_route_256 = vehicle_entity(
        entity_id="entity-unknown-256",
        vehicle_id="vehicle-unknown-256",
        route_id="256",
        latitude=50.4501,
        longitude=30.5234,
    )
    return (
        bytes_field(1, header)
        + bytes_field(2, valid)
        + bytes_field(2, unknown_route)
        + bytes_field(2, unknown_route_255)
        + bytes_field(2, unknown_route_256)
    )


class KyivGatewayTests(unittest.TestCase):
    def test_parser_keeps_vehicle_position_without_trip_id(self) -> None:
        _, entities, vehicles = parse_kyiv_vehicle_positions(vehicle_feed())

        self.assertEqual(entities, 4)
        self.assertEqual(len(vehicles), 4)
        valid = next(vehicle for vehicle in vehicles if vehicle.route_id == "3_6")
        self.assertEqual(valid.trip_id, None)
        self.assertEqual(valid.vehicle_id, "vehicle-valid")
        self.assertAlmostEqual(valid.latitude, 50.4501, places=4)

    def test_gateway_requires_static_supported_route_type_and_drops_unknown_route(self) -> None:
        gateway = KyivVehiclePositionsGateway(
            transport=lambda _url: vehicle_feed(),
            valid_route_registry=lambda: {
                "kyiv:3_6": "3",
                "kyiv:257": "255",
            },
            clock=lambda: 1_000.0,
        )

        response = gateway.handle(
            KYIV_VEHICLE_POSITIONS_PATH,
            {"cityID": ["kyiv"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["entityCount"], 4)
        self.assertEqual(response.payload["vehicleCount"], 1)
        vehicle = response.payload["vehicles"][0]
        self.assertEqual(vehicle["routeID"], "3_6")
        self.assertIsNone(vehicle["tripID"])

    def test_gateway_rejects_stale_or_out_of_region_positions(self) -> None:
        feed = bytes_field(
            1,
            varint_field(3, 1_000),
        ) + bytes_field(
            2,
            vehicle_entity(
                entity_id="entity-outside",
                vehicle_id="vehicle-outside",
                route_id="3_6",
                latitude=51.2,
                longitude=30.5234,
                timestamp=1_000,
            ),
        ) + bytes_field(
            2,
            vehicle_entity(
                entity_id="entity-stale",
                vehicle_id="vehicle-stale",
                route_id="3_6",
                latitude=50.4501,
                longitude=30.5234,
                timestamp=600,
            ),
        )
        gateway = KyivVehiclePositionsGateway(
            transport=lambda _url: feed,
            valid_route_registry=lambda: {"3_6": "3"},
            clock=lambda: 1_000.0,
            max_stale=300.0,
        )

        response = gateway.handle(
            KYIV_VEHICLE_POSITIONS_PATH,
            {"cityID": ["kyiv"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["vehicleCount"], 0)


if __name__ == "__main__":
    unittest.main()
