import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from tfl_gateway import TfLProxy


class TfLProxyTests(unittest.TestCase):
    def test_missing_key_fails_without_calling_upstream(self) -> None:
        calls: list[str] = []

        def transport(url: str, _timeout: float) -> tuple[int, bytes]:
            calls.append(url)
            return 200, b"{}"

        response = TfLProxy(api_key=None, transport=transport).handle(
            "/tfl/stops/search",
            {"query": ["Oxford Circus"]},
        )

        self.assertEqual(response.status, 503)
        self.assertEqual(response.payload, {"error": "TfL provider unavailable"})
        self.assertEqual(calls, [])

    def test_search_adds_server_side_key_and_caches_success(self) -> None:
        calls: list[str] = []

        def transport(url: str, _timeout: float) -> tuple[int, bytes]:
            calls.append(url)
            return 200, b'{"matches":[]}'

        proxy = TfLProxy(api_key="test-key", transport=transport)
        first = proxy.handle("/tfl/stops/search", {"query": ["Oxford Circus"]})
        second = proxy.handle("/tfl/stops/search", {"query": ["Oxford Circus"]})

        self.assertEqual(first.status, 200)
        self.assertEqual(first.payload, {"matches": []})
        self.assertEqual(second.payload, first.payload)
        self.assertEqual(len(calls), 1)
        self.assertIn("/StopPoint/Search/Oxford%20Circus", calls[0])
        self.assertIn("modes=bus%2Ctube", calls[0])
        self.assertIn("app_key=test-key", calls[0])

    def test_routes_nearby_arrivals_vehicle_and_topology(self) -> None:
        calls: list[str] = []

        def transport(url: str, _timeout: float) -> tuple[int, bytes]:
            calls.append(url)
            return 200, json.dumps({"ok": True}).encode("utf-8")

        proxy = TfLProxy(api_key="test-key", transport=transport)
        requests = [
            ("/tfl/stops/nearby", {"lat": ["51.5074"], "lon": ["-0.1278"], "radius": ["1500"]}),
            ("/tfl/stops/940GZZLUOXC/arrivals", {}),
            ("/tfl/vehicles/LTZ1030/arrivals", {}),
            ("/tfl/lines/159/topology", {"direction": ["inbound"]}),
        ]

        for path, query in requests:
            self.assertEqual(proxy.handle(path, query).status, 200)

        self.assertIn("/StopPoint?", calls[0])
        self.assertIn("/StopPoint/940GZZLUOXC/Arrivals?", calls[1])
        self.assertIn("/Vehicle/LTZ1030/Arrivals?", calls[2])
        self.assertIn("/Line/159/Route/Sequence/inbound?", calls[3])

    def test_upstream_errors_are_sanitized(self) -> None:
        def transport(_url: str, _timeout: float) -> tuple[int, bytes]:
            return 429, b'{"message":"rate limited"}'

        response = TfLProxy(api_key="test-key", transport=transport).handle(
            "/tfl/stops/search",
            {"query": ["London"]},
        )

        self.assertEqual(response.status, 429)
        self.assertEqual(response.payload, {"error": "TfL provider request rejected"})

    def test_invalid_json_is_not_cached(self) -> None:
        calls = 0

        def transport(_url: str, _timeout: float) -> tuple[int, bytes]:
            nonlocal calls
            calls += 1
            return 200, b"not-json"

        proxy = TfLProxy(api_key="test-key", transport=transport)
        response = proxy.handle("/tfl/stops/search", {"query": ["London"]})

        self.assertEqual(response.status, 502)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
