import json
import struct
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from gtfsrt_gateway import (  # noqa: E402
    GTFSRealtimeHTTPResponse,
    parse_gtfs_realtime_feed,
)
from poland_gateway import (  # noqa: E402
    GdyniaDelaysGateway,
    PolandGTFSRealtimeGateway,
    _CombinedFeedCache,
)


def _varint(value: int) -> bytes:
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _field(number: int, wire_type: int, value: bytes | int) -> bytes:
    encoded = _varint((number << 3) | wire_type)
    if wire_type == 0:
        return encoded + _varint(int(value))
    if wire_type == 2:
        value = bytes(value)
        return encoded + _varint(len(value)) + value
    if wire_type == 5:
        return encoded + bytes(value)
    raise AssertionError("unsupported test wire type")


def _text(number: int, value: str) -> bytes:
    return _field(number, 2, value.encode())


def _feed(*entities: bytes, timestamp: int = 1_700_000_000) -> bytes:
    header = _field(1, 2, _field(1, 2, b"2.0") + _field(3, 0, 0) + _field(4, 0, timestamp))
    return _field(1, 2, header) + b"".join(_field(2, 2, entity) for entity in entities)


def _entity(entity_id: str, payload_field: int, payload: bytes) -> bytes:
    return _text(1, entity_id) + _field(payload_field, 2, payload)


def _trip_entity() -> bytes:
    trip = _text(1, "trip-1") + _text(5, "route-1")
    stop_event = _field(2, 0, 1_700_000_100) + _field(1, 0, 42)
    stop_update = _field(1, 2, stop_event) + _text(4, "stop-1") + _field(3, 0, 2)
    trip_update = _field(1, 2, trip) + _field(2, 2, stop_update)
    return _entity("trip-entity", 3, trip_update)


def _trip_entity_without_stop_id() -> bytes:
    trip = _text(1, "trip-1") + _text(5, "route-1")
    stop_event = _field(2, 0, 1_700_000_100) + _field(1, 0, 42)
    stop_update = _field(1, 2, stop_event) + _field(3, 0, 2)
    trip_update = _field(1, 2, trip) + _field(2, 2, stop_update)
    return _entity("trip-entity", 3, trip_update)


def _vehicle_entity(
    entity_id: str,
    vehicle_id: str,
    latitude: float,
    longitude: float,
    timestamp: int,
) -> bytes:
    position = _field(1, 5, struct.pack("<f", latitude)) + _field(2, 5, struct.pack("<f", longitude))
    vehicle = (
        _field(1, 2, _text(1, "trip-1") + _text(5, "route-1"))
        + _field(2, 2, position)
        + _field(5, 0, timestamp)
        + _field(7, 2, b"stop-1")
        + _field(8, 2, _text(1, vehicle_id))
    )
    return _entity(entity_id, 4, vehicle)


def _alert_entity() -> bytes:
    translation = _field(1, 2, _text(1, "Przystanek zamknięty") + _text(2, "pl"))
    alert = _field(10, 2, translation) + _field(7, 0, 1)
    return _entity("alert-1", 5, alert)


class _FakeTransport:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.fetch_count = 0

    def fetch(self, url: str) -> GTFSRealtimeHTTPResponse:
        self.fetch_count += 1
        return GTFSRealtimeHTTPResponse(200, "application/x-protobuf", self.payloads[url])

    def fetch_raw(self, url: str, *, accept: str) -> GTFSRealtimeHTTPResponse:
        return GTFSRealtimeHTTPResponse(200, "application/json", self.payloads[url])


class _TimedTransport(_FakeTransport):
    def __init__(
        self,
        payloads: dict[str, bytes],
        clock: "_MutableClock",
        completion_times: dict[str, float],
    ) -> None:
        super().__init__(payloads)
        self.clock = clock
        self.completion_times = completion_times

    def fetch(self, url: str) -> GTFSRealtimeHTTPResponse:
        self.clock.value = self.completion_times[url]
        return super().fetch(url)


class _MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class PolandRealtimeTests(unittest.TestCase):
    def test_trip_updates_resolve_missing_stop_id_from_static_trip_sequence(self) -> None:
        payload = _feed(_trip_entity_without_stop_id())
        source = {
            "id": "poland-lublin",
            "namespace": "pl-lublin:",
            "realtime": {"tripUpdatesURL": "https://example.test/trips"},
        }
        gateway = PolandGTFSRealtimeGateway(
            provider_id="poland-lublin",
            city_ids={"lublin"},
            sources=[source],
            path="/poland/lublin/realtime/trip-updates",
            kind="tripUpdates",
            trip_stop_resolver=lambda _source_id, _keys: {("trip-1", 2): "stop-1"},
            transport=_FakeTransport({"https://example.test/trips": payload}),
        )

        response = gateway.handle(
            "/poland/lublin/realtime/trip-updates",
            {"cityID": ["lublin"], "stopIDs": ["pl-lublin:stop-1"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.payload["updates"]), 1)
        self.assertEqual(response.payload["updates"][0]["stopID"], "pl-lublin:stop-1")

    def test_combined_feed_is_fetched_once_for_trip_vehicle_and_alert_gateways(self) -> None:
        now = 1_700_000_200.0
        url = "https://example.test/combined"
        source = {
            "id": "poland-tier2",
            "namespace": "pl-tier2:",
            "realtime": {"combinedURL": url},
        }
        transport = _FakeTransport({
            url: _feed(
                _trip_entity(),
                _vehicle_entity("vehicle-entity", "vehicle-1", 52.2, 21.0, int(now - 10)),
                _alert_entity(),
            ),
        })
        combined_cache = _CombinedFeedCache(clock=lambda: now)
        common = {
            "provider_id": "poland-tier2",
            "city_ids": {"tier2"},
            "sources": [source],
            "clock": lambda: now,
            "transport": transport,
            "combined_feed_cache": combined_cache,
        }
        trip_gateway = PolandGTFSRealtimeGateway(
            **common,
            path="/poland/tier2/realtime/trip-updates",
            kind="tripUpdates",
        )
        vehicle_gateway = PolandGTFSRealtimeGateway(
            **common,
            path="/poland/tier2/realtime/vehicle-positions",
            kind="vehiclePositions",
        )

        trip_response = trip_gateway.handle(
            "/poland/tier2/realtime/trip-updates",
            {"cityID": ["tier2"], "stopIDs": ["pl-tier2:stop-1"]},
        )
        vehicle_response = vehicle_gateway.handle(
            "/poland/tier2/realtime/vehicle-positions",
            {"cityID": ["tier2"]},
        )

        self.assertEqual(trip_response.status, 200)
        self.assertEqual(vehicle_response.status, 200)
        self.assertEqual(len(trip_response.payload["updates"]), 1)
        self.assertEqual(vehicle_response.payload["vehicleCount"], 1)
        self.assertEqual(transport.fetch_count, 1)

    def test_combined_feed_decodes_all_entity_types(self) -> None:
        feed = parse_gtfs_realtime_feed(
            _feed(_trip_entity(), _vehicle_entity("vehicle-entity", "vehicle-1", 52.2, 21.0, 1_700_000_000), _alert_entity())
        )

        self.assertEqual(feed.entity_count, 3)
        self.assertEqual(len(feed.trip_updates), 1)
        self.assertEqual(len(feed.vehicle_positions), 1)
        self.assertEqual(len(feed.alerts), 1)
        self.assertEqual(feed.alerts[0].header_text, "Przystanek zamknięty")

    def test_vehicle_filtering_drops_stale_and_invalid_coordinates(self) -> None:
        now = 1_700_000_200.0
        payload = _feed(
            _vehicle_entity("fresh", "fresh", 52.2, 21.0, 1_700_000_180),
            _vehicle_entity("stale", "stale", 52.2, 21.0, 1_700_000_000),
            _vehicle_entity("zero", "zero", 0.0, 0.0, 1_700_000_180),
            _vehicle_entity("outside", "outside", 91.0, 21.0, 1_700_000_180),
        )
        source = {
            "id": "poland-warsaw",
            "namespace": "pl-warsaw:",
            "realtime": {"vehiclePositionsURL": "https://example.test/vehicles"},
        }
        gateway = PolandGTFSRealtimeGateway(
            provider_id="poland-warsaw",
            city_ids={"warsaw"},
            sources=[source],
            path="/poland/warsaw/realtime/vehicle-positions",
            kind="vehiclePositions",
            city_regions={"warsaw": {"minimumLatitude": 52.0, "maximumLatitude": 52.5, "minimumLongitude": 20.5, "maximumLongitude": 21.5}},
            clock=lambda: now,
            transport=_FakeTransport({"https://example.test/vehicles": payload}),
        )

        response = gateway.handle(
            "/poland/warsaw/realtime/vehicle-positions",
            {"cityID": ["warsaw"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["vehicleCount"], 1)
        self.assertEqual(response.payload["droppedVehicleCount"], 3)
        self.assertEqual(response.payload["health"]["staleCoordinateCount"], 1)
        self.assertEqual(response.payload["health"]["invalidCoordinateCount"], 2)

    def test_runtime_endpoint_filters_mixed_119_and_121_second_positions(self) -> None:
        now = 1_700_000_200.0
        payload = _feed(
            _vehicle_entity("fresh-entity", "fresh", 52.2, 21.0, int(now - 119)),
            _vehicle_entity("stale-entity", "stale", 52.2, 21.0, int(now - 121)),
        )
        source = {
            "id": "poland-warsaw",
            "namespace": "pl-warsaw:",
            "realtime": {"vehiclePositionsURL": "https://example.test/vehicles"},
        }
        gateway = PolandGTFSRealtimeGateway(
            provider_id="poland-warsaw",
            city_ids={"warsaw"},
            sources=[source],
            path="/poland/warsaw/realtime/vehicle-positions",
            kind="vehiclePositions",
            clock=lambda: now,
            transport=_FakeTransport({"https://example.test/vehicles": payload}),
        )

        response = gateway.handle(
            "/poland/warsaw/realtime/vehicle-positions",
            {"cityID": ["warsaw"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["vehicleCount"], 1)
        self.assertEqual(response.payload["vehicles"][0]["vehicleID"], "pl-warsaw:fresh")
        self.assertEqual(response.payload["droppedVehicleCount"], 1)

    def test_stale_health_uses_each_source_fetch_completion_time(self) -> None:
        clock = _MutableClock(0.0)
        sources = [
            {
                "id": "poland-source-a",
                "namespace": "pl-a:",
                "realtime": {"vehiclePositionsURL": "https://example.test/a"},
            },
            {
                "id": "poland-source-b",
                "namespace": "pl-b:",
                "realtime": {"vehiclePositionsURL": "https://example.test/b"},
            },
        ]
        transport = _TimedTransport(
            {
                "https://example.test/a": _feed(
                    _vehicle_entity("a-entity", "a", 52.2, 21.0, 950)
                ),
                "https://example.test/b": _feed(
                    _vehicle_entity("b-entity", "b", 52.2, 21.0, 1880)
                ),
            },
            clock,
            {"https://example.test/a": 1_000.0, "https://example.test/b": 2_000.0},
        )
        gateway = PolandGTFSRealtimeGateway(
            provider_id="poland-test",
            city_ids={"warsaw"},
            sources=sources,
            path="/poland/test/realtime/vehicle-positions",
            kind="vehiclePositions",
            clock=clock,
            transport=transport,
        )

        response = gateway.handle(
            "/poland/test/realtime/vehicle-positions",
            {"cityID": ["warsaw"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["health"]["staleCoordinateCount"], 0)
        self.assertEqual(
            response.payload["health"]["sourceFetchCompletedAt"],
            {
                "poland-source-a": "1970-01-01T00:16:40Z",
                "poland-source-b": "1970-01-01T00:33:20Z",
            },
        )
        self.assertEqual(response.payload["vehicleCount"], 1)
        self.assertEqual(response.payload["vehicles"][0]["vehicleID"], "pl-b:b")

    def test_krakow_sources_keep_independent_namespaces(self) -> None:
        sources = [
            {"id": "poland-krakow-a", "namespace": "pl-krakow-a:", "realtime": {"tripUpdatesURL": "https://example.test/a"}},
            {"id": "poland-krakow-m", "namespace": "pl-krakow-m:", "realtime": {"tripUpdatesURL": "https://example.test/m"}},
        ]
        gateway = PolandGTFSRealtimeGateway(
            provider_id="poland-krakow",
            city_ids={"krakow"},
            sources=sources,
            path="/poland/krakow/realtime/trip-updates",
            kind="tripUpdates",
            clock=lambda: 1_700_000_200.0,
            transport=_FakeTransport({
                "https://example.test/a": _feed(_trip_entity()),
                "https://example.test/m": _feed(_trip_entity()),
            }),
        )

        response = gateway.handle(
            "/poland/krakow/realtime/trip-updates",
            {"cityID": ["krakow"], "stopIDs": ["pl-krakow-a:stop-1,pl-krakow-m:stop-1"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            {item["tripID"] for item in response.payload["updates"]},
            {"pl-krakow-a:trip-1", "pl-krakow-m:trip-1"},
        )

    def test_gdynia_delay_payload_is_normalized_without_vehicle_positions(self) -> None:
        source = {
            "id": "poland-gdynia",
            "namespace": "pl-gdynia:",
            "realtime": {"delaysURL": "https://example.test/delays/{STOP_ID}"},
        }
        payload = {
            "lastUpdate": "2026-08-28 18:32:08",
            "delay": [
                {
                    "routeId": 10085,
                    "trip": 4239558,
                    "delayInSeconds": 16,
                    "estimatedTime": "18:34",
                }
            ],
        }
        gateway = GdyniaDelaysGateway(
            provider_id="poland-gdynia",
            city_ids={"gdynia"},
            source=source,
            path="/poland/gdynia/realtime/trip-updates",
            clock=lambda: 1_787_954_000.0,
            transport=_FakeTransport(
                {"https://example.test/delays/31241": json.dumps(payload).encode()}
            ),
        )

        response = gateway.handle(
            "/poland/gdynia/realtime/trip-updates",
            {"cityID": ["gdynia"], "stopIDs": ["pl-gdynia:31241"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["entityCount"], 1)
        self.assertEqual(response.payload["updates"][0]["tripID"], "pl-gdynia:4239558")
        self.assertEqual(response.payload["updates"][0]["delaySeconds"], 16)
        self.assertEqual(response.payload["updates"][0]["stopID"], "pl-gdynia:31241")
        self.assertIsNotNone(response.payload["updates"][0]["effectiveTime"])


if __name__ == "__main__":
    unittest.main()
