import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from import_static_departures_database import populate_german_city_memberships
from static_departures_api import Database, Handler
from swap_static_departures_database import activate_database


def write_database(path: Path, version: str, valid: bool = True) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO metadata VALUES ('databaseVersion', 'placeholder');
        """
    )
    db.execute("UPDATE metadata SET value=? WHERE key='databaseVersion'", (version,))
    if valid:
        db.executescript(
            """
            CREATE TABLE raw_stops (
                stop_id TEXT PRIMARY KEY,
                parent_station TEXT NOT NULL,
                stop_name TEXT NOT NULL,
                platform_code TEXT NOT NULL,
                source_order INTEGER NOT NULL,
                canonical_stop_id TEXT
            );
            CREATE TABLE city_stops (city_id TEXT NOT NULL, stop_id TEXT NOT NULL, PRIMARY KEY (city_id, stop_id));
            CREATE TABLE city_aliases (alias_city_id TEXT PRIMARY KEY, canonical_city_id TEXT NOT NULL);
            CREATE TABLE routes (route_id TEXT PRIMARY KEY, short_name TEXT NOT NULL, long_name TEXT NOT NULL);
            CREATE TABLE trips (
                trip_id TEXT PRIMARY KEY,
                service_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                headsign TEXT NOT NULL,
                direction_id TEXT NOT NULL,
                terminal_stop_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE calendar (
                service_id TEXT PRIMARY KEY,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                monday INTEGER NOT NULL,
                tuesday INTEGER NOT NULL,
                wednesday INTEGER NOT NULL,
                thursday INTEGER NOT NULL,
                friday INTEGER NOT NULL,
                saturday INTEGER NOT NULL,
                sunday INTEGER NOT NULL
            );
            CREATE TABLE calendar_dates (
                service_id TEXT NOT NULL,
                service_date TEXT NOT NULL,
                exception_type INTEGER NOT NULL,
                PRIMARY KEY (service_id, service_date)
            );
            CREATE TABLE active_services (
                service_id TEXT NOT NULL,
                service_date TEXT NOT NULL,
                PRIMARY KEY (service_id, service_date)
            );
            CREATE TABLE stop_times (
                trip_id TEXT NOT NULL,
                raw_stop_id TEXT NOT NULL,
                departure_time TEXT NOT NULL,
                departure_seconds INTEGER NOT NULL,
                stop_sequence INTEGER NOT NULL
            );
            """
        )
        db.executemany(
            "INSERT INTO raw_stops VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("stop-parent", "", "Test Stop", "", 1, "stop-parent"),
                ("stop-platform", "stop-parent", "Test Stop Platform 1", "1", 2, "stop-parent"),
                ("terminal", "", "Terminal", "", 3, "terminal"),
            ],
        )
        db.execute("INSERT INTO city_stops VALUES ('dresden', 'stop-parent')")
        db.execute("INSERT INTO city_stops VALUES ('dresden', 'terminal')")
        db.execute("INSERT INTO city_stops VALUES ('koln', 'stop-parent')")
        db.execute("INSERT INTO city_aliases VALUES ('koeln', 'koln')")
        db.execute("INSERT INTO routes VALUES ('route-1', '7', 'Line 7')")
        db.execute("INSERT INTO trips VALUES ('trip-1', 'service-1', 'route-1', 'Weixdorf', '0', 'terminal')")
        db.execute("INSERT INTO active_services VALUES ('service-1', '20260728')")
        db.executemany(
            "INSERT INTO stop_times VALUES (?, ?, ?, ?, ?)",
            [
                ("trip-1", "stop-platform", "23:55:00", 86100, 1),
                ("trip-1", "terminal", "25:05:00", 90300, 2),
            ],
        )
    db.commit()
    db.close()


class StaticDeparturesHTTPServer:
    def __init__(self, database_path: Path, ttl: float = 0.0) -> None:
        self.database = Database(str(database_path), ttl=ttl)
        handler = type("StaticDeparturesTestHandler", (Handler,), {"database": self.database})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "StaticDeparturesHTTPServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.database.close()

    def get(self, path: str) -> dict[str, object]:
        with urlopen(f"{self.base_url}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


class StaticDeparturesDatabaseTests(unittest.TestCase):
    def test_reopens_when_atomic_symlink_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second, current = root / "first.sqlite", root / "second.sqlite", root / "current.sqlite"
            write_database(first, "first")
            write_database(second, "second")
            current.symlink_to(first.name)
            database = Database(str(current), ttl=0)
            try:
                self.assertEqual(database.meta()["databaseVersion"], "first")
                next_link = root / "next.sqlite"
                next_link.symlink_to(second.name)
                os.replace(next_link, current)
                meta = database.meta()
                self.assertEqual(meta["databaseVersion"], "second")
                self.assertEqual(meta["databaseInode"], str(current.stat().st_ino))
            finally:
                database.close()

    def test_invalid_database_has_no_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "invalid.sqlite"
            sqlite3.connect(path).close()
            with self.assertRaises(sqlite3.OperationalError):
                Database(str(path), ttl=0).meta()

    def test_connection_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "readonly")
            database = Database(str(path), ttl=0)
            with database.lock:
                with self.assertRaises(sqlite3.OperationalError):
                    database._connection().execute("CREATE TABLE forbidden (id TEXT)")

    def test_activation_refuses_invalid_next_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "staging").mkdir()
            current = root / "departures-current.sqlite"
            write_database(current, "current")
            write_database(root / "staging" / "departures-next.sqlite", "invalid", valid=False)
            with self.assertRaises(ValueError):
                activate_database(root)
            self.assertTrue((root / "staging" / "departures-next.sqlite").exists())
            database = Database(str(current), ttl=0)
            try:
                self.assertEqual(database.meta()["databaseVersion"], "current")
            finally:
                database.close()

    def test_activation_swaps_valid_database_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "staging").mkdir()
            write_database(root / "departures-current.sqlite", "current")
            write_database(root / "staging" / "departures-next.sqlite", "next")
            self.assertEqual(activate_database(root), "next")
            database = Database(str(root / "departures-current.sqlite"), ttl=0)
            try:
                self.assertEqual(database.meta()["databaseVersion"], "next")
                self.assertFalse((root / "staging" / "departures-next.sqlite").exists())
            finally:
                database.close()


class StaticDeparturesImportTests(unittest.TestCase):
    def test_known_package_stop_is_registered_without_matching_gtfs_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stop_data = root / "stop-data"
            (stop_data / "stops").mkdir(parents=True)
            (stop_data / "manifest.json").write_text(
                json.dumps({
                    "cities": [{
                        "id": "ennepetal-05954008",
                        "url": "stops/ennepetal-05954008.json"
                    }]
                }),
                encoding="utf-8"
            )
            (stop_data / "stops" / "ennepetal-05954008.json").write_text(
                json.dumps([{"id": "545562", "name": "Ennepetal, Seniorenheim"}]),
                encoding="utf-8"
            )
            database = sqlite3.connect(root / "departures.sqlite")
            try:
                database.execute(
                    "CREATE TABLE city_stops (city_id TEXT NOT NULL, stop_id TEXT NOT NULL, "
                    "PRIMARY KEY (city_id, stop_id))"
                )

                city_ids = populate_german_city_memberships(database, stop_data, set())

                self.assertEqual(city_ids, {"ennepetal-05954008"})
                self.assertEqual(
                    database.execute(
                        "SELECT city_id, stop_id FROM city_stops"
                    ).fetchall(),
                    [("ennepetal-05954008", "545562")]
                )
            finally:
                database.close()


class StaticDeparturesEndpointTests(unittest.TestCase):
    def test_health_meta_lines_and_board(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "endpoint")
            with StaticDeparturesHTTPServer(path) as server:
                health = server.get("/static-departures/health")
                self.assertTrue(health["ok"])
                self.assertEqual(health["database"]["databaseVersion"], "endpoint")
                self.assertEqual(server.get("/static-departures/meta")["databaseVersion"], "endpoint")
                lines = server.get("/static-departures/lines?cityID=dresden&stopID=stop-parent")["lines"]
                self.assertEqual(lines, [{"routeID": "route-1", "line": "7", "direction": "0"}])
                board = server.get(
                    "/static-departures/board?cityID=dresden&stopID=stop-parent"
                    "&from=2026-07-28T23:50:00%2B02:00&to=2026-07-29T00:10:00%2B02:00&limit=1"
                )["departures"]
                self.assertEqual(len(board), 1)
                self.assertEqual(board[0]["scheduledTime"], "23:55:00")
                self.assertEqual(board[0]["platform"], "1")
                self.assertFalse(board[0]["isRealtime"])
                overnight = server.get(
                    "/static-departures/board?cityID=dresden&stopID=terminal"
                    "&from=2026-07-29T01:00:00%2B02:00&to=2026-07-29T01:10:00%2B02:00&limit=1"
                )["departures"]
                self.assertEqual(len(overnight), 1)
                self.assertEqual(overnight[0]["serviceDate"], "2026-07-28")
                self.assertEqual(overnight[0]["scheduledTime"], "25:05:00")
                aliased = server.get("/static-departures/lines?cityID=koeln&stopID=stop-parent")
                self.assertEqual(aliased["cityID"], "koln")
                self.assertEqual(aliased["requestedCityID"], "koeln")
                self.assertEqual(aliased["lines"][0]["line"], "7")

    def test_known_stop_without_scheduled_departures_returns_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "endpoint")
            database = sqlite3.connect(path)
            try:
                database.execute(
                    "INSERT INTO city_stops VALUES ('ennepetal-05954008', '545562')"
                )
                database.commit()
            finally:
                database.close()

            with StaticDeparturesHTTPServer(path) as server:
                board = server.get(
                    "/static-departures/board?cityID=ennepetal-05954008&stopID=545562"
                )

            self.assertEqual(board["cityID"], "ennepetal-05954008")
            self.assertEqual(board["stopID"], "545562")
            self.assertEqual(board["departures"], [])

    def test_parallel_reads_continue_during_symlink_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second, current = root / "first.sqlite", root / "second.sqlite", root / "current.sqlite"
            write_database(first, "first")
            write_database(second, "second")
            current.symlink_to(first.name)
            with StaticDeparturesHTTPServer(current, ttl=0) as server:
                self.assertEqual(server.get("/static-departures/meta")["databaseVersion"], "first")
                results: list[str] = []

                def fetch_meta() -> str:
                    return str(server.get("/static-departures/meta")["databaseVersion"])

                with ThreadPoolExecutor(max_workers=8) as executor:
                    futures = [executor.submit(fetch_meta) for _ in range(20)]
                    next_link = root / "next.sqlite"
                    next_link.symlink_to(second.name)
                    os.replace(next_link, current)
                    futures.extend(executor.submit(fetch_meta) for _ in range(20))
                    for future in futures:
                        results.append(future.result(timeout=5))

                self.assertIn("second", results)
                self.assertEqual(server.get("/static-departures/meta")["databaseVersion"], "second")


if __name__ == "__main__":
    unittest.main()
