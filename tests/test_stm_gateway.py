import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "services"))
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from external_gtfs import build_external_departure_index, build_external_stop_packages  # noqa: E402
from stm_gateway import (  # noqa: E402
    STM_ALERTS_PATH,
    STM_TRIP_UPDATES_PATH,
    STM_VEHICLE_POSITIONS_PATH,
    STMRateLimitError,
    STMRealtimeGateway,
    STMRealtimePoller,
    _STMTransport,
    parse_stm_trip_updates,
    parse_stm_vehicle_positions,
)


def _varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _field_varint(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _field_bytes(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _field_fixed32(number: int, value: float) -> bytes:
    return _varint((number << 3) | 5) + struct.pack("<f", value)


def _feed(*entities: bytes, timestamp: int = 1000) -> bytes:
    header = _field_varint(3, timestamp)
    return _field_bytes(1, header) + b"".join(_field_bytes(2, entity) for entity in entities)


def _trip_entity(
    entity_id: str,
    trip_id: str,
    route_id: str,
    stop_id: str,
    *,
    trip_relationship: int = 0,
    stop_relationship: int = 0,
    update_timestamp: int = 1000,
    sequence: int = 1,
) -> bytes:
    descriptor = (
        _field_bytes(1, trip_id.encode())
        + _field_varint(4, trip_relationship)
        + _field_bytes(5, route_id.encode())
        + _field_bytes(6, b"0")
    )
    event = _field_varint(1, 20) + _field_varint(2, 1000)
    stop_update = (
        _field_varint(1, sequence)
        + _field_bytes(2, event)
        + _field_bytes(4, stop_id.encode())
        + _field_varint(5, stop_relationship)
    )
    trip_update = (
        _field_bytes(1, descriptor)
        + _field_bytes(2, stop_update)
        + _field_varint(4, update_timestamp)
    )
    return _field_bytes(1, entity_id.encode()) + _field_bytes(3, trip_update)


def _vehicle_entity(
    entity_id: str,
    vehicle_id: str,
    trip_id: str,
    route_id: str,
    *,
    timestamp: int = 1000,
    latitude: float = 45.48,
    longitude: float = -73.55,
) -> bytes:
    descriptor = _field_bytes(1, trip_id.encode()) + _field_bytes(5, route_id.encode())
    position = (
        _field_fixed32(1, latitude)
        + _field_fixed32(2, longitude)
        + _field_fixed32(3, 90.0)
        + _field_fixed32(5, 2.5)
    )
    vehicle = (
        _field_bytes(1, descriptor)
        + _field_bytes(2, position)
        + _field_varint(3, 14)
        + _field_varint(5, timestamp)
        + _field_bytes(7, b"61618")
        + _field_bytes(8, _field_bytes(1, vehicle_id.encode()))
    )
    return _field_bytes(1, entity_id.encode()) + _field_bytes(4, vehicle)


class _FakeTransport:
    def __init__(self, trip: bytes, vehicle: bytes, alerts: bytes) -> None:
        self.trip = trip
        self.vehicle = vehicle
        self.alerts = alerts
        self.calls = []

    def fetch(self, url, accept, etag=None):
        self.calls.append((url, accept, etag))
        if url.endswith("tripUpdates"):
            return self.trip, None, False
        if url.endswith("vehiclePositions"):
            return self.vehicle, None, False
        return self.alerts, "stm-etag", False


class _RateLimitedTransport:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, url, accept, etag=None):
        self.calls += 1
        raise STMRateLimitError(120.0)


class _Response:
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b"{}"


class STMGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trip_feed = _feed(
            _trip_entity("bus-entity", "trip-bus", "25", "61618"),
            _trip_entity("metro-entity", "trip-metro", "1", "9999111"),
        )
        self.vehicle_feed = _feed(
            _vehicle_entity("bus-entity", "40065", "trip-bus", "25"),
            _vehicle_entity("metro-entity", "metro-1", "trip-metro", "1"),
            _vehicle_entity("unknown-entity", "unknown-1", "missing-trip", "25"),
            _vehicle_entity("stale-entity", "stale-1", "trip-bus", "25", timestamp=600),
        )
        self.alert_feed = json.dumps(
            {
                "header": {"timestamp": 1000},
                "alerts": [
                    {
                        "informed_entities": [
                            {"route_short_name": "25", "direction_id": "1"},
                            {"stop_code": "61618"},
                            {"route_short_name": "unknown", "stop_code": "unknown"},
                        ],
                        "active_periods": {"start": 1000, "end": 1100},
                        "header_texts": [
                            {"language": "fr", "text": "Avis STM"},
                            {"language": "en", "text": "STM notice"},
                        ],
                        "description_texts": [
                            {"language": "fr", "text": "Description"},
                        ],
                        "cause": None,
                        "effect": None,
                    }
                ],
            }
        ).encode()

    def _gateway(self, poller):
        return STMRealtimeGateway(
            poller=poller,
            valid_registry=lambda: (
                {"trip-bus", "trip-metro"},
                {"25", "1"},
                {"trip-bus": "25", "trip-metro": "1"},
                {"25": "3", "1": "1"},
            ),
            valid_stop_registry=lambda: {"61618", "STATION_M146", "9999111"},
            public_stop_registry=lambda: {"61618", "STATION_M146", "9999111"},
            stop_selector=lambda ids: set(ids) | (
                {"9999111"} if "STATION_M146" in ids else set()
            ),
            route_short_registry=lambda: {"25": {"25"}},
            stop_code_registry=lambda: {"61618": {"61618"}},
            clock=lambda: 1000.0,
        )

    def test_trip_updates_require_fresh_exact_bus_joins_and_parent_expands(self) -> None:
        transport = _FakeTransport(self.trip_feed, self.vehicle_feed, self.alert_feed)
        poller = STMRealtimePoller(
            "unit-test-key",
            clock=lambda: 1000.0,
            monotonic=lambda: 1000.0,
            transport=transport,
        )
        poller.refresh_once("trip")
        gateway = self._gateway(poller)

        response = gateway.handle(
            STM_TRIP_UPDATES_PATH,
            {"cityID": ["montreal"], "stopID": ["STATION_M146"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["updateCount"], 0)
        self.assertEqual(response.payload["metroExcludedCount"], 1)
        self.assertEqual(response.payload["matchedStopIDs"], ["9999111", "STATION_M146"])

        bus_response = gateway.handle(
            STM_TRIP_UPDATES_PATH,
            {"cityID": ["montreal"], "stopID": ["61618"]},
        )
        self.assertEqual(bus_response.payload["updateCount"], 1)
        self.assertEqual(bus_response.payload["updates"][0]["routeID"], "25")

    def test_trip_parser_handles_relationships_and_stale_updates(self) -> None:
        feed = _feed(
            _trip_entity("scheduled", "trip-1", "25", "61618"),
            _trip_entity("canceled", "trip-2", "25", "61618", trip_relationship=3),
            _trip_entity("skipped", "trip-3", "25", "61618", stop_relationship=1),
            _trip_entity("no-data", "trip-4", "25", "61618", stop_relationship=2),
            _trip_entity("stale", "trip-5", "25", "61618", update_timestamp=600),
        )
        parsed = parse_stm_trip_updates(feed, now=1000)
        self.assertEqual(parsed["entityCount"], 5)
        self.assertEqual(len(parsed["updates"]), 2)
        self.assertEqual(parsed["skippedUpdateCount"], 1)
        self.assertEqual(parsed["noDataUpdateCount"], 1)
        self.assertEqual(parsed["staleUpdateCount"], 1)
        self.assertEqual(
            {item["scheduleRelationship"] for item in parsed["updates"]},
            {"SCHEDULED", "CANCELED"},
        )

    def test_vehicle_positions_use_strict_gate_and_never_interpolate(self) -> None:
        transport = _FakeTransport(self.trip_feed, self.vehicle_feed, self.alert_feed)
        poller = STMRealtimePoller("unit-test-key", clock=lambda: 1000.0, transport=transport)
        poller.refresh_once("vehicle")
        response = self._gateway(poller).handle(
            STM_VEHICLE_POSITIONS_PATH,
            {"cityID": ["montreal"]},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["vehicleCount"], 1)
        self.assertEqual(response.payload["strictEligibleCount"], 1)
        self.assertEqual(response.payload["staleVehicleCount"], 1)
        self.assertEqual(response.payload["unmatchedTripCount"], 1)
        self.assertEqual(response.payload["metroEntityCount"], 1)
        self.assertFalse(response.payload["interpolation"])
        self.assertEqual(response.payload["vehicles"][0]["vehicleID"], "40065")

    def test_alerts_map_route_stop_direction_and_keep_unmapped_selectors(self) -> None:
        transport = _FakeTransport(self.trip_feed, self.vehicle_feed, self.alert_feed)
        poller = STMRealtimePoller("unit-test-key", clock=lambda: 1000.0, transport=transport)
        poller.refresh_once("alerts")
        response = self._gateway(poller).handle(
            STM_ALERTS_PATH,
            {"cityID": ["montreal"]},
        )
        alert = response.payload["alerts"][0]
        self.assertEqual(alert["routeIDs"], ["25"])
        self.assertEqual(alert["stopIDs"], ["61618"])
        self.assertEqual(alert["directionIDs"], ["1"])
        self.assertEqual(alert["activePeriods"], [{"start": 1000, "end": 1100}])
        self.assertEqual(alert["title"]["fr"], "Avis STM")
        self.assertIsNone(alert["cause"])
        self.assertEqual(len(alert["unmappedSelectors"]), 2)

    def test_auth_header_is_apikey_only(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["headers"] = {key.lower(): value for key, value in request.header_items()}
            return _Response()

        with mock.patch("stm_gateway.urlopen", fake_urlopen):
            _STMTransport("unit-test-key").fetch("https://api.stm.info/test", "application/json")
        self.assertEqual(captured["headers"]["apikey"], "unit-test-key")
        self.assertNotIn("stm_shared_secret", captured["headers"])
        self.assertNotIn("sharedsecret", captured["headers"])

    def test_rate_limit_sets_backoff_without_discarding_cache(self) -> None:
        transport = _RateLimitedTransport()
        poller = STMRealtimePoller(
            "unit-test-key",
            monotonic=lambda: 1000.0,
            transport=transport,
        )
        poller._safe_refresh("trip")
        poller._safe_refresh("trip")
        self.assertEqual(transport.calls, 1)
        self.assertEqual(poller._backoff_until["trip"], 1120.0)

    def test_static_parent_child_and_24_hour_time_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "stm.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "stops.txt",
                    "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station,stop_code\n"
                    "STATION_M146,Berri-UQAM,45.515, -73.561,1,,M146\n"
                    "9999111,Berri platform,45.515,-73.561,0,STATION_M146,9999111\n"
                    "ENTRANCE_M146,Entrance,45.515,-73.561,2,STATION_M146,ENTRANCE_M146\n"
                    "61618,Sherbrooke / Préfontaine,45.55,-73.55,0,,61618\n",
                )
                archive.writestr(
                    "stop_times.txt",
                    "trip_id,stop_id,departure_time,stop_sequence\nT1,9999111,25:30:00,1\n"
                    "T1,61618,26:00:00,2\n",
                )
                archive.writestr(
                    "routes.txt",
                    "route_id,route_short_name,route_long_name,route_type\nR1,25,Bus,3\n",
                )
                archive.writestr(
                    "trips.txt",
                    "route_id,service_id,trip_id,trip_headsign,direction_id\nR1,S1,T1,Terminus,0\n",
                )
                archive.writestr(
                    "calendar.txt",
                    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "S1,1,1,1,1,1,1,1,20200101,20301231\n",
                )
            cities = [{
                "id": "montreal",
                "name": "Montréal",
                "latitude": 45.515,
                "longitude": -73.561,
                "radiusMeters": 50000,
            }]
            output = root / "output"
            with zipfile.ZipFile(archive_path) as archive:
                _manifest, packages = build_external_stop_packages(
                    archive, cities, output, namespace="", publish_passenger_stop_ids=True
                )
            ids = {item["id"] for item in packages["montreal"]}
            self.assertIn("STATION_M146", ids)
            self.assertIn("9999111", ids)
            self.assertNotIn("ENTRANCE_M146", ids)
            with zipfile.ZipFile(archive_path) as archive:
                build_external_departure_index(
                    archive, cities, output, "America/Toronto", namespace="", departure_window_days=1
                )
            departures = json.loads((output / "departures" / "montreal.json").read_text())
            times = [item["p"] for item in departures["stops"]["9999111"]]
            self.assertIn("25:30:00", times)


if __name__ == "__main__":
    unittest.main()
