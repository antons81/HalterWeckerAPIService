import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from translink_gateway import TransLinkProxy  # noqa: E402


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _field(number: int, value: bytes | int, wire_type: int = 2) -> bytes:
    result = bytearray(_varint((number << 3) | wire_type))
    if wire_type == 2:
        result.extend(_varint(len(value)))
        result.extend(value)
    else:
        result.extend(_varint(int(value)))
    return bytes(result)


def _text(number: int, value: str) -> bytes:
    return _field(number, value.encode())


def _trip_update_feed(*, stop_id: str = "75", empty: bool = False) -> bytes:
    if empty:
        header = _field(4, 1, 0)
        return _field(1, header)
    event = _field(2, 1_725_000_900, 0) + _field(1, 120, 0)
    stop_update = _field(3, 15, 0) + _field(2, event) + _text(4, stop_id)
    trip = _text(1, "15210220") + _text(5, "6612") + _text(6, "0")
    trip_update = _field(1, trip) + _field(2, stop_update) + _field(4, 1_725_000_000, 0)
    entity = _text(1, "entity-1") + _field(3, trip_update)
    header = _field(4, 1_725_000_000, 0)
    return _field(1, header) + _field(2, entity)


class TransLinkGatewayTests(unittest.TestCase):
    def test_native_trip_route_stop_join_and_secret_boundary(self) -> None:
        requested_urls: list[str] = []

        def transport(url: str) -> bytes:
            requested_urls.append(url)
            return _trip_update_feed()

        gateway = TransLinkProxy("test-key", transport=transport)
        response = gateway.handle(
            "/translink/realtime/trip-updates",
            {"cityID": ["vancouver"], "stopIDs": ["75", "11535"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["updates"][0]["tripID"], "15210220")
        self.assertEqual(response.payload["updates"][0]["routeID"], "6612")
        self.assertEqual(response.payload["updates"][0]["stopID"], "75")
        self.assertEqual(response.payload["updates"][0]["stopSequence"], 15)
        self.assertTrue(requested_urls[0].endswith("apikey=test-key"))
        self.assertNotIn("test-key", str(response.payload))

    def test_empty_valid_feed_is_cached_and_returned(self) -> None:
        calls = 0

        def transport(_url: str) -> bytes:
            nonlocal calls
            calls += 1
            return _trip_update_feed(empty=True)

        gateway = TransLinkProxy("secret", transport=transport, cache_ttl=60)
        query = {"cityID": ["vancouver"], "stopID": ["11535"]}
        first = gateway.handle("/translink/realtime/trip-updates", query)
        second = gateway.handle("/translink/realtime/trip-updates", query)

        self.assertEqual(first.status, 200)
        self.assertEqual(first.payload["updates"], [])
        self.assertEqual(second.status, 200)
        self.assertEqual(calls, 1)

    def test_stale_snapshot_is_used_when_refresh_fails(self) -> None:
        now = [100.0]
        calls = 0

        def transport(_url: str) -> bytes:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _trip_update_feed()
            raise OSError("upstream down")

        gateway = TransLinkProxy(
            "secret",
            transport=transport,
            clock=lambda: now[0],
            cache_ttl=1,
            max_stale=30,
        )
        query = {"cityID": ["vancouver"], "stopID": ["75"]}
        self.assertFalse(gateway.handle("/translink/realtime/trip-updates", query).payload["stale"])
        now[0] = 105
        stale = gateway.handle("/translink/realtime/trip-updates", query)
        self.assertEqual(stale.status, 200)
        self.assertTrue(stale.payload["stale"])

    def test_invalid_request_does_not_call_upstream(self) -> None:
        gateway = TransLinkProxy("secret", transport=lambda _url: _trip_update_feed())
        response = gateway.handle(
            "/translink/realtime/trip-updates",
            {"cityID": ["toronto"], "stopID": ["75"]},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(response.payload, {"error": "unsupported cityID"})


if __name__ == "__main__":
    unittest.main()
