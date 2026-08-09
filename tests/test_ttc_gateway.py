import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from ttc_gateway import TTCProxy  # noqa: E402


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


def _feed(*, trip_id: str = "trip-600", route_id: str = "600", stop_id: str = "100") -> bytes:
    event = _field(2, 1_725_000_900, 0) + _field(1, 120, 0)
    stop_update = _field(3, 15, 0) + _field(2, event) + _text(4, stop_id)
    trip = _text(1, trip_id) + _text(5, route_id) + _text(6, "0")
    trip_update = _field(1, trip) + _field(2, stop_update)
    entity = _text(1, "entity-1") + _field(3, trip_update)
    header = _field(3, 1_725_000_000, 0)
    return _field(1, header) + _field(2, entity)


class TTCGatewayTests(unittest.TestCase):
    def test_surface_namespace_is_preserved_and_subway_is_filtered(self) -> None:
        gateway = TTCProxy(
            trip_registry=lambda: (
                {"ttc-surface:trip-600"},
                {"ttc-surface:trip-600": "ttc-surface:600"},
            ),
            transport=lambda _: _feed(),
        )
        response = gateway.handle(
            "/ttc/realtime/trip-updates",
            {"cityID": ["toronto"], "stopIDs": ["ttc-surface:100"]},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["providerID"], "ttc-toronto")
        self.assertEqual(response.payload["cityID"], "toronto")
        self.assertIsNotNone(response.payload["feedTimestamp"])
        self.assertEqual(response.payload["updates"][0]["tripID"], "ttc-surface:trip-600")
        self.assertEqual(response.payload["updates"][0]["routeID"], "ttc-surface:600")

        subway_response = gateway.handle(
            "/ttc/realtime/trip-updates",
            {"cityID": ["toronto"], "stopIDs": ["ttc-subway:100"]},
        )
        self.assertEqual(subway_response.status, 200)
        self.assertEqual(subway_response.payload["updates"], [])

    def test_trip_registry_is_loaded_from_static_snapshot_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "trips").mkdir()
            (root / "trips" / "toronto.json").write_text(
                json.dumps({
                    "ttc-surface:trip-600": {"r": "ttc-surface:600"},
                    "ttc-subway:trip-600": {"r": "ttc-subway:600"},
                })
            )
            previous = os.environ.pop("TTC_API_KEY", None)
            try:
                gateway = TTCProxy(
                    static_data_root=root,
                    transport=lambda _: _feed(),
                )
                response = gateway.handle(
                    "/ttc/realtime/trip-updates",
                    {"cityID": ["toronto"], "stopIDs": ["ttc-surface:100"]},
                )
            finally:
                if previous is not None:
                    os.environ["TTC_API_KEY"] = previous
            self.assertEqual(response.status, 200)
            self.assertEqual(len(response.payload["updates"]), 1)
            self.assertEqual(response.payload["updates"][0]["routeID"], "ttc-surface:600")

    def test_unknown_route_is_not_published(self) -> None:
        gateway = TTCProxy(
            trip_registry=lambda: (
                {"ttc-surface:trip-600"},
                {"ttc-surface:trip-600": "ttc-surface:506"},
            ),
            transport=lambda _: _feed(),
        )
        response = gateway.handle(
            "/ttc/realtime/trip-updates",
            {"cityID": ["toronto"], "stopIDs": ["ttc-surface:100"]},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["updates"], [])
