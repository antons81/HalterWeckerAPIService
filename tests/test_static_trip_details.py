import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import static_departures_api as api


class StaticTripDetailsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "stops").mkdir()
        (self.root / "stops" / "fixture.json").write_text(json.dumps([
            {"id": "child", "latitude": 32.0, "longitude": 34.8, "platform": "627", "floor": "6"},
            {"id": "target", "latitude": 31.2, "longitude": 34.9},
        ]))
        path = self.root / "test.sqlite"
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE city_departure_modes(city_id,mode,timezone,stop_id_prefix,identifier_prefix);
                INSERT INTO city_departure_modes VALUES('fixture','canonical','Asia/Jerusalem','feed:','feed:');
                CREATE TABLE routes(route_id PRIMARY KEY,short_name,long_name,agency_id,route_type);
                CREATE TABLE agencies(agency_id PRIMARY KEY,agency_name);
                INSERT INTO agencies VALUES('feed:operator','Operator');
                CREATE TABLE trips(trip_id PRIMARY KEY,route_id,headsign,direction_id,service_id);
                CREATE TABLE active_services(service_id,service_date,PRIMARY KEY(service_id,service_date));
                INSERT INTO active_services VALUES('service','20260906');
                CREATE TABLE raw_stops(stop_id PRIMARY KEY,stop_name);
                INSERT INTO raw_stops VALUES('feed:child','Origin'),('feed:target','Destination');
                CREATE TABLE stop_times(trip_id,raw_stop_id,stop_sequence,arrival_time,departure_time);
                CREATE INDEX stop_times_by_trip ON stop_times(trip_id,stop_sequence);
            """)
            for name, mode in (("intercity", "3"), ("rail", "2"), ("tram", "0"), ("urban", "3")):
                db.execute("INSERT INTO routes VALUES(?,?,?,?,?)", ("feed:" + name, name, "", "feed:operator", mode))
                db.execute("INSERT INTO trips VALUES(?,?,?,?,?)", ("feed:" + name, "feed:" + name, "Destination", "0", "service"))
                db.executemany("INSERT INTO stop_times VALUES(?,?,?,?,?)", [
                    ("feed:" + name, "feed:target", 2, "25:05:00", "25:10:00"),
                    ("feed:" + name, "feed:child", 1, "24:00:00", "24:05:00")])
        self.db = api.Database(str(path))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_modes_order_coordinates_and_scheduled_times(self):
        for name, mode in (("intercity", "bus"), ("rail", "train"), ("tram", "tram"), ("urban", "bus")):
            with self.subTest(name=name):
                result = self.db.trip_details("fixture", name, str(self.root), "2026-09-06")
                self.assertEqual(result["tripID"], name)
                self.assertEqual(result["routeID"], name)
                self.assertEqual(result["line"], name)
                self.assertEqual(result["destination"], "Destination")
                self.assertEqual(result["transportMode"], mode)
                self.assertEqual(result["operatorID"], "operator")
                self.assertEqual(result["operator"], "Operator")
                self.assertEqual(result["timezone"], "Asia/Jerusalem")
                self.assertEqual(result["serviceDate"], "2026-09-06")
                self.assertEqual([s["stopSequence"] for s in result["stops"]], [1, 2])
                self.assertEqual([s["scheduledArrival"] for s in result["stops"]], ["24:00:00", "25:05:00"])
                self.assertEqual([s["scheduledDeparture"] for s in result["stops"]], ["24:05:00", "25:10:00"])
                self.assertEqual(result["stops"][0]["latitude"], 32.0)
                self.assertEqual(result["stops"][0]["platform"], "627")
                self.assertIsNone(result["geometry"])
                self.assertFalse(result["isRealtime"])

    def test_missing_stop_catalog_returns_partial_metadata(self):
        result = self.db.trip_details("fixture", "rail", str(self.root / "missing"), "2026-09-06")
        self.assertEqual(len(result["stops"]), 2)
        self.assertIsNone(result["stops"][0]["latitude"])
        self.assertIsNone(result["stops"][0]["longitude"])
        self.assertEqual(result["stops"][0]["scheduledArrival"], "24:00:00")

    def test_legacy_database_without_arrival_column_is_supported(self):
        path = self.root / "legacy.sqlite"
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE city_departure_modes(city_id,mode,timezone,stop_id_prefix,identifier_prefix);
                INSERT INTO city_departure_modes VALUES('fixture','canonical','Asia/Jerusalem','feed:','feed:');
                CREATE TABLE routes(route_id PRIMARY KEY,short_name,long_name,agency_id,route_type);
                CREATE TABLE agencies(agency_id PRIMARY KEY,agency_name);
                INSERT INTO agencies VALUES('feed:operator','Operator');
                CREATE TABLE trips(trip_id PRIMARY KEY,route_id,headsign,direction_id,service_id);
                INSERT INTO trips VALUES('feed:rail','feed:rail','Destination','0','service');
                CREATE TABLE active_services(service_id,service_date,PRIMARY KEY(service_id,service_date));
                INSERT INTO active_services VALUES('service','20260906');
                CREATE TABLE raw_stops(stop_id PRIMARY KEY,stop_name);
                INSERT INTO raw_stops VALUES('feed:child','Origin'),('feed:target','Destination');
                CREATE TABLE stop_times(trip_id,raw_stop_id,stop_sequence,departure_time);
                INSERT INTO routes VALUES('feed:rail','rail','','feed:operator','2');
                INSERT INTO stop_times VALUES('feed:rail','feed:child',1,'24:05:00');
            """)
        legacy = api.Database(str(path))
        try:
            result = legacy.trip_details("fixture", "rail", str(self.root), "2026-09-06")
        finally:
            legacy.close()
        self.assertIsNone(result["stops"][0]["scheduledArrival"])
        self.assertEqual(result["stops"][0]["scheduledDeparture"], "24:05:00")

    def test_unknown_and_inactive_trip(self):
        self.assertIsNone(self.db.trip_details("fixture", "missing", str(self.root)))
        self.assertIsNone(self.db.trip_details("fixture", "rail", str(self.root), "2026-09-07"))
        self.assertIsNone(self.db.trip_details("other", "rail", str(self.root)))
        with self.assertRaises(ValueError):
            self.db.trip_details("../fixture", "rail", str(self.root))

    def test_http_contract_and_errors(self):
        handler = type("TripHandler", (api.Handler,), {"database": self.db})
        server = ThreadingHTTPServer(("localhost", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(api, "STATIC_DATA_ROOT", str(self.root)):
                endpoint = f"http://localhost:{server.server_port}/static-departures/trip"
                with urlopen(endpoint + "?cityID=fixture&tripID=rail&serviceDate=2026-09-06") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.load(response)["transportMode"], "train")
                for query, status in (("", 400), ("?cityID=fixture&tripID=missing", 404),
                                      ("?cityID=fixture&tripID=rail&serviceDate=bad", 400)):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(endpoint + query)
                    self.assertEqual(error.exception.code, status)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
