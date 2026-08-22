import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import static_departures_api
from import_static_departures_database import populate_german_city_memberships
from static_departures_api import Database, Handler
from apple_store_notification_store import AppleStoreNotificationStore
from gtfsrt_gateway import GatewayResponse
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
    def __init__(
        self,
        database_path: Path,
        ttl: float = 0.0,
        handler_overrides: dict[str, object] | None = None,
    ) -> None:
        self.database = Database(str(database_path), ttl=ttl)
        self.notification_store = AppleStoreNotificationStore(
            database_path.with_name("apple-store-notifications.sqlite3")
        )
        handler_attributes = {
            "database": self.database,
            "apple_store_notification_store": self.notification_store,
        }
        handler_attributes.update(handler_overrides or {})
        handler = type(
            "StaticDeparturesTestHandler",
            (Handler,),
            handler_attributes,
        )
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
        self.notification_store.close()

    def get(self, path: str) -> dict[str, object]:
        with urlopen(f"{self.base_url}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path: str, body: bytes) -> int:
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status
        except HTTPError as error:
            return error.code


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
                self.assertNotIn("databasePath", meta)
                self.assertNotIn("databaseInode", meta)
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
    def test_australia_realtime_alias_uses_existing_static_departures_proxy_path(self) -> None:
        class RecordingGateway:
            def __init__(self) -> None:
                self.paths: list[str] = []

            def handle(self, path: str, _query: dict[str, list[str]]) -> GatewayResponse:
                self.paths.append(path)
                return GatewayResponse(HTTPStatus.OK, {"ok": True})

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "australia-alias")
            gateway = RecordingGateway()
            with StaticDeparturesHTTPServer(
                path,
                handler_overrides={"australia_seq_vehicle_positions_gateway": gateway},
            ) as server:
                response = server.get(
                    "/static-departures/australia/seq/realtime/vehicle-positions?cityID=brisbane"
                )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(gateway.paths, ["/australia/seq/realtime/vehicle-positions"])

    def test_server_header_does_not_expose_runtime_version(self) -> None:
        self.assertEqual(
            Handler.version_string(Handler.__new__(Handler)),
            "HalteWecker",
        )

    def test_health_meta_lines_and_board(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "endpoint")
            with StaticDeparturesHTTPServer(path) as server:
                health = server.get("/static-departures/health")
                self.assertTrue(health["ok"])
                self.assertEqual(health["database"]["databaseVersion"], "endpoint")
                metadata = server.get("/static-departures/meta")
                self.assertEqual(metadata["databaseVersion"], "endpoint")
                for key in ("databasePath", "databaseDevice", "databaseInode", "databaseMTimeNS"):
                    self.assertNotIn(key, metadata)
                lines = server.get("/static-departures/lines?cityID=dresden&stopID=stop-parent")["lines"]
                self.assertEqual(lines, [{
                    "routeID": "route-1",
                    "line": "7",
                    "directionID": "0",
                    "direction": "Weixdorf",
                    "destination": "Weixdorf",
                    "destinationStopID": "terminal",
                    "directionKey": "route-1|direction:0|destination-stop:terminal",
                }])
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

    def test_apple_store_notification_without_signed_payload_returns_bad_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "apple-notification")
            with StaticDeparturesHTTPServer(path) as server:
                self.assertEqual(
                    server.post("/api/apple/store-notifications", b"{}"),
                    HTTPStatus.BAD_REQUEST,
                )

    def test_apple_store_notification_with_null_signed_payload_returns_bad_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "apple-notification")
            with StaticDeparturesHTTPServer(path) as server:
                self.assertEqual(
                    server.post(
                        "/api/apple/store-notifications",
                        b'{"signedPayload":null}',
                    ),
                    HTTPStatus.BAD_REQUEST,
                )

    def test_apple_store_notification_with_empty_signed_payload_returns_bad_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "apple-notification")
            with StaticDeparturesHTTPServer(path) as server:
                self.assertEqual(
                    server.post(
                        "/api/apple/store-notifications",
                        b'{"signedPayload":""}',
                    ),
                    HTTPStatus.BAD_REQUEST,
                )

    def test_apple_store_notification_with_arbitrary_signed_payload_returns_bad_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "apple-notification")
            with StaticDeparturesHTTPServer(path) as server:
                self.assertEqual(
                    server.post(
                        "/api/apple/store-notifications",
                        b'{"signedPayload":"test"}',
                    ),
                    HTTPStatus.BAD_REQUEST,
                )

    def test_apple_store_notification_with_invalid_json_returns_bad_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "apple-notification")
            with StaticDeparturesHTTPServer(path) as server:
                self.assertEqual(
                    server.post("/api/apple/store-notifications", b"not-json"),
                    HTTPStatus.BAD_REQUEST,
                )

    def test_toronto_namespaced_board_is_stop_scoped_and_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "toronto")
            database = sqlite3.connect(path)
            try:
                database.execute(
                    "CREATE TABLE city_departure_modes (city_id TEXT PRIMARY KEY, mode TEXT NOT NULL, timezone TEXT NOT NULL, stop_id_prefix TEXT NOT NULL DEFAULT '')"
                )
                database.execute(
                    "INSERT INTO city_departure_modes VALUES ('toronto', 'canonical', 'America/Toronto', '')"
                )
                database.execute(
                    "INSERT INTO raw_stops VALUES (?, ?, ?, ?, ?, ?)",
                    ("ttc-surface:100", "", "TTC Surface", "", 10, "ttc-surface:100"),
                )
                database.execute(
                    "INSERT INTO city_stops VALUES (?, ?)",
                    ("toronto", "ttc-surface:100"),
                )
                database.execute(
                    "INSERT INTO routes VALUES (?, ?, ?)",
                    ("ttc-surface:506", "506", "Carlaw"),
                )
                database.execute(
                    "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)",
                    ("ttc-surface:trip-1", "ttc-surface:service-1", "ttc-surface:506", "Carlaw", "0", "terminal"),
                )
                database.execute(
                    "INSERT INTO active_services VALUES (?, ?)",
                    ("ttc-surface:service-1", "20260728"),
                )
                database.execute(
                    "INSERT INTO stop_times VALUES (?, ?, ?, ?, ?)",
                    ("ttc-surface:trip-1", "ttc-surface:100", "08:00:00", 28_800, 1),
                )
                database.commit()
            finally:
                database.close()

            with StaticDeparturesHTTPServer(path) as server:
                board = server.get(
                    "/static-departures/board?cityID=toronto&stopID=ttc-surface:100&limit=1"
                )

            self.assertEqual(board["cityID"], "toronto")
            self.assertEqual(board["stopID"], "ttc-surface:100")
            self.assertEqual(len(board["departures"]), 1)
            self.assertEqual(board["departures"][0]["tripID"], "ttc-surface:trip-1")
            self.assertEqual(board["departures"][0]["stopID"], "ttc-surface:100")

    def test_translink_internal_prefix_is_removed_from_public_board(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "translink")
            database = sqlite3.connect(path)
            try:
                database.execute(
                    "CREATE TABLE city_departure_modes (city_id TEXT PRIMARY KEY, mode TEXT NOT NULL, timezone TEXT NOT NULL, stop_id_prefix TEXT NOT NULL DEFAULT '', identifier_prefix TEXT NOT NULL DEFAULT '')"
                )
                database.execute(
                    "INSERT INTO city_departure_modes VALUES ('vancouver', 'canonical', 'America/Vancouver', 'ca:', 'ca:')"
                )
                database.execute(
                    "INSERT INTO raw_stops VALUES (?, ?, ?, ?, ?, ?)",
                    ("ca:11535", "", "Seymour", "", 10, "ca:11535"),
                )
                database.execute("INSERT INTO city_stops VALUES ('vancouver', '11535')")
                database.execute("INSERT INTO routes VALUES ('ca:6612', '002', 'Macdonald')")
                database.execute(
                    "INSERT INTO trips VALUES ('ca:trip-1', 'ca:service-1', 'ca:6612', 'Burrard Station', '0', '')"
                )
                database.execute("INSERT INTO active_services VALUES ('ca:service-1', '20260728')")
                database.execute(
                    "INSERT INTO stop_times VALUES ('ca:trip-1', 'ca:11535', '08:00:00', 28800, 15)"
                )
                database.commit()
            finally:
                database.close()

            with StaticDeparturesHTTPServer(path) as server:
                board = server.get(
                    "/static-departures/board?cityID=vancouver&stopID=11535&limit=1"
                )
                lines = server.get(
                    "/static-departures/lines?cityID=vancouver&stopID=11535"
                )

            self.assertEqual(board["departures"][0]["tripID"], "trip-1")
            self.assertEqual(board["departures"][0]["routeID"], "6612")
            self.assertEqual(board["departures"][0]["stopID"], "11535")
            self.assertEqual(lines["lines"][0]["routeID"], "6612")

    def test_cta_internal_prefix_keeps_native_board_id_and_overflow_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "cta-chicago")
            database = sqlite3.connect(path)
            try:
                database.execute(
                    "CREATE TABLE city_departure_modes (city_id TEXT PRIMARY KEY, mode TEXT NOT NULL, timezone TEXT NOT NULL, stop_id_prefix TEXT NOT NULL DEFAULT '', identifier_prefix TEXT NOT NULL DEFAULT '')"
                )
                database.execute(
                    "INSERT INTO city_departure_modes VALUES ('chicago', 'canonical', 'America/Chicago', 'cta-chicago:', 'cta-chicago:')"
                )
                database.execute(
                    "INSERT INTO raw_stops VALUES (?, ?, ?, ?, ?, ?)",
                    ("cta-chicago:100", "", "CTA Stop", "", 10, "cta-chicago:100"),
                )
                database.execute("INSERT INTO city_stops VALUES ('chicago', '100')")
                database.execute(
                    "INSERT INTO routes VALUES (?, ?, ?)",
                    ("cta-chicago:route-1", "Blue", "CTA Blue Line"),
                )
                database.execute(
                    "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "cta-chicago:trip-1",
                        "cta-chicago:service-1",
                        "cta-chicago:route-1",
                        "O'Hare",
                        "0",
                        "cta-chicago:100",
                    ),
                )
                database.execute(
                    "INSERT INTO active_services VALUES (?, ?)",
                    ("cta-chicago:service-1", "20260810"),
                )
                database.execute(
                    "INSERT INTO stop_times VALUES (?, ?, ?, ?, ?)",
                    ("cta-chicago:trip-1", "cta-chicago:100", "25:30:00", 91_800, 1),
                )
                database.commit()
            finally:
                database.close()

            with StaticDeparturesHTTPServer(path) as server:
                board = server.get(
                    "/static-departures/board?cityID=chicago&stopID=100"
                    "&from=2026-08-11T01:20:00%2D05:00"
                    "&to=2026-08-11T01:40:00%2D05:00"
                    "&limit=1"
                )

            self.assertEqual(board["cityID"], "chicago")
            self.assertEqual(board["stopID"], "100")
            self.assertEqual(len(board["departures"]), 1)
            self.assertEqual(board["departures"][0]["scheduledTime"], "25:30:00")
            self.assertEqual(board["departures"][0]["tripID"], "trip-1")
            self.assertEqual(board["departures"][0]["routeID"], "route-1")

    def test_lines_are_topology_and_keep_multiple_destinations_without_active_services(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "topology")
            database = sqlite3.connect(path)
            try:
                database.executemany(
                    "INSERT INTO raw_stops VALUES (?, ?, ?, ?, ?, ?)",
                    [("terminal-b", "", "Destination B", "", 4, "terminal-b"),
                     ("terminal-c", "", "Destination C", "", 5, "terminal-c")],
                )
                database.executemany(
                    "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)",
                    [("trip-b", "future-only", "route-1", "Destination B", "0", "terminal-b"),
                     ("trip-c", "future-only", "route-1", "Destination C", "0", "terminal-c"),
                     ("trip-same-name", "future-only", "route-1", "Weixdorf", "0", "terminal-b")],
                )
                database.executemany(
                    "INSERT INTO stop_times VALUES (?, ?, ?, ?, ?)",
                     [("trip-b", "stop-platform", "02:00:00", 7200, 1),
                     ("trip-c", "stop-platform", "03:00:00", 10800, 1),
                     ("trip-same-name", "stop-platform", "04:00:00", 14400, 1)],
                )
                database.commit()
                lines = Database(str(path)).lines("dresden", "stop-parent")
            finally:
                database.close()

            destinations = {entry["destination"] for entry in lines}
            self.assertEqual(destinations, {"Weixdorf", "Destination B", "Destination C"})
            weixdorf_keys = {
                entry["directionKey"] for entry in lines if entry["destination"] == "Weixdorf"
            }
            self.assertEqual(len(weixdorf_keys), 2)

    def test_lines_use_terminal_stop_name_when_trip_headsign_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "empty-headsign")
            database = sqlite3.connect(path)
            try:
                database.execute("UPDATE trips SET headsign='' WHERE trip_id='trip-1'")
                database.commit()
                lines = Database(str(path)).lines("dresden", "stop-parent")
            finally:
                database.close()

            self.assertEqual(lines[0]["destination"], "Terminal")
            self.assertEqual(lines[0]["destinationStopID"], "terminal")

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


class StaticDeparturesStaticFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_root = static_departures_api.STATIC_DATA_ROOT

    def tearDown(self) -> None:
        static_departures_api.STATIC_DATA_ROOT = self._saved_root

    def _start(self, root: Path) -> StaticDeparturesHTTPServer:
        static_departures_api.STATIC_DATA_ROOT = str(root)
        path = root / "current.sqlite"
        write_database(path, "static")
        return StaticDeparturesHTTPServer(path)

    def _status(self, server: StaticDeparturesHTTPServer, url_path: str) -> tuple[int, dict[str, object]]:
        try:
            with urlopen(f"{server.base_url}{url_path}", timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_serves_stop_data_files_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "stops").mkdir()
            (root / "manifest.json").write_text('{"version":"dev"}', encoding="utf-8")
            (root / "stops" / "stockholm.json").write_text('[{"id":"11706"}]', encoding="utf-8")
            with self._start(root) as server:
                for prefix in ("/static-stop-data/", "/static-stop-data-dev/"):
                    status, manifest = self._status(server, f"{prefix}manifest.json")
                    self.assertEqual(status, 200)
                    self.assertEqual(manifest, {"version": "dev"})
                    status, stops = self._status(server, f"{prefix}stops/stockholm.json")
                    self.assertEqual(status, 200)
                    self.assertEqual(stops, [{"id": "11706"}])

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self._start(root) as server:
                for attempt in (
                    "/static-stop-data/../manifest.json",
                    "/static-stop-data/%2e%2e/manifest.json",
                    "/static-stop-data/a/../../etc/passwd",
                    "/static-stop-data-dev/../manifest.json",
                    "/static-stop-data-dev/%2e%2e/manifest.json",
                    "/static-stop-data-dev/a/../../etc/passwd",
                ):
                    status, _ = self._status(server, attempt)
                    self.assertEqual(status, 403, attempt)

    def test_rejects_non_json_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "notes.txt").write_text("hello", encoding="utf-8")
            with self._start(root) as server:
                for prefix in ("/static-stop-data/", "/static-stop-data-dev/"):
                    status, _ = self._status(server, f"{prefix}notes.txt")
                    self.assertEqual(status, 404)
                    status, _ = self._status(server, f"{prefix}missing.json")
                    self.assertEqual(status, 404)

    def test_static_serving_disabled_without_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            with self._start(root) as server:
                static_departures_api.STATIC_DATA_ROOT = ""
                for prefix in ("/static-stop-data/", "/static-stop-data-dev/"):
                    status, _ = self._status(server, f"{prefix}manifest.json")
                    self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
