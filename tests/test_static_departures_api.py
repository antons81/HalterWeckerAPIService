import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import static_departures_api
from build_german_departure_index import connect
from import_static_departures_database import add_external_gtfs, populate_german_city_memberships
from static_departures_api import Database, Handler
from apple_store_notification_store import AppleStoreNotificationStore
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


class _DeterministicThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128


class StaticDeparturesHTTPServer:
    def __init__(self, database_path: Path, ttl: float = 0.0) -> None:
        self.database = Database(str(database_path), ttl=ttl)
        self.notification_store = AppleStoreNotificationStore(
            database_path.with_name("apple-store-notifications.sqlite3")
        )
        handler = type(
            "StaticDeparturesTestHandler",
            (Handler,),
            {
                "database": self.database,
                "apple_store_notification_store": self.notification_store,
            },
        )
        self.server = _DeterministicThreadingHTTPServer(("127.0.0.1", 0), handler)
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

    def test_multi_provider_import_preserves_provider_scoped_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = connect(root / "departures.sqlite")
            database.execute(
                """
                CREATE TABLE city_departure_modes (
                    city_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    stop_id_prefix TEXT NOT NULL DEFAULT '',
                    identifier_prefix TEXT NOT NULL DEFAULT ''
                ) WITHOUT ROWID
                """
            )
            sources = [
                {
                    "id": "ttc-surface",
                    "cities": "ttc-cities.json",
                    "identifierPrefix": "ttc-surface:",
                    "namespace": "ttc-surface:",
                    "timezone": "America/Toronto",
                },
                {
                    "id": "ttc-subway",
                    "cities": "ttc-cities.json",
                    "identifierPrefix": "ttc-subway:",
                    "namespace": "ttc-subway:",
                    "timezone": "America/Toronto",
                },
            ]
            city = {"id": "toronto", "packageMode": "external"}
            try:
                with (
                    mock.patch("import_static_departures_database.load_external_gtfs_sources", return_value=sources),
                    mock.patch("import_static_departures_database.validate_external_gtfs_source"),
                    mock.patch("import_static_departures_database.load_external_cities", return_value=[city]),
                    mock.patch(
                        "import_static_departures_database.authenticated_external_request",
                        side_effect=[("surface.zip", {}), ("subway.zip", {})],
                    ),
                    mock.patch("import_static_departures_database.load_gtfs_archive", return_value=mock.Mock()),
                    mock.patch("import_static_departures_database.agency_scoped_archive", side_effect=lambda archive, _: archive),
                    mock.patch("import_static_departures_database.populate_gtfs"),
                    mock.patch("import_static_departures_database.resolve_canonical_stops"),
                    mock.patch(
                        "import_static_departures_database.populate_provider_city_memberships",
                        return_value={"toronto"},
                    ),
                    mock.patch("import_static_departures_database.populate_active_services"),
                    mock.patch("import_static_departures_database.update_terminal_stops"),
                ):
                    add_external_gtfs(
                        database,
                        root / "stop-data",
                        {"ttc-surface": "surface.zip", "ttc-subway": "subway.zip"},
                        repository_root=root,
                        sources_path=root / "sources.json",
                        dates=[date(2026, 7, 28)],
                    )

                self.assertEqual(
                    database.execute(
                        """
                        SELECT provider_id, city_id, stop_id_prefix
                        FROM provider_city_modes
                        ORDER BY provider_id
                        """
                    ).fetchall(),
                    [
                        ("ttc-subway", "toronto", "ttc-subway:"),
                        ("ttc-surface", "toronto", "ttc-surface:"),
                    ],
                )
                self.assertEqual(
                    database.execute(
                        "SELECT stop_id_prefix FROM city_departure_modes WHERE city_id='toronto'"
                    ).fetchone(),
                    ("",),
                )
            finally:
                database.close()


class StaticDeparturesEndpointTests(unittest.TestCase):
    def test_server_header_does_not_expose_runtime_version(self) -> None:
        self.assertEqual(
            Handler.version_string(Handler.__new__(Handler)),
            "HalteWecker",
        )

    def test_legacy_city_departures_route_is_provider_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "legacy-compatibility")
            database = sqlite3.connect(path)
            try:
                database.executescript(
                    """
                    CREATE TABLE city_departure_modes (
                        city_id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        stop_id_prefix TEXT NOT NULL DEFAULT '',
                        identifier_prefix TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE provider_city_modes (
                        provider_id TEXT NOT NULL,
                        city_id TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        stop_id_prefix TEXT NOT NULL DEFAULT '',
                        identifier_prefix TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (provider_id, city_id)
                    );
                    CREATE TABLE provider_city_stops (
                        provider_id TEXT NOT NULL,
                        city_id TEXT NOT NULL,
                        stop_id TEXT NOT NULL,
                        PRIMARY KEY (provider_id, city_id, stop_id)
                    );
                    CREATE TABLE provider_entities (
                        entity_type TEXT NOT NULL,
                        provider_id TEXT NOT NULL,
                        key_1 TEXT NOT NULL,
                        key_2 TEXT NOT NULL DEFAULT '',
                        key_3 TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (entity_type, provider_id, key_1, key_2, key_3)
                    ) WITHOUT ROWID;
                    """
                )
                database.executemany(
                    "INSERT INTO city_departure_modes VALUES (?, 'canonical', ?, ?, ?)",
                    [
                        ("vaxjo", "Europe/Stockholm", "se:", "se:"),
                        ("schipkau", "Europe/Berlin", "", "germany:"),
                        ("toronto", "America/Toronto", "", ""),
                    ],
                )
                database.executemany(
                    "INSERT INTO provider_city_modes VALUES (?, ?, 'canonical', ?, ?, ?)",
                    [
                        ("sweden", "vaxjo", "Europe/Stockholm", "se:", "se:"),
                        ("germany", "schipkau", "Europe/Berlin", "", "germany:"),
                        ("ttc-surface", "toronto", "America/Toronto", "ttc-surface:", ""),
                        ("ttc-subway", "toronto", "America/Toronto", "ttc-subway:", ""),
                    ],
                )
                database.executemany(
                    "INSERT INTO city_stops VALUES (?, '74424')",
                    [("vaxjo",), ("schipkau",)],
                )
                database.executemany(
                    "INSERT INTO provider_city_stops VALUES (?, ?, '74424')",
                    [("sweden", "vaxjo"), ("germany", "schipkau")],
                )
                database.executemany(
                    "INSERT INTO city_stops VALUES ('toronto', ?)",
                    [("ttc-surface:100",), ("ttc-subway:100",)],
                )
                database.executemany(
                    "INSERT INTO provider_city_stops VALUES (?, 'toronto', ?)",
                    [
                        ("ttc-surface", "ttc-surface:100"),
                        ("ttc-subway", "ttc-subway:100"),
                    ],
                )
                database.executemany(
                    "INSERT INTO raw_stops VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("se:74424", "", "Växjö Busstation", "", 10, "74424"),
                        ("se:74424-platform", "se:74424", "Växjö Busstation", "1", 11, "74424"),
                        ("74424", "", "Schipkau, Siedlung", "", 12, "74424"),
                        ("ttc-surface:100", "", "Toronto Surface", "", 13, "ttc-surface:100"),
                        ("ttc-subway:100", "", "Toronto Subway", "", 14, "ttc-subway:100"),
                    ],
                )
                database.executemany(
                    "INSERT INTO routes VALUES (?, ?, ?)",
                    [
                        ("se:290", "290", "Växjö"),
                        ("germany:616", "616", "Senftenberg"),
                        ("ttc-surface:1", "1", "Surface"),
                        ("ttc-subway:1", "1", "Subway"),
                    ],
                )
                database.executemany(
                    "INSERT INTO trips VALUES (?, ?, ?, ?, '0', '')",
                    [
                        ("se:trip-290", "se:service-290", "se:290", "Växjö"),
                        ("germany:trip-616", "germany:service-616", "germany:616", "Senftenberg"),
                        ("ttc-surface:trip-1", "ttc-surface:service-1", "ttc-surface:1", "Surface"),
                        ("ttc-subway:trip-1", "ttc-subway:service-1", "ttc-subway:1", "Subway"),
                    ],
                )
                service_date = datetime.now(ZoneInfo("Europe/Stockholm")).date().strftime("%Y%m%d")
                toronto_service_date = datetime.now(ZoneInfo("America/Toronto")).date().strftime("%Y%m%d")
                database.executemany(
                    "INSERT INTO active_services VALUES (?, ?)",
                    [
                        ("se:service-290", service_date),
                        ("germany:service-616", service_date),
                        ("ttc-surface:service-1", toronto_service_date),
                        ("ttc-subway:service-1", toronto_service_date),
                    ],
                )
                database.executemany(
                    "INSERT INTO stop_times VALUES (?, ?, ?, ?, 1)",
                    [
                        ("se:trip-290", "se:74424-platform", "12:07:00", 43_620),
                        ("germany:trip-616", "74424", "12:05:00", 43_500),
                        ("ttc-surface:trip-1", "ttc-surface:100", "08:01:00", 28_860),
                        ("ttc-subway:trip-1", "ttc-subway:100", "08:02:00", 28_920),
                    ],
                )
                database.executemany(
                    "INSERT INTO provider_entities(entity_type, provider_id, key_1) VALUES (?, ?, ?)",
                    [
                        ("raw_stops", "sweden", "se:74424"),
                        ("raw_stops", "sweden", "se:74424-platform"),
                        ("raw_stops", "germany", "74424"),
                        ("raw_stops", "ttc-surface", "ttc-surface:100"),
                        ("raw_stops", "ttc-subway", "ttc-subway:100"),
                        ("trips", "sweden", "se:trip-290"),
                        ("trips", "germany", "germany:trip-616"),
                        ("trips", "ttc-surface", "ttc-surface:trip-1"),
                        ("trips", "ttc-subway", "ttc-subway:trip-1"),
                    ],
                )
                database.commit()
            finally:
                database.close()

            with StaticDeparturesHTTPServer(path) as server:
                vaxjo = server.get("/departures/vaxjo.json")
                schipkau = server.get("/departures/schipkau.json")
                toronto = server.get("/departures/toronto.json")
                unknown_status = self._http_status(server, "/departures/not-a-city.json")
                traversal_status = self._http_status(server, "/departures/../current.json")

            self.assertEqual(vaxjo["timezone"], "Europe/Stockholm")
            self.assertEqual(
                set(vaxjo),
                {"generatedAt", "timezone", "stops", "platforms"},
            )
            self.assertEqual([item["r"] for item in vaxjo["stops"]["74424"]], ["290"])
            self.assertEqual(vaxjo["stops"]["74424"][0]["s"], "74424-platform")
            self.assertTrue(
                {"t", "r", "h", "d", "p", "q", "s"}.issubset(
                    vaxjo["stops"]["74424"][0]
                )
            )
            self.assertEqual(vaxjo["platforms"]["74424"], ["74424-platform"])
            self.assertEqual([item["r"] for item in schipkau["stops"]["74424"]], ["616"])
            self.assertEqual(
                [item["r"] for item in toronto["stops"]["ttc-surface:100"]],
                ["ttc-surface:1"],
            )
            self.assertEqual(
                [item["r"] for item in toronto["stops"]["ttc-subway:100"]],
                ["ttc-subway:1"],
            )
            self.assertEqual(
                toronto["stops"]["ttc-surface:100"][0]["t"],
                "ttc-surface:trip-1",
            )
            self.assertEqual(
                toronto["stops"]["ttc-subway:100"][0]["t"],
                "ttc-subway:trip-1",
            )
            self.assertEqual(unknown_status, HTTPStatus.NOT_FOUND)
            self.assertEqual(traversal_status, HTTPStatus.NOT_FOUND)

    def test_legacy_germany_city_without_provider_modes_uses_exact_city_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "legacy-germany-compatibility")
            local_date = datetime.now(ZoneInfo("Europe/Berlin")).date().strftime("%Y%m%d")
            database = sqlite3.connect(path)
            try:
                database.executescript(
                    """
                    CREATE TABLE provider_city_modes (
                        provider_id TEXT NOT NULL,
                        city_id TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        stop_id_prefix TEXT NOT NULL DEFAULT '',
                        identifier_prefix TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (provider_id, city_id)
                    );
                    CREATE TABLE provider_city_stops (
                        provider_id TEXT NOT NULL,
                        city_id TEXT NOT NULL,
                        stop_id TEXT NOT NULL,
                        PRIMARY KEY (provider_id, city_id, stop_id)
                    );
                    CREATE TABLE provider_entities (
                        entity_type TEXT NOT NULL,
                        provider_id TEXT NOT NULL,
                        key_1 TEXT NOT NULL,
                        key_2 TEXT NOT NULL DEFAULT '',
                        key_3 TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (entity_type, provider_id, key_1, key_2, key_3)
                    );
                    INSERT INTO city_stops VALUES ('berlin', '74424');
                    INSERT INTO city_stops VALUES ('berlin', 'vbb:74424');
                    INSERT INTO city_stops VALUES ('vaxjo', 'se:74424');
                    INSERT INTO provider_city_stops VALUES ('germany', 'berlin', '74424');
                    INSERT INTO provider_city_stops VALUES ('sweden', 'vaxjo', 'se:74424');
                    INSERT INTO provider_city_stops VALUES ('vbb', 'berlin', '74424');
                    INSERT INTO provider_city_modes VALUES ('vbb', 'berlin', 'canonical', 'Europe/Berlin', 'vbb:', 'vbb:');
                    INSERT INTO raw_stops VALUES ('74424', '', 'Berlin Stop', '', 10, '74424');
                    INSERT INTO raw_stops VALUES ('se:74424', '', 'Växjö Busstation', '', 11, 'se:74424');
                    INSERT INTO raw_stops VALUES ('vbb:74424', '', 'Berlin VBB Stop', '', 12, 'vbb:74424');
                    INSERT INTO routes VALUES ('616', '616', 'Senftenberg');
                    INSERT INTO routes VALUES ('se:290', '290', 'Växjö');
                    INSERT INTO routes VALUES ('vbb:617', '617', 'Berlin');
                    INSERT INTO trips VALUES ('germany-trip-616', 'germany-service', '616', 'Senftenberg', '0', '');
                    INSERT INTO trips VALUES ('sweden-trip-290', 'sweden-service', 'se:290', 'Växjö', '0', '');
                    INSERT INTO trips VALUES ('vbb-trip-617', 'vbb-service', 'vbb:617', 'Berlin', '0', '');
                    INSERT INTO active_services VALUES ('germany-service', '%s');
                    INSERT INTO active_services VALUES ('sweden-service', '%s');
                    INSERT INTO active_services VALUES ('vbb-service', '%s');
                    INSERT INTO stop_times VALUES ('germany-trip-616', '74424', '12:05:00', 43500, 1);
                    INSERT INTO stop_times VALUES ('sweden-trip-290', 'se:74424', '12:07:00', 43620, 1);
                    INSERT INTO stop_times VALUES ('vbb-trip-617', 'vbb:74424', '12:08:00', 43680, 1);
                    INSERT INTO provider_entities(entity_type, provider_id, key_1)
                    VALUES ('raw_stops', 'vbb', 'vbb:74424'), ('trips', 'vbb', 'vbb-trip-617');
                    """ % (local_date, local_date, local_date)
                )
                database.commit()
            finally:
                database.close()

            with StaticDeparturesHTTPServer(path) as server:
                berlin = server.get("/departures/berlin.json")

            self.assertEqual(berlin["timezone"], "Europe/Berlin")
            self.assertEqual([item["r"] for item in berlin["stops"]["74424"]], ["616"])
            self.assertEqual(
                [item["r"] for item in berlin["stops"]["vbb:74424"]],
                ["vbb:617"],
            )
            self.assertNotIn("se:74424", berlin["stops"])

    def test_legacy_city_without_provider_ownership_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "missing-ownership")
            database = sqlite3.connect(path)
            try:
                database.execute("INSERT INTO city_stops VALUES ('fixture', 'stop-parent')")
                database.execute(
                    """
                    CREATE TABLE provider_city_stops (
                        provider_id TEXT NOT NULL,
                        city_id TEXT NOT NULL,
                        stop_id TEXT NOT NULL,
                        PRIMARY KEY (provider_id, city_id, stop_id)
                    )
                    """
                )
                database.execute(
                    "INSERT INTO provider_city_stops VALUES ('unknown-provider', 'fixture', 'stop-parent')"
                )
                database.commit()
            finally:
                database.close()

            database = Database(str(path), ttl=0)
            try:
                with self.assertRaises(static_departures_api.RuntimeUnavailable):
                    database.legacy_city_departures("fixture")
            finally:
                database.close()

    @staticmethod
    def _http_status(server: "StaticDeparturesHTTPServer", path: str) -> int:
        try:
            with urlopen(f"{server.base_url}{path}", timeout=5) as response:
                return response.status
        except HTTPError as error:
            return error.code

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

    def test_bounded_board_queries_only_the_requested_number_of_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "bounded-limit")
            with StaticDeparturesHTTPServer(path) as server:
                with mock.patch.object(server.database, "board", wraps=server.database.board) as board:
                    for limit in (1, 24, 100):
                        for upper_bound in ("", "&to=2026-07-29T00:10:00%2B02:00"):
                            result = server.get(
                                "/static-departures/board?cityID=dresden&stopID=stop-parent"
                                "&from=2026-07-28T23:50:00%2B02:00"
                                + upper_bound + f"&limit={limit}"
                            )["departures"]
                            self.assertEqual(board.call_args.args[2], limit)
                            self.assertEqual(len(result), 1)
                            self.assertEqual(result[0]["scheduledTime"], "23:55:00")

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

    def test_public_stop_ids_resolve_through_provider_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "norway-public-stop-id")
            database = sqlite3.connect(path)
            try:
                database.execute(
                    """
                    CREATE TABLE city_departure_modes (
                        city_id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        stop_id_prefix TEXT NOT NULL DEFAULT '',
                        identifier_prefix TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                database.executemany(
                    "INSERT INTO city_departure_modes VALUES (?, ?, ?, ?, ?)",
                    [
                        ("alesund", "canonical", "Europe/Oslo", "no:", "no:"),
                        ("vasteras", "canonical", "Europe/Stockholm", "se:", ""),
                        ("vaxjo", "canonical", "Europe/Stockholm", "se:", ""),
                        ("stockholm", "canonical", "Europe/Stockholm", "se:", ""),
                    ],
                )
                database.execute(
                    """
                    CREATE TABLE provider_city_modes (
                        provider_id TEXT NOT NULL,
                        city_id TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        stop_id_prefix TEXT NOT NULL DEFAULT '',
                        identifier_prefix TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (provider_id, city_id)
                    )
                    """
                )
                database.executemany(
                    "INSERT INTO provider_city_modes VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("norway", "alesund", "canonical", "Europe/Oslo", "no:", "no:"),
                        ("sweden", "vasteras", "canonical", "Europe/Stockholm", "se:", ""),
                        ("sweden", "vaxjo", "canonical", "Europe/Stockholm", "se:", ""),
                        ("sweden", "stockholm", "canonical", "Europe/Stockholm", "se:", ""),
                    ],
                )
                database.execute(
                    """
                    CREATE TABLE provider_city_stops (
                        provider_id TEXT NOT NULL,
                        city_id TEXT NOT NULL,
                        stop_id TEXT NOT NULL,
                        PRIMARY KEY (provider_id, city_id, stop_id)
                    )
                    """
                )
                database.executemany(
                    "INSERT INTO provider_city_stops VALUES (?, ?, ?)",
                    [
                        ("norway", "alesund", "NSR:StopPlace:37480"),
                        ("norway", "alesund", "no:NSR:StopPlace:37480"),
                        ("sweden", "vasteras", "1049"),
                        ("sweden", "vasteras", "se:1049"),
                        ("sweden", "vaxjo", "74424"),
                        ("sweden", "stockholm", "9022050012574002"),
                        ("sweden", "stockholm", "se:9022050012574002"),
                        ("germany", "schipkau", "74424"),
                    ],
                )
                database.executemany(
                    "INSERT INTO city_stops VALUES (?, ?)",
                    [
                        ("alesund", "NSR:StopPlace:37480"),
                        ("alesund", "foreign:StopPlace:37480"),
                        ("vasteras", "1049"),
                        ("vasteras", "se:1049"),
                        ("vaxjo", "74424"),
                        ("stockholm", "9022050012574002"),
                        ("stockholm", "se:9022050012574002"),
                        ("schipkau", "74424"),
                    ],
                )
                database.execute(
                    """
                    CREATE TABLE provider_entities (
                        entity_type TEXT NOT NULL,
                        provider_id TEXT NOT NULL,
                        key_1 TEXT NOT NULL,
                        key_2 TEXT NOT NULL DEFAULT '',
                        key_3 TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (entity_type, provider_id, key_1, key_2, key_3)
                    ) WITHOUT ROWID
                    """
                )
                database.executemany(
                    "INSERT INTO raw_stops VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            "no:NSR:StopPlace:37480",
                            "",
                            "Ålesund rutebilstasjon",
                            "",
                            10,
                            "no:NSR:StopPlace:37480",
                        ),
                        (
                            "no:foreign:StopPlace:37480",
                            "",
                            "Unowned Stop",
                            "",
                            11,
                            "no:foreign:StopPlace:37480",
                        ),
                        ("1049", "", "Västerås Central", "", 12, "1049"),
                        ("258063", "1049", "Västerås Central Platform", "", 13, "1049"),
                        ("se:1049", "", "Västerås Central duplicate", "", 14, "se:1049"),
                        ("74424", "", "Schipkau, Siedlung", "", 15, "74424"),
                        ("74424-617-child", "74424", "Schipkau, Siedlung, Mast 2", "", 16, "74424"),
                        ("se:74424-platform", "", "Växjö Busstation", "1", 17, "74424"),
                        ("se:9022050012574002", "se:12574", "Ronna skola", "", 0, "se:12574"),
                    ],
                )
                database.execute("INSERT INTO routes VALUES (?, ?, ?)", ("no:1", "1", "Airport"))
                database.execute("INSERT INTO routes VALUES (?, ?, ?)", ("17294", "3", "Västerås"))
                database.execute("INSERT INTO routes VALUES (?, ?, ?)", ("germany:616", "616", "Senftenberg"))
                database.execute("INSERT INTO routes VALUES (?, ?, ?)", ("germany:617", "617", "Großräschen"))
                database.execute("INSERT INTO routes VALUES (?, ?, ?)", ("se:290", "290", "Växjö"))
                database.execute("INSERT INTO routes VALUES (?, ?, ?)", ("se:752", "752", "Stockholm"))
                database.execute(
                    "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)",
                    ("no:trip-1", "no:service-1", "no:1", "Airport", "0", ""),
                )
                database.execute(
                    "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)",
                    ("1371673", "service-2", "17294", "Västerås", "0", ""),
                )
                database.execute(
                    "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)",
                    ("trip-616", "germany:service-616", "germany:616", "Senftenberg", "0", ""),
                )
                database.execute(
                    "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)",
                    ("trip-617", "germany:service-617", "germany:617", "Großräschen", "0", ""),
                )
                database.execute(
                    "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)",
                    ("se:trip-290", "se:service-290", "se:290", "Växjö", "0", ""),
                )
                database.execute(
                    "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)",
                    ("se:trip-752", "se:service-752", "se:752", "Stockholm", "0", ""),
                )
                database.executemany(
                    "INSERT INTO active_services VALUES (?, ?)",
                    [
                        ("no:service-1", "20261013"),
                        ("service-2", "20261013"),
                        ("germany:service-616", "20261013"),
                        ("germany:service-617", "20261013"),
                        ("se:service-290", "20261013"),
                        ("se:service-752", "20261017"),
                    ],
                )
                database.executemany(
                    "INSERT INTO stop_times VALUES (?, ?, ?, ?, ?)",
                    [
                        ("no:trip-1", "no:NSR:StopPlace:37480", "12:15:00", 44_100, 1),
                        ("no:trip-1", "no:foreign:StopPlace:37480", "12:20:00", 44_400, 1),
                        ("1371673", "258063", "04:23:00", 15_780, 1),
                        ("trip-616", "74424", "12:05:00", 43_500, 1),
                        ("trip-617", "74424-617-child", "12:10:00", 43_800, 1),
                        ("se:trip-290", "se:74424-platform", "12:07:00", 43_620, 1),
                        ("se:trip-752", "se:9022050012574002", "06:51:15", 24_675, 1),
                    ],
                )
                database.executemany(
                    "INSERT INTO provider_entities(entity_type, provider_id, key_1) VALUES (?, ?, ?)",
                    [
                        ("raw_stops", "norway", "no:NSR:StopPlace:37480"),
                        ("raw_stops", "norway", "no:foreign:StopPlace:37480"),
                        ("raw_stops", "sweden", "1049"),
                        ("raw_stops", "sweden", "258063"),
                        ("raw_stops", "sweden", "se:1049"),
                        ("raw_stops", "germany", "74424"),
                        ("raw_stops", "germany", "74424-617-child"),
                        ("raw_stops", "sweden", "se:74424-platform"),
                        ("raw_stops", "sweden", "se:9022050012574002"),
                        ("trips", "norway", "no:trip-1"),
                        ("trips", "sweden", "1371673"),
                        ("trips", "germany", "trip-616"),
                        ("trips", "germany", "trip-617"),
                        ("trips", "sweden", "se:trip-290"),
                        ("trips", "sweden", "se:trip-752"),
                    ],
                )
                database.commit()
            finally:
                database.close()

            with StaticDeparturesHTTPServer(path) as server:
                board = server.get(
                    "/static-departures/board?cityID=alesund&stopID=NSR:StopPlace:37480"
                    "&from=2026-10-13T12:00:00%2B02:00&to=2026-10-13T12:30:00%2B02:00"
                )
                foreign = server.get(
                    "/static-departures/board?cityID=alesund&stopID=foreign:StopPlace:37480"
                    "&from=2026-10-13T12:00:00%2B02:00&to=2026-10-13T12:30:00%2B02:00"
                )
                sweden = server.get(
                    "/static-departures/board?cityID=vasteras&stopID=1049"
                    "&from=2026-10-13T04:00:00%2B02:00&to=2026-10-13T05:00:00%2B02:00"
                )
                vaxjo = server.get(
                    "/static-departures/board?cityID=vaxjo&stopID=74424"
                    "&from=2026-10-13T12:00:00%2B02:00&to=2026-10-13T12:30:00%2B02:00"
                )
                schipkau = server.get(
                    "/static-departures/board?cityID=schipkau&stopID=74424"
                    "&from=2026-10-13T12:00:00%2B02:00&to=2026-10-13T12:30:00%2B02:00"
                )
                vaxjo_lines = server.get("/static-departures/lines?cityID=vaxjo&stopID=74424")
                schipkau_lines = server.get("/static-departures/lines?cityID=schipkau&stopID=74424")
                stockholm_lines = server.get(
                    "/static-departures/lines?cityID=stockholm&stopID=9022050012574002"
                )
                stockholm_board = server.get(
                    "/static-departures/board?cityID=stockholm&stopID=9022050012574002"
                    "&from=2026-10-17T00:00:00%2B02:00&to=2026-10-18T00:00:00%2B02:00"
                )

            self.assertEqual(len(board["departures"]), 1)
            self.assertEqual(board["departures"][0]["scheduledTime"], "12:15:00")
            self.assertEqual(board["departures"][0]["stopID"], "NSR:StopPlace:37480")
            self.assertEqual(foreign["departures"], [])
            self.assertEqual(len(sweden["departures"]), 1)
            self.assertEqual(sweden["departures"][0]["scheduledTime"], "04:23:00")
            self.assertEqual(
                [(departure["line"], departure["destination"]) for departure in vaxjo["departures"]],
                [("290", "Växjö")],
            )
            self.assertEqual([line["line"] for line in vaxjo_lines["lines"]], ["290"])
            self.assertEqual(
                [departure["line"] for departure in schipkau["departures"]],
                ["616", "617"],
            )
            self.assertEqual(
                {line["line"] for line in schipkau_lines["lines"]},
                {"616", "617"},
            )
            self.assertEqual([line["line"] for line in stockholm_lines["lines"]], ["752"])
            self.assertEqual([departure["line"] for departure in stockholm_board["departures"]], ["752"])
            self.assertEqual(stockholm_board["departures"][0]["scheduledTime"], "06:51:15")

            database = Database(str(path))
            try:
                foreign_trip = database.trip_details("vaxjo", "trip-616", temp)
                german_trip = database.trip_details("schipkau", "trip-616", temp)
                swedish_trip = database.trip_details("vasteras", "1371673", temp)
                vaxjo_swedish_trip = database.trip_details("vaxjo", "se:trip-290", temp)
            finally:
                database.close()
            self.assertIsNone(foreign_trip)
            self.assertEqual(german_trip["line"], "616")
            self.assertEqual(swedish_trip["line"], "3")
            self.assertEqual(vaxjo_swedish_trip["line"], "290")

    def test_multi_provider_departures_never_fall_back_to_global_raw_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current.sqlite"
            write_database(path, "provider-prefix-collision")
            database = sqlite3.connect(path)
            try:
                database.execute(
                    """
                    CREATE TABLE city_departure_modes (
                        city_id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        stop_id_prefix TEXT NOT NULL DEFAULT '',
                        identifier_prefix TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                database.execute(
                    """
                    CREATE TABLE provider_city_modes (
                        provider_id TEXT NOT NULL,
                        city_id TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        stop_id_prefix TEXT NOT NULL DEFAULT '',
                        identifier_prefix TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (provider_id, city_id)
                    )
                    """
                )
                database.executemany(
                    "INSERT INTO city_departure_modes VALUES (?, ?, ?, ?, ?)",
                    [
                        ("toronto", "canonical", "America/Toronto", "", ""),
                        ("germany", "canonical", "Europe/Berlin", "", ""),
                        ("single-provider", "canonical", "UTC", "single:", "single:"),
                    ],
                )
                database.executemany(
                    "INSERT INTO provider_city_modes VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("ttc-surface", "toronto", "canonical", "America/Toronto", "ttc-surface:", ""),
                        ("ttc-subway", "toronto", "canonical", "America/Toronto", "ttc-subway:", ""),
                        ("germany", "germany", "canonical", "Europe/Berlin", "", ""),
                        ("single-provider", "single-provider", "canonical", "UTC", "single:", "single:"),
                    ],
                )
                database.executemany(
                    "INSERT INTO raw_stops VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("100", "", "Eberlestraße", "", 20, "100"),
                        ("ttc-surface:100", "", "TTC Surface", "", 21, "ttc-surface:100"),
                        ("ttc-subway:100", "", "TTC Subway", "", 22, "ttc-subway:100"),
                        ("single:100", "", "Single Provider", "", 23, "single:100"),
                    ],
                )
                database.executemany(
                    "INSERT INTO city_stops VALUES (?, ?)",
                    [
                        ("germany", "100"),
                        ("toronto", "ttc-surface:100"),
                        ("toronto", "ttc-subway:100"),
                        ("single-provider", "single:100"),
                    ],
                )
                database.executemany(
                    "INSERT INTO routes VALUES (?, ?, ?)",
                    [
                        ("germany:134", "134", "Eberlestraße"),
                        ("ttc-surface:506", "506", "Surface"),
                        ("ttc-subway:1", "1", "Subway"),
                        ("single:7", "7", "Single"),
                    ],
                )
                database.executemany(
                    "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("germany:trip", "germany:service", "germany:134", "Eberlestraße", "0", ""),
                        ("ttc-surface:trip", "ttc-surface:service", "ttc-surface:506", "Surface", "0", ""),
                        ("ttc-subway:trip", "ttc-subway:service", "ttc-subway:1", "Subway", "0", ""),
                        ("single:trip", "single:service", "single:7", "Single", "0", ""),
                    ],
                )
                database.executemany(
                    "INSERT INTO active_services VALUES (?, ?)",
                    [
                        ("germany:service", "20260728"),
                        ("ttc-surface:service", "20260728"),
                        ("ttc-subway:service", "20260728"),
                        ("single:service", "20260728"),
                    ],
                )
                database.executemany(
                    "INSERT INTO stop_times VALUES (?, ?, ?, ?, ?)",
                    [
                        ("germany:trip", "100", "08:00:00", 28_800, 1),
                        ("ttc-surface:trip", "ttc-surface:100", "08:05:00", 29_100, 1),
                        ("ttc-subway:trip", "ttc-subway:100", "08:10:00", 29_400, 1),
                        ("single:trip", "single:100", "08:15:00", 29_700, 1),
                    ],
                )
                database.commit()
            finally:
                database.close()

            legacy = Database(str(path))
            try:
                toronto_scope = legacy._external_stop_time_ids("toronto", "100")
                toronto = legacy.external_departures_for(
                    "toronto",
                    "100",
                    10,
                    datetime(2026, 7, 28, 7, 0, tzinfo=ZoneInfo("America/Toronto")),
                    "America/Toronto",
                )
                germany = legacy.external_departures_for(
                    "germany",
                    "100",
                    10,
                    datetime(2026, 7, 28, 7, 0, tzinfo=ZoneInfo("Europe/Berlin")),
                    "Europe/Berlin",
                )
                single = legacy.external_departures_for(
                    "single-provider",
                    "100",
                    10,
                    datetime(2026, 7, 28, 7, 0, tzinfo=ZoneInfo("UTC")),
                    "UTC",
                )
                board_from = datetime(2026, 7, 28, 7, 0, tzinfo=ZoneInfo("America/Toronto"))
                toronto_lines = legacy.lines("toronto", "100")
                surface_lines = legacy.lines("toronto", "ttc-surface:100")
                subway_lines = legacy.lines("toronto", "ttc-subway:100")
                germany_lines = legacy.lines("germany", "100")
                single_lines = legacy.lines("single-provider", "single:100")
                toronto_board = legacy.board("toronto", "100", 10, board_from, board_from.replace(hour=9))
                surface_board = legacy.board("toronto", "ttc-surface:100", 10, board_from, board_from.replace(hour=9))
                subway_board = legacy.board("toronto", "ttc-subway:100", 10, board_from, board_from.replace(hour=9))
                germany_board = legacy.board("germany", "100", 10, board_from.astimezone(ZoneInfo("Europe/Berlin")).replace(hour=7), board_from.astimezone(ZoneInfo("Europe/Berlin")).replace(hour=9))
                foreign_lines = legacy.lines("toronto", "foreign:100")
                foreign_board = legacy.board("toronto", "foreign:100", 10, board_from, board_from.replace(hour=9))
            finally:
                legacy.close()

            self.assertEqual(
                [(row["routeID"], row["stopID"]) for row in toronto],
                [("ttc-surface:506", "100"), ("ttc-subway:1", "100")],
            )
            self.assertEqual(toronto_scope, ("ttc-subway:100", "ttc-surface:100"))
            self.assertNotIn("134", {str(row["routeID"]) for row in toronto})
            self.assertEqual([(row["routeID"], row["stopID"]) for row in germany], [("germany:134", "100")])
            self.assertEqual([(row["routeID"], row["stopID"]) for row in single], [("7", "100")])
            self.assertEqual(
                {row["routeID"] for row in toronto_lines},
                {"ttc-surface:506", "ttc-subway:1"},
            )
            self.assertEqual([row["routeID"] for row in surface_lines], ["ttc-surface:506"])
            self.assertEqual([row["routeID"] for row in subway_lines], ["ttc-subway:1"])
            self.assertEqual([row["routeID"] for row in germany_lines], ["germany:134"])
            self.assertEqual([row["routeID"] for row in single_lines], ["7"])
            self.assertEqual({row["routeID"] for row in toronto_board}, {"ttc-surface:506", "ttc-subway:1"})
            self.assertEqual([row["routeID"] for row in surface_board], ["ttc-surface:506"])
            self.assertEqual([row["routeID"] for row in subway_board], ["ttc-subway:1"])
            self.assertEqual([row["routeID"] for row in germany_board], ["germany:134"])
            self.assertEqual(foreign_lines, [])
            self.assertEqual(foreign_board, [])

            path_database = path

            def http_status(path: str) -> int:
                try:
                    with StaticDeparturesHTTPServer(path_database) as server:
                        server.get(path)
                    return HTTPStatus.OK
                except HTTPError as error:
                    return error.code

            with StaticDeparturesHTTPServer(path_database) as server:
                raw_lines_response = server.get(
                    "/static-departures/lines?cityID=toronto&stopID=100"
                )
                raw_board_response = server.get(
                    "/static-departures/board?cityID=toronto&stopID=100&limit=10"
                    "&from=2026-07-28T07:00:00-04:00&to=2026-07-28T09:00:00-04:00"
                )
                surface_response = server.get(
                    "/static-departures/lines?cityID=toronto&stopID=ttc-surface:100"
                )
                subway_response = server.get(
                    "/static-departures/lines?cityID=toronto&stopID=ttc-subway:100"
                )
                germany_response = server.get(
                    "/static-departures/lines?cityID=germany&stopID=100"
                )
                single_response = server.get(
                    "/static-departures/lines?cityID=single-provider&stopID=single:100"
                )

            self.assertEqual(
                {row["routeID"] for row in raw_lines_response["lines"]},
                {"ttc-surface:506", "ttc-subway:1"},
            )
            self.assertEqual(
                {row["routeID"] for row in raw_board_response["departures"]},
                {"ttc-surface:506", "ttc-subway:1"},
            )
            self.assertEqual([row["routeID"] for row in surface_response["lines"]], ["ttc-surface:506"])
            self.assertEqual([row["routeID"] for row in subway_response["lines"]], ["ttc-subway:1"])
            self.assertEqual([row["routeID"] for row in germany_response["lines"]], ["germany:134"])
            self.assertEqual([row["routeID"] for row in single_response["lines"]], ["7"])
            self.assertEqual(
                http_status("/static-departures/lines?cityID=toronto&stopID=foreign:100"),
                HTTPStatus.NOT_FOUND,
            )
            self.assertEqual(
                http_status("/static-departures/lines?cityID=single-provider&stopID=100"),
                HTTPStatus.NOT_FOUND,
            )

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
