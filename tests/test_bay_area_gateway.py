import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from bay_area_gateway import (  # noqa: E402
    BAY_AREA_VEHICLE_POSITIONS_PATH,
    BayAreaVehiclePositionsProxy,
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
    trip = bytes_field(1, b"trip-1") + bytes_field(5, b"route-1")
    position = fixed32_field(1, 37.7749) + fixed32_field(2, -122.4194)
    vehicle = (
        bytes_field(1, trip)
        + bytes_field(2, position)
        + varint_field(5, 900)
        + bytes_field(7, b"stop-1")
        + bytes_field(8, bytes_field(1, b"vehicle-1"))
    )
    entity = bytes_field(1, b"entity-1") + bytes_field(4, vehicle)
    return bytes_field(1, header) + bytes_field(2, entity)


def empty_vehicle_feed() -> bytes:
    return bytes_field(1, varint_field(3, 901))


class BayAreaGatewayTests(unittest.TestCase):
    def test_vehicle_parser_preserves_real_position_fields(self) -> None:
        timestamp, entity_count, vehicles = parse_vehicle_positions(vehicle_feed())

        self.assertEqual(timestamp, 900)
        self.assertEqual(entity_count, 1)
        self.assertEqual(vehicles[0].vehicle_id, "vehicle-1")
        self.assertEqual(vehicles[0].route_id, "route-1")
        self.assertAlmostEqual(vehicles[0].latitude, 37.7749, places=4)

    def test_gateway_uses_one_cache_for_valid_empty_or_nonempty_snapshots(self) -> None:
        calls = []
        proxy = BayAreaVehiclePositionsProxy(
            transport=lambda _url: calls.append(True) or vehicle_feed(),
            upstream_url="https://example.test/vehicle-positions",
            valid_registry=lambda: ({"trip-1"}, {"route-1"}, {"trip-1": "route-1"}),
            clock=lambda: 1000.0,
        )

        first = proxy.handle(
            BAY_AREA_VEHICLE_POSITIONS_PATH,
            {"cityID": ["san-francisco"]},
        )
        second = proxy.handle(
            BAY_AREA_VEHICLE_POSITIONS_PATH,
            {"cityID": ["oakland"]},
        )

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(first.payload["vehicles"][0]["vehicleID"], "vehicle-1")
        self.assertEqual(len(calls), 1)

    def test_gateway_rejects_unknown_city_without_fetching(self) -> None:
        called = False

        def transport(_url: str) -> bytes:
            nonlocal called
            called = True
            return vehicle_feed()

        proxy = BayAreaVehiclePositionsProxy(
            transport=transport,
            upstream_url="https://example.test/vehicle-positions",
        )
        response = proxy.handle(
            BAY_AREA_VEHICLE_POSITIONS_PATH,
            {"cityID": ["bart-only"]},
        )

        self.assertEqual(response.status, 400)
        self.assertFalse(called)

    def test_gateway_caches_a_valid_empty_snapshot(self) -> None:
        calls = []
        proxy = BayAreaVehiclePositionsProxy(
            transport=lambda _url: calls.append(True) or empty_vehicle_feed(),
            upstream_url="https://example.test/vehicle-positions",
            clock=lambda: 1000.0,
        )

        first = proxy.handle(
            BAY_AREA_VEHICLE_POSITIONS_PATH,
            {"cityID": ["san-francisco"]},
        )
        second = proxy.handle(
            BAY_AREA_VEHICLE_POSITIONS_PATH,
            {"cityID": ["san-jose"]},
        )

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(first.payload["vehicleCount"], 0)
        self.assertEqual(second.payload["vehicleCount"], 0)
        self.assertEqual(len(calls), 1)

    def test_gateway_keeps_a_bounded_stale_snapshot_after_malformed_refresh(self) -> None:
        calls = []
        now = [1000.0]
        responses = [vehicle_feed(), b"\x80"]
        proxy = BayAreaVehiclePositionsProxy(
            transport=lambda _url: calls.append(True) or responses.pop(0),
            upstream_url="https://example.test/vehicle-positions",
            clock=lambda: now[0],
            cache_ttl=60.0,
            max_stale=300.0,
        )

        first = proxy.handle(
            BAY_AREA_VEHICLE_POSITIONS_PATH,
            {"cityID": ["san-francisco"]},
        )
        now[0] = 1061.0
        second = proxy.handle(
            BAY_AREA_VEHICLE_POSITIONS_PATH,
            {"cityID": ["oakland"]},
        )

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertFalse(first.payload["stale"])
        self.assertTrue(second.payload["stale"])
        self.assertEqual(second.payload["vehicleCount"], 1)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
