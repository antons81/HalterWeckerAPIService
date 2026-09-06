import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from static_departures_api import Database, ExternalStaticData, Handler


class IsraelStaticAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "stops").mkdir()
        (root / "routes").mkdir()
        (root / "departures").mkdir()
        stops = [
            {
                "id": "12961",
                "name": "ת.מרכזית תל אביב קומה 6/רציפים",
                "latitude": 32.0,
                "longitude": 34.8,
                "locationType": 1,
                "parentStation": None,
            },
            {
                "id": "36168",
                "name": "ת.מרכזית תל אביב קומה 6/רציפים",
                "latitude": 32.0,
                "longitude": 34.8,
                "locationType": 0,
                "parentStation": "12961",
                "platform": "627",
                "floor": "6",
            },
            {
                "id": "36169",
                "name": "ת.מרכזית תל אביב קומה 6/רציפים",
                "latitude": 32.0,
                "longitude": 34.8,
                "locationType": 0,
                "parentStation": "12961",
                "platform": "628",
                "floor": "6",
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
            "36168": [
                {"t": "israel:trip-midnight-past", "r": "israel:route-a", "h": "Midnight Past", "d": "0", "p": "00:05:00", "agencyID": "egg", "operator": "Egged", "routeType": "3"},
                {"t": "israel:trip-past", "r": "israel:route-a", "h": "Past", "d": "0", "p": "11:55:00", "agencyID": "egg", "operator": "Egged", "routeType": "3"},
                {"t": "israel:trip-early", "r": "israel:route-a", "h": "Early", "d": "0", "p": "12:05:00", "agencyID": "egg", "operator": "Egged", "routeType": "3"},
            ],
            "36169": [
                {"t": "israel:trip-later", "r": "israel:route-b", "h": "Later", "d": "0", "p": "12:30:00", "agencyID": "dan", "operator": "Dan", "routeType": "0"},
                {"t": "israel:trip-midnight", "r": "israel:route-b", "h": "Midnight", "d": "0", "p": "24:05:00", "agencyID": "dan", "operator": "Dan", "routeType": "0"},
                {"t": "israel:trip-late", "r": "israel:route-b", "h": "Late", "d": "0", "p": "25:10:00", "agencyID": "dan", "operator": "Dan", "routeType": "0"},
            ],
            "ordinary": [
                {"t": "israel:ordinary-trip", "r": "israel:route-a", "h": "Ordinary Destination", "d": "0", "p": "08:00:00", "platform": "O", "agencyID": "egg", "operator": "Egged", "routeType": "3"},
            ],
        }
        (root / "stops/israel.json").write_text(json.dumps(stops), encoding="utf-8")
        (root / "routes/israel.json").write_text(json.dumps(routes), encoding="utf-8")
        (root / "departures/israel.json").write_text(json.dumps({"timezone": "Asia/Jerusalem", "stops": departures, "platforms": {}}), encoding="utf-8")
        self.now = datetime(2026, 9, 6, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        store = ExternalStaticData(str(root), now_provider=lambda: self.now)
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
        parent_details = self.get("/israel/stations/12961")
        self.assertEqual(parent_details["id"], "12961")
        self.assertEqual(parent_details["locationType"], 1)
        self.assertEqual(parent_details["childPlatformCount"], 2)
        self.assertEqual({item["id"] for item in parent_details["platforms"]}, {"36168", "36169"})
        child_details = self.get("/israel/stations/36168")
        self.assertEqual(child_details["id"], "36168")
        self.assertEqual(child_details["platform"], "627")
        self.assertEqual(child_details["floor"], "6")

        parent = self.get("/israel/stations/12961/departures?limit=2")
        self.assertEqual(
            [item["scheduledTime"] for item in parent["departures"]],
            ["12:05:00", "12:30:00"],
        )
        self.assertEqual([item["stopID"] for item in parent["departures"]], ["36168", "36169"])
        self.assertEqual(parent["departures"][0]["platform"], "627")
        self.assertEqual(parent["departures"][0]["floor"], "6")
        self.assertEqual(parent["departures"][1]["platform"], "628")
        self.assertEqual(parent["timezone"], "Asia/Jerusalem")
        self.assertFalse(parent["departures"][0]["isRealtime"])

        child = self.get("/israel/platforms/36169/departures?limit=1")
        self.assertEqual([item["scheduledTime"] for item in child["departures"]], ["12:30:00"])
        self.assertEqual({item["stopID"] for item in child["departures"]}, {"36169"})
        self.assertEqual(child["departures"][0]["platform"], "628")
        self.assertEqual(child["departures"][0]["operator"], "Dan")

    def test_explicit_from_preserves_timetable_selection(self) -> None:
        payload = self.get("/israel/stations/12961/departures?from=2026-09-06T11:50:00%2B03:00&limit=2")
        self.assertEqual(
            [item["scheduledTime"] for item in payload["departures"]],
            ["11:55:00", "12:05:00"],
        )

    def test_midnight_service_date_and_gtfs_overflow(self) -> None:
        self.now = datetime(2026, 9, 7, 0, 10, tzinfo=ZoneInfo("Asia/Jerusalem"))
        payload = self.get("/israel/stations/12961/departures?limit=3")
        self.assertEqual(
            [item["scheduledTime"] for item in payload["departures"]],
            ["11:55:00", "12:05:00", "12:30:00"],
        )

        explicit = self.get("/israel/stations/12961/departures?at=2026-09-06T23:59:00%2B03:00&limit=3")
        self.assertEqual(
            [item["scheduledTime"] for item in explicit["departures"]],
            ["24:05:00", "00:05:00", "25:10:00"],
        )

    def test_default_crosses_into_next_service_day_when_needed(self) -> None:
        self.now = datetime(2026, 9, 6, 23, 50, tzinfo=ZoneInfo("Asia/Jerusalem"))
        payload = self.get("/israel/stations/12961/departures?limit=3")
        self.assertEqual(
            [(item["tripID"], item["scheduledTime"]) for item in payload["departures"]],
            [
                ("trip-midnight", "24:05:00"),
                ("trip-midnight-past", "00:05:00"),
                ("trip-late", "25:10:00"),
            ],
        )

    def test_nearby_search_and_ordinary_stop_regression(self) -> None:
        nearby = self.get("/israel/stations/nearby?latitude=32&longitude=34.8&radiusMeters=5000&limit=20")
        ids = {item["id"] for item in nearby["stations"]}
        self.assertIn("12961", ids)
        self.assertNotIn("36168", ids)
        self.assertNotIn("36169", ids)

        search = self.get(f"/israel/stations/search?q={quote('ת.מרכזית תל אביב')}&limit=20")
        self.assertEqual([item["id"] for item in search["stations"]], ["12961"])
        details = self.get("/israel/stations/12961")
        self.assertEqual(details["childPlatformCount"], 2)
        self.assertEqual({item["id"] for item in details["platforms"]}, {"36168", "36169"})
        self.assertNotIn("stopDescription", details)

        self.now = datetime(2026, 9, 6, 7, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        ordinary = self.get("/israel/stations/ordinary/departures?limit=10")
        self.assertEqual(ordinary["departures"][0]["stopID"], "ordinary")
        self.assertEqual(ordinary["departures"][0]["destination"], "Ordinary Destination")
        self.assertEqual(ordinary["departures"][0]["operator"], "Egged")

    def test_sqlite_departures_do_not_load_departure_json(self) -> None:
        root = Path(self.temp.name) / "sqlite-static"
        (root / "stops").mkdir(parents=True)
        (root / "routes").mkdir()
        (root / "departures").mkdir()
        stops = [
            {
                "id": "12961",
                "name": "Central Station",
                "latitude": 32.0,
                "longitude": 34.8,
                "locationType": 1,
            },
            {
                "id": "36168",
                "name": "Platform 627",
                "latitude": 32.0,
                "longitude": 34.8,
                "locationType": 0,
                "parentStation": "12961",
                "platform": "627",
                "floor": "6",
            },
            {
                "id": "36169",
                "name": "Platform 628",
                "latitude": 32.0,
                "longitude": 34.8,
                "locationType": 0,
                "parentStation": "12961",
                "platform": "628",
                "floor": "6",
            },
        ]
        (root / "stops/israel.json").write_text(json.dumps(stops), encoding="utf-8")
        (root / "routes/israel.json").write_text("not-json", encoding="utf-8")
        (root / "departures/israel.json").write_text("not-json", encoding="utf-8")

        database_path = root / "departures.sqlite"
        connection = sqlite3.connect(database_path)
        connection.executescript(
            """
            CREATE TABLE city_departure_modes (
                city_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                timezone TEXT NOT NULL,
                stop_id_prefix TEXT NOT NULL,
                identifier_prefix TEXT NOT NULL
            );
            CREATE TABLE raw_stops (
                stop_id TEXT PRIMARY KEY,
                parent_station TEXT NOT NULL,
                stop_name TEXT NOT NULL,
                platform_code TEXT NOT NULL,
                location_type INTEGER NOT NULL,
                platform_display TEXT NOT NULL,
                floor_display TEXT NOT NULL,
                canonical_stop_id TEXT
            );
            CREATE TABLE routes (
                route_id TEXT PRIMARY KEY,
                short_name TEXT NOT NULL,
                long_name TEXT NOT NULL,
                route_type TEXT NOT NULL,
                agency_id TEXT NOT NULL
            );
            CREATE TABLE agencies (agency_id TEXT PRIMARY KEY, agency_name TEXT NOT NULL);
            CREATE TABLE trips (
                trip_id TEXT PRIMARY KEY,
                service_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                headsign TEXT NOT NULL,
                direction_id TEXT NOT NULL,
                terminal_stop_id TEXT NOT NULL
            );
            CREATE TABLE active_services (service_id TEXT NOT NULL, service_date TEXT NOT NULL);
            CREATE TABLE stop_times (
                trip_id TEXT NOT NULL,
                raw_stop_id TEXT NOT NULL,
                departure_time TEXT NOT NULL,
                departure_seconds INTEGER NOT NULL,
                stop_sequence INTEGER NOT NULL
            );
            CREATE INDEX stop_times_by_stop
                ON stop_times(raw_stop_id, departure_seconds, trip_id, stop_sequence);
            INSERT INTO city_departure_modes VALUES
                ('israel', 'canonical', 'Asia/Jerusalem', 'israel:', 'israel:');
            INSERT INTO raw_stops VALUES
                ('israel:12961', '', 'Central Station', '', 1, '', '', 'israel:12961'),
                ('israel:36168', 'israel:12961', 'Platform 627', '627', 0, '627', '6', 'israel:12961'),
                ('israel:36169', 'israel:12961', 'Platform 628', '628', 0, '628', '6', 'israel:12961');
            INSERT INTO routes VALUES
                ('israel:route-a', 'A1', 'Route A', '3', 'israel:egg');
            INSERT INTO agencies VALUES ('israel:egg', 'Egged');
            INSERT INTO trips VALUES
                ('israel:trip-1205', 'svc', 'israel:route-a', 'Bat Yam', '0', ''),
                ('israel:trip-1230', 'svc', 'israel:route-a', 'Tel Aviv', '0', ''),
                ('israel:trip-2405', 'svc', 'israel:route-a', 'Bat Yam', '0', ''),
                ('israel:trip-2510', 'svc', 'israel:route-a', 'Bat Yam', '0', ''),
                ('israel:trip-next', 'svc', 'israel:route-a', 'Tel Aviv', '0', '');
            INSERT INTO active_services VALUES ('svc', '20260906'), ('svc', '20260907');
            INSERT INTO stop_times VALUES
                ('israel:trip-1205', 'israel:36168', '12:05:00', 43500, 1),
                ('israel:trip-1230', 'israel:36169', '12:30:00', 45000, 1),
                ('israel:trip-2405', 'israel:36168', '24:05:00', 86700, 1),
                ('israel:trip-2510', 'israel:36168', '25:10:00', 90600, 1),
                ('israel:trip-next', 'israel:36169', '00:05:00', 300, 1);
            """
        )
        connection.commit()
        connection.close()

        database = Database(str(database_path), ttl=0)
        store = ExternalStaticData(
            str(root),
            now_provider=lambda: datetime(2026, 9, 6, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem")),
            database=database,
        )
        handler = type(
            "SQLiteIsraelTestHandler",
            (Handler,),
            {"database": database, "external_static_data": store},
        )
        server = ThreadingHTTPServer(("localhost", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(
                f"http://localhost:{server.server_port}/israel/stations/12961/departures?limit=3",
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(
                [item["scheduledTime"] for item in payload["departures"]],
                ["24:05:00", "00:05:00", "25:10:00"],
            )
            self.assertEqual(
                [item["stopID"] for item in payload["departures"]],
                ["36168", "36169", "36168"],
            )
            self.assertEqual(payload["departures"][0]["operator"], "Egged")
            self.assertEqual(payload["departures"][0]["platform"], "627")
            self.assertEqual(payload["departures"][0]["floor"], "6")

            with urlopen(
                f"http://localhost:{server.server_port}/israel/stations/12961/departures"
                "?from=2026-09-06T23:59:00%2B03:00&limit=3",
                timeout=5,
            ) as response:
                overnight_payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(
                [item["scheduledTime"] for item in overnight_payload["departures"]],
                ["24:05:00", "00:05:00", "25:10:00"],
            )

            with urlopen(
                f"http://localhost:{server.server_port}/israel/platforms/36169/departures?limit=1",
                timeout=5,
            ) as response:
                child_payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(child_payload["departures"][0]["stopID"], "36169")
            self.assertEqual(child_payload["departures"][0]["platform"], "628")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            database.close()


if __name__ == "__main__":
    unittest.main()
