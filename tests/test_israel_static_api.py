import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from static_departures_api import ExternalStaticData, Handler


class IsraelStaticAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "stops").mkdir()
        (root / "routes").mkdir()
        (root / "departures").mkdir()
        stops = [
            {
                "id": "israel:hub",
                "name": "Test Central Hub",
                "latitude": 32.0,
                "longitude": 34.8,
                "locationType": 1,
                "parentStation": None,
            },
            {
                "id": "israel:platform-a",
                "name": "Test Central Hub",
                "latitude": 32.0,
                "longitude": 34.8,
                "locationType": 0,
                "parentStation": "israel:hub",
                "platform": "A",
                "floor": "2",
            },
            {
                "id": "israel:platform-b",
                "name": "Test Central Hub",
                "latitude": 32.0,
                "longitude": 34.8,
                "locationType": 0,
                "parentStation": "israel:hub",
                "platform": "B",
                "floor": "3",
            },
            {
                "id": "israel:ordinary",
                "name": "Ordinary Stop",
                "latitude": 32.001,
                "longitude": 34.801,
                "locationType": 0,
                "parentStation": None,
            },
        ]
        routes = {
            "israel:route-a": {
                "short_name": "A1",
                "agency": "egg",
                "agencyName": "Egged",
                "type": "3",
            },
            "israel:route-b": {
                "short_name": "B2",
                "agency": "dan",
                "agencyName": "Dan",
                "type": "0",
            },
        }
        departures = {
            "israel:hub": [
                {"t": "israel:trip-late", "r": "israel:route-b", "h": "Late", "d": "0", "p": "25:05:00", "s": "israel:platform-b", "platform": "B", "floor": "3", "agencyID": "dan", "operator": "Dan", "routeType": "0"},
                {"t": "israel:trip-early", "r": "israel:route-a", "h": "Early", "d": "0", "p": "23:55:00", "s": "israel:platform-a", "platform": "A", "floor": "2", "agencyID": "egg", "operator": "Egged", "routeType": "3"},
            ],
            "israel:platform-a": [
                {"t": "israel:trip-early", "r": "israel:route-a", "h": "Early", "d": "0", "p": "23:55:00", "platform": "A", "floor": "2", "agencyID": "egg", "operator": "Egged", "routeType": "3"},
            ],
            "israel:platform-b": [
                {"t": "israel:trip-late", "r": "israel:route-b", "h": "Late", "d": "0", "p": "25:05:00", "platform": "B", "floor": "3", "agencyID": "dan", "operator": "Dan", "routeType": "0"},
            ],
            "israel:ordinary": [
                {"t": "israel:ordinary-trip", "r": "israel:route-a", "h": "Ordinary Destination", "d": "0", "p": "08:00:00", "platform": "O", "agencyID": "egg", "operator": "Egged", "routeType": "3"},
            ],
        }
        (root / "stops/israel.json").write_text(json.dumps(stops), encoding="utf-8")
        (root / "routes/israel.json").write_text(json.dumps(routes), encoding="utf-8")
        (root / "departures/israel.json").write_text(json.dumps({"timezone": "Asia/Jerusalem", "stops": departures, "platforms": {"israel:hub": ["israel:platform-a", "israel:platform-b"]}}), encoding="utf-8")
        store = ExternalStaticData(str(root))
        handler = type("IsraelTestHandler", (Handler,), {"external_static_data": store})
        self.server = ThreadingHTTPServer(("localhost", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://localhost:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temp.cleanup()

    def get(self, path: str) -> dict[str, object]:
        with urlopen(f"{self.base_url}{path}", timeout=5) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode("utf-8"))

    def test_parent_aggregation_and_child_platform_query(self) -> None:
        parent = self.get("/israel/stations/hub/departures?limit=10")
        self.assertEqual([item["scheduledTime"] for item in parent["departures"]], ["23:55:00", "25:05:00"])
        self.assertEqual([item["stopID"] for item in parent["departures"]], ["platform-a", "platform-b"])
        self.assertEqual(parent["departures"][0]["platform"], "A")
        self.assertEqual(parent["departures"][1]["platform"], "B")
        self.assertEqual(parent["timezone"], "Asia/Jerusalem")
        self.assertFalse(parent["departures"][0]["isRealtime"])

        child = self.get("/israel/platforms/platform-b/departures?limit=10")
        self.assertEqual([item["stopID"] for item in child["departures"]], ["platform-b"])
        self.assertEqual(child["departures"][0]["platform"], "B")
        self.assertEqual(child["departures"][0]["operator"], "Dan")

    def test_nearby_search_and_ordinary_stop_regression(self) -> None:
        nearby = self.get("/israel/stations/nearby?latitude=32&longitude=34.8&radiusMeters=5000&limit=20")
        ids = {item["id"] for item in nearby["stations"]}
        self.assertIn("hub", ids)
        self.assertNotIn("platform-a", ids)
        self.assertNotIn("platform-b", ids)

        search = self.get(f"/israel/stations/search?q={quote('Test Central Hub')}&limit=20")
        self.assertEqual([item["id"] for item in search["stations"]], ["hub"])
        details = self.get("/israel/stations/hub")
        self.assertEqual(details["childPlatformCount"], 2)
        self.assertEqual({item["id"] for item in details["platforms"]}, {"platform-a", "platform-b"})
        self.assertNotIn("stopDescription", details)

        ordinary = self.get("/israel/stations/ordinary/departures?limit=10")
        self.assertEqual(ordinary["departures"][0]["stopID"], "ordinary")
        self.assertEqual(ordinary["departures"][0]["destination"], "Ordinary Destination")
        self.assertEqual(ordinary["departures"][0]["operator"], "Egged")


if __name__ == "__main__":
    unittest.main()
