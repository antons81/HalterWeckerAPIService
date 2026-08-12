import struct
import sys
import unittest
import zipfile
from tempfile import TemporaryDirectory
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mbta_gateway import (  # noqa: E402
    MBTA_VEHICLE_POSITIONS_PATH,
    MBTAVehiclePositionsGateway,
    parse_mbta_trip_updates,
    parse_mbta_vehicle_positions,
)
from gtfs_agency import agency_scoped_archive  # noqa: E402


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


def trip_update_feed() -> bytes:
    header = varint_field(3, 1000)
    trip = bytes_field(1, b"trip-1") + varint_field(4, 0) + bytes_field(5, b"route-1")
    missing_stop = varint_field(4, 7) + bytes_field(2, varint_field(2, 1015))
    explicit_stop = bytes_field(5, b"70075") + varint_field(4, 8) + bytes_field(2, varint_field(1, 20))
    trip_update = bytes_field(1, trip) + bytes_field(2, missing_stop) + bytes_field(2, explicit_stop)
    entity = bytes_field(1, b"entity-1") + bytes_field(3, trip_update)
    return bytes_field(1, header) + bytes_field(2, entity)


def vehicle_feed() -> bytes:
    header = varint_field(3, 1000)
    trip = bytes_field(1, b"trip-1") + bytes_field(5, b"route-1")
    position = fixed32_field(1, 42.3601) + fixed32_field(2, -71.0589)
    vehicle = (
        bytes_field(1, trip)
        + bytes_field(2, position)
        + varint_field(3, 7)
        + varint_field(5, 1000)
        + bytes_field(7, b"70075")
        + bytes_field(8, bytes_field(1, b"vehicle-1"))
    )
    entity = bytes_field(1, b"entity-1") + bytes_field(4, vehicle)
    return bytes_field(1, header) + bytes_field(2, entity)


class MBTAGatewayTests(unittest.TestCase):
    def test_agency_scope_excludes_cape_flyer_rows(self) -> None:
        tables = {
            "agency.txt": "agency_id,agency_name\n1,MBTA\n3,Cape Cod RTA\n",
            "routes.txt": "route_id,agency_id,route_short_name,route_type\nmbta,1,CR,2\ncape,3,CapeFlyer,2\n",
            "trips.txt": "route_id,service_id,trip_id\nmbta,svc,mbta-trip\ncape,svc,cape-trip\n",
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nmbta-stop,MBTA,42,-71\ncape-stop,Cape,41,-70\n",
            "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nmbta-trip,25:00:00,25:01:00,mbta-stop,1\ncape-trip,10:00:00,10:01:00,cape-stop,1\n",
            "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nsvc,1,1,1,1,1,1,1,20260101,20261231\n",
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "feed.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name, value in tables.items():
                    archive.writestr(name, value)
            original = zipfile.ZipFile(path)
            scoped = agency_scoped_archive(original, "1")
            try:
                self.assertIn(b"mbta-trip", scoped.open("trips.txt").read())
                self.assertNotIn(b"cape-trip", scoped.open("trips.txt").read())
                self.assertIn(b"25:00:00", scoped.open("stop_times.txt").read())
            finally:
                scoped.close()

    def test_trip_updates_resolve_missing_stop_id_by_sequence(self) -> None:
        timestamp, entities, updates = parse_mbta_trip_updates(
            trip_update_feed(),
            lambda trip_ids, sequence_keys: {("trip-1", 7): "70075"},
        )
        self.assertEqual((timestamp, entities), (1000, 1))
        self.assertEqual([update.stop_id for update in updates], ["70075", "70075"])
        self.assertEqual(updates[0].stop_sequence, 7)

    def test_vehicle_parser_uses_standard_mbta_fields(self) -> None:
        timestamp, entities, vehicles = parse_mbta_vehicle_positions(vehicle_feed())
        self.assertEqual((timestamp, entities), (1000, 1))
        self.assertEqual(vehicles[0].vehicle_id, "vehicle-1")
        self.assertEqual(vehicles[0].stop_id, "70075")
        self.assertEqual(vehicles[0].stop_sequence, 7)
        self.assertEqual(vehicles[0].route_id, "route-1")

    def test_vehicle_gateway_applies_strict_fresh_join_and_region_gates(self) -> None:
        gateway = MBTAVehiclePositionsGateway(
            transport=lambda _url: vehicle_feed(),
            valid_registry=lambda: ({"trip-1"}, {"route-1"}, {"trip-1": "route-1"}, lambda value: value),
            clock=lambda: 1000.0,
        )
        response = gateway.handle(MBTA_VEHICLE_POSITIONS_PATH, {"cityID": ["boston"]})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["vehicleCount"], 1)
        self.assertEqual(response.payload["vehicles"][0]["stopID"], "70075")


if __name__ == "__main__":
    unittest.main()
