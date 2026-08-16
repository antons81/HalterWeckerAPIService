import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from bay_area_gateway import (  # noqa: E402
    BAY_AREA_PROVIDER_ID,
    BAY_AREA_STOP_ID_PREFIX,
    BAY_AREA_TRIP_UPDATES_PATH,
    BayAreaTripUpdatesProxy,
    internal_stop_id,
)
from build_german_departure_index import (  # noqa: E402
    connect,
    populate_active_services,
    populate_gtfs,
    resolve_canonical_stops,
    update_terminal_stops,
)
from import_static_departures_database import populate_provider_city_memberships  # noqa: E402
from static_departures_api import Database  # noqa: E402
from static_departures_ownership import (  # noqa: E402
    delete_provider_data,
    rebuild_city_departure_modes,
    rebuild_city_stops,
    register_city_mode,
    register_city_stops,
)


ACTIVE_DATE = date(2026, 8, 10)


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


def _trip_update_feed() -> bytes:
    event = _field(2, 1_775_000_900, 0) + _field(1, 120, 0)
    stop_update = _field(3, 1, 0) + _field(2, event) + _field(4, b"13114")
    trip = _field(1, b"trip-b") + _field(5, b"route-b") + _field(6, b"0")
    trip_update = _field(1, trip) + _field(2, stop_update)
    entity = _field(1, b"entity-b") + _field(3, trip_update)
    header = _field(4, 1_775_000_000, 0)
    return _field(1, header) + _field(2, entity)


def _feed(
    path: Path,
    transfer_rows: str = (
        "13114,13115,route,route-a,,,2,60\n"
        "13114,13115,route,route-b,,,2,60\n"
        "13114,13115,route,route-b,,,2,60\n"
    ),
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,parent_station\n"
            "13100,Parent,37.77,-122.42,\n"
            "13114,Platform,37.77,-122.42,13100\n"
            "13115,Next platform,37.78,-122.41,\n",
        )
        archive.writestr(
            "routes.txt",
            "route_id,route_short_name,route_long_name,route_type\n"
            "route,1,Route,3\n",
        )
        archive.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,trip_headsign,direction_id\n"
            "route,service,trip,Destination,0\n",
        )
        archive.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "service,1,1,1,1,1,1,1,20200101,20301231\n",
        )
        archive.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "trip,08:00:00,08:00:00,13114,1\n"
            "trip,08:10:00,08:10:00,13115,2\n",
        )
        archive.writestr(
            "transfers.txt",
            "from_stop_id,to_stop_id,from_route_id,to_route_id,from_trip_id,to_trip_id,transfer_type,min_transfer_time\n"
            + transfer_rows,
        )
        archive.writestr(
            "pathways.txt",
            "pathway_id,from_stop_id,to_stop_id,pathway_mode,is_bidirectional\n"
            "p1,13114,13115,1,1\n",
        )


class ProviderStopIdentityTests(unittest.TestCase):
    def test_colliding_native_stops_are_separate_and_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "feed.zip"
            _feed(feed)
            database_path = root / "departures.sqlite"
            connection = connect(database_path)
            connection.executescript(
                """
                CREATE TABLE city_departure_modes (
                    city_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    stop_id_prefix TEXT NOT NULL DEFAULT '',
                    identifier_prefix TEXT NOT NULL DEFAULT ''
                ) WITHOUT ROWID;
                """
            )
            with zipfile.ZipFile(feed) as archive:
                populate_gtfs(
                    connection,
                    archive,
                    identifier_prefix="a:",
                    provider_id="provider-a",
                )
            with zipfile.ZipFile(feed) as archive:
                populate_gtfs(
                    connection,
                    archive,
                    stop_id_prefix=BAY_AREA_STOP_ID_PREFIX,
                    provider_id=BAY_AREA_PROVIDER_ID,
                )

            self.assertEqual(
                {
                    row[0]
                    for row in connection.execute(
                        "SELECT stop_id FROM raw_stops WHERE stop_id IN (?, ?)",
                        ("13114", internal_stop_id("13114")),
                    )
                },
                {"13114", "511-bay-area:13114"},
            )
            self.assertEqual(
                connection.execute(
                    "SELECT raw_stop_id FROM stop_times WHERE trip_id='trip'"
                ).fetchone(),
                ("511-bay-area:13114",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT parent_station FROM raw_stops WHERE stop_id=?",
                    ("511-bay-area:13114",),
                ).fetchone()[0],
                "511-bay-area:13100",
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT from_trip_id, to_trip_id
                    FROM transfers
                    WHERE from_stop_id=?
                    """,
                    ("13114",),
                ).fetchone(),
                ("", ""),
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT from_stop_id, to_stop_id, from_trip_id, to_trip_id,
                           from_route_id, to_route_id, transfer_type, min_transfer_time
                    FROM transfers
                    WHERE from_stop_id=?
                    ORDER BY to_route_id
                    """,
                    ("511-bay-area:13114",),
                ).fetchall(),
                [
                    (
                        "511-bay-area:13114",
                        "511-bay-area:13115",
                        "",
                        "",
                        "route",
                        "route-a",
                        2,
                        60,
                    ),
                    (
                        "511-bay-area:13114",
                        "511-bay-area:13115",
                        "",
                        "",
                        "route",
                        "route-b",
                        2,
                        60,
                    ),
                ],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT from_stop_id, to_stop_id FROM pathways WHERE from_stop_id=?",
                    ("511-bay-area:13114",),
                ).fetchone(),
                ("511-bay-area:13114", "511-bay-area:13115"),
            )

            register_city_stops(
                connection,
                "provider-a",
                [("provider-a-city", "13100"), ("provider-a-city", "13114")],
            )
            register_city_stops(
                connection,
                BAY_AREA_PROVIDER_ID,
                [("san-francisco", "13100"), ("san-francisco", "13114")],
            )
            register_city_mode(
                connection,
                "provider-a",
                "provider-a-city",
                "exact-stop-with-parent-fallback",
                "UTC",
            )
            register_city_mode(
                connection,
                BAY_AREA_PROVIDER_ID,
                "san-francisco",
                "exact-stop-with-parent-fallback",
                "America/Los_Angeles",
                BAY_AREA_STOP_ID_PREFIX,
            )
            populate_active_services(connection, [ACTIVE_DATE])
            resolve_canonical_stops(connection)
            update_terminal_stops(connection)
            rebuild_city_stops(connection)
            rebuild_city_departure_modes(connection)
            connection.commit()
            connection.close()

            database = Database(str(database_path), ttl=0)
            provider_a_board = database.board("provider-a-city", "13114", 10)
            bay_area_board = database.board("san-francisco", "13114", 10)
            self.assertTrue(provider_a_board)
            self.assertTrue(bay_area_board)
            self.assertEqual(provider_a_board[0]["stopID"], "13114")
            self.assertEqual(bay_area_board[0]["stopID"], "13114")
            database.close()

            connection = sqlite3.connect(database_path)
            delete_provider_data(connection, [BAY_AREA_PROVIDER_ID])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM raw_stops WHERE stop_id='13114'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM raw_stops WHERE stop_id='511-bay-area:13114'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM transfers WHERE from_stop_id='13114'"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM transfers WHERE from_stop_id='511-bay-area:13114'"
                ).fetchone()[0],
                0,
            )
            connection.close()

    def test_conflicting_duplicate_transfer_semantics_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "feed.zip"
            _feed(
                feed,
                "13114,13115,route,route-a,,,2,60\n"
                "13114,13115,route,route-a,,,2,120\n",
            )
            connection = connect(root / "departures.sqlite")
            with zipfile.ZipFile(feed) as archive:
                with self.assertRaisesRegex(
                    ValueError,
                    "Conflicting duplicate GTFS transfer rows",
                ):
                    populate_gtfs(connection, archive, provider_id="provider-a")
            connection.close()

    def test_package_membership_maps_public_511_id_to_internal_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "feed.zip"
            _feed(feed)
            connection = connect(root / "departures.sqlite")
            with zipfile.ZipFile(feed) as archive:
                populate_gtfs(
                    connection,
                    archive,
                    identifier_prefix="a:",
                    provider_id="provider-a",
                )
            with zipfile.ZipFile(feed) as archive:
                populate_gtfs(
                    connection,
                    archive,
                    stop_id_prefix=BAY_AREA_STOP_ID_PREFIX,
                    provider_id=BAY_AREA_PROVIDER_ID,
                )
            stop_data = root / "stop-data"
            (stop_data / "stops").mkdir(parents=True)
            (stop_data / "manifest.json").write_text(
                json.dumps({"cities": [{"id": "san-francisco", "url": "stops/san-francisco.json"}]}),
                encoding="utf-8",
            )
            (stop_data / "stops" / "san-francisco.json").write_text(
                json.dumps([{"id": "13114"}]),
                encoding="utf-8",
            )
            register_city_mode(
                connection,
                BAY_AREA_PROVIDER_ID,
                "san-francisco",
                "canonical",
                "America/Los_Angeles",
                BAY_AREA_STOP_ID_PREFIX,
            )
            populate_provider_city_memberships(
                connection,
                stop_data,
                {"san-francisco"},
                stop_id_prefix_by_provider={BAY_AREA_PROVIDER_ID: BAY_AREA_STOP_ID_PREFIX},
            )
            self.assertEqual(
                connection.execute(
                    "SELECT stop_id FROM provider_city_stops WHERE provider_id=?",
                    (BAY_AREA_PROVIDER_ID,),
                ).fetchone()[0],
                "13114",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_city_stops WHERE provider_id='provider-a'"
                ).fetchone()[0],
                0,
            )
            connection.close()

    def test_catalog_only_package_stop_is_queryable_without_static_departures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = connect(root / "departures.sqlite")
            stop_data = root / "stop-data"
            (stop_data / "stops").mkdir(parents=True)
            (stop_data / "manifest.json").write_text(
                json.dumps({"cities": [{"id": "kyiv", "url": "stops/kyiv.json"}]}),
                encoding="utf-8",
            )
            catalog_stop_id = "kyiv-catalog:2477"
            (stop_data / "stops" / "kyiv.json").write_text(
                json.dumps([
                    {
                        "id": catalog_stop_id,
                        "name": "Вул. Олени Теліги",
                        "latitude": 50.4776622073,
                        "longitude": 30.451028891,
                        "staticDeparturesAvailable": False,
                        "staticDepartureProviderID": "kyiv",
                    }
                ]),
                encoding="utf-8",
            )
            register_city_mode(
                connection,
                "kyiv",
                "kyiv",
                "canonical",
                "Europe/Kyiv",
                "",
                "kyiv:",
            )

            imported = populate_provider_city_memberships(
                connection,
                stop_data,
                {"kyiv"},
            )
            self.assertEqual(imported, {"kyiv"})
            rebuild_city_stops(connection, {"kyiv"})
            database = Database(str(root / "departures.sqlite"), ttl=0)
            self.assertTrue(database.city_has_stop("kyiv", catalog_stop_id))
            self.assertEqual(database.lines("kyiv", catalog_stop_id), [])
            self.assertEqual(database.board("kyiv", catalog_stop_id, 10), [])
            connection.close()

    def test_indexed_catalog_only_kyiv_package_stop_keeps_city_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = connect(root / "departures.sqlite")
            stop_data = root / "stop-data"
            (stop_data / "stops").mkdir(parents=True)
            (stop_data / "manifest.json").write_text(
                json.dumps({"cities": [{"id": "kyiv", "url": "stops/kyiv.json"}]}),
                encoding="utf-8",
            )
            (stop_data / "stops" / "kyiv.json").write_text(
                json.dumps([{"id": "2_10363", "name": "вул. Північна"}]),
                encoding="utf-8",
            )
            register_city_mode(
                connection,
                "kyiv",
                "kyiv",
                "canonical",
                "Europe/Kyiv",
                "",
                "kyiv:",
            )

            imported = populate_provider_city_memberships(
                connection,
                stop_data,
                {"kyiv"},
                indexed_ownership_lookup=True,
                catalog_only_city_ids={"kyiv"},
            )

            self.assertEqual(imported, {"kyiv"})
            self.assertEqual(
                connection.execute(
                    "SELECT city_id, stop_id FROM city_stops"
                ).fetchall(),
                [("kyiv", "2_10363")],
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM provider_city_stops").fetchone()[0],
                0,
            )
            connection.close()

            ordinary_stop_data = root / "ordinary-stop-data"
            (ordinary_stop_data / "stops").mkdir(parents=True)
            (ordinary_stop_data / "manifest.json").write_text(
                json.dumps({"cities": [{"id": "ordinary-city", "url": "stops/ordinary-city.json"}]}),
                encoding="utf-8",
            )
            (ordinary_stop_data / "stops" / "ordinary-city.json").write_text(
                json.dumps([{"id": "unresolved-stop"}]),
                encoding="utf-8",
            )
            ordinary_connection = connect(root / "ordinary.sqlite")
            with self.assertRaisesRegex(ValueError, "ordinary-city/unresolved-stop"):
                populate_provider_city_memberships(
                    ordinary_connection,
                    ordinary_stop_data,
                    {"ordinary-city"},
                    indexed_ownership_lookup=True,
                )
            ordinary_connection.close()

    def test_indexed_membership_path_matches_legacy_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "feed.zip"
            _feed(feed)
            stop_data = root / "stop-data"
            (stop_data / "stops").mkdir(parents=True)
            (stop_data / "manifest.json").write_text(
                json.dumps({"cities": [{"id": "san-francisco", "url": "stops/san-francisco.json"}]}),
                encoding="utf-8",
            )
            (stop_data / "stops" / "san-francisco.json").write_text(
                json.dumps([{"id": "13114", "sourceStopIDs": ["13115"]}]),
                encoding="utf-8",
            )

            def build(database_name: str, indexed: bool) -> tuple[list[tuple], list[tuple]]:
                connection = connect(root / database_name)
                with zipfile.ZipFile(feed) as archive:
                    populate_gtfs(
                        connection,
                        archive,
                        identifier_prefix="a:",
                        provider_id="provider-a",
                    )
                with zipfile.ZipFile(feed) as archive:
                    populate_gtfs(
                        connection,
                        archive,
                        identifier_prefix="b:",
                        stop_id_prefix="b:",
                        provider_id="provider-b",
                    )
                register_city_mode(
                    connection,
                    "provider-a",
                    "san-francisco",
                    "canonical",
                    "America/Los_Angeles",
                    "a:",
                )
                register_city_mode(
                    connection,
                    "provider-b",
                    "san-francisco",
                    "canonical",
                    "America/Los_Angeles",
                    "b:",
                )
                populate_provider_city_memberships(
                    connection,
                    stop_data,
                    {"san-francisco"},
                    stop_id_prefix_by_provider={"provider-a": "a:", "provider-b": "b:"},
                    indexed_ownership_lookup=indexed,
                )
                snapshot = (
                    list(connection.execute("SELECT city_id, stop_id FROM city_stops ORDER BY city_id, stop_id")),
                    list(connection.execute(
                        "SELECT provider_id, city_id, stop_id FROM provider_city_stops "
                        "ORDER BY provider_id, city_id, stop_id"
                    )),
                )
                connection.close()
                return snapshot

            self.assertEqual(
                build("legacy.sqlite", indexed=False),
                build("indexed.sqlite", indexed=True),
            )

    def test_indexed_membership_temp_tables_are_cleaned_between_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "feed.zip"
            _feed(feed)
            first_stop_data = root / "first-stop-data"
            second_stop_data = root / "second-stop-data"
            for stop_data, city_id, stop_id in (
                (first_stop_data, "first-city", "13114"),
                (second_stop_data, "second-city", "13115"),
            ):
                (stop_data / "stops").mkdir(parents=True)
                (stop_data / "manifest.json").write_text(
                    json.dumps({"cities": [{"id": city_id, "url": f"stops/{city_id}.json"}]}),
                    encoding="utf-8",
                )
                (stop_data / "stops" / f"{city_id}.json").write_text(
                    json.dumps([{"id": stop_id}]),
                    encoding="utf-8",
                )

            connection = connect(root / "departures.sqlite")
            with zipfile.ZipFile(feed) as archive:
                populate_gtfs(
                    connection,
                    archive,
                    identifier_prefix="a:",
                    provider_id="provider-a",
                )
            with zipfile.ZipFile(feed) as archive:
                populate_gtfs(
                    connection,
                    archive,
                    identifier_prefix="b:",
                    stop_id_prefix="b:",
                    provider_id="provider-b",
                )
            for city_id in ("first-city", "second-city"):
                register_city_mode(
                    connection,
                    "provider-a",
                    city_id,
                    "canonical",
                    "America/Los_Angeles",
                    "a:",
                )
                register_city_mode(
                    connection,
                    "provider-b",
                    city_id,
                    "canonical",
                    "America/Los_Angeles",
                    "b:",
                )

            prefixes = {"provider-a": "a:", "provider-b": "b:"}
            self.assertEqual(
                populate_provider_city_memberships(
                    connection,
                    first_stop_data,
                    {"first-city"},
                    stop_id_prefix_by_provider=prefixes,
                    indexed_ownership_lookup=True,
                ),
                {"first-city"},
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM sqlite_temp_master
                    WHERE type='table'
                      AND name IN (
                          'scoped_membership_candidate_stop_ids',
                          'scoped_membership_stop_owners'
                      )
                    """
                ).fetchone()[0],
                0,
            )

            self.assertEqual(
                populate_provider_city_memberships(
                    connection,
                    second_stop_data,
                    {"second-city"},
                    stop_id_prefix_by_provider=prefixes,
                    indexed_ownership_lookup=True,
                ),
                {"second-city"},
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM sqlite_temp_master
                    WHERE type='table'
                      AND name IN (
                          'scoped_membership_candidate_stop_ids',
                          'scoped_membership_stop_owners'
                      )
                    """
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT city_id, stop_id FROM city_stops WHERE city_id LIKE '%-city' ORDER BY city_id, stop_id"
                ).fetchall(),
                [("first-city", "13114"), ("second-city", "13115")],
            )
            connection.close()

    def test_511_realtime_maps_stop_internally_but_keeps_public_native_response(self) -> None:
        gateway = BayAreaTripUpdatesProxy(
            provider_id=BAY_AREA_PROVIDER_ID,
            city_id="san-francisco",
            city_ids={"san-francisco"},
            path=BAY_AREA_TRIP_UPDATES_PATH,
            upstream_url="https://example.test/trip-updates",
            transport=lambda _url: _trip_update_feed(),
            valid_trip_registry=lambda: ({"trip-b"}, {"trip-b": "route-b"}),
            valid_stop_registry=lambda: {"provider-a:13114"},
            stop_id_mapper=internal_stop_id,
        )
        rejected = gateway.handle(
            BAY_AREA_TRIP_UPDATES_PATH,
            {"cityID": ["san-francisco"], "stopID": ["13114"]},
        )
        self.assertEqual(rejected.payload["updates"], [])

        gateway = BayAreaTripUpdatesProxy(
            provider_id=BAY_AREA_PROVIDER_ID,
            city_id="san-francisco",
            city_ids={"san-francisco"},
            path=BAY_AREA_TRIP_UPDATES_PATH,
            upstream_url="https://example.test/trip-updates",
            transport=lambda _url: _trip_update_feed(),
            valid_trip_registry=lambda: ({"trip-b"}, {"trip-b": "route-b"}),
            valid_stop_registry=lambda: {"511-bay-area:13114"},
            stop_id_mapper=internal_stop_id,
        )
        accepted = gateway.handle(
            BAY_AREA_TRIP_UPDATES_PATH,
            {"cityID": ["san-francisco"], "stopID": ["13114"]},
        )
        self.assertEqual(accepted.payload["updates"][0]["stopID"], "13114")
        self.assertEqual(accepted.payload["updates"][0]["tripID"], "trip-b")
        self.assertEqual(accepted.payload["updates"][0]["routeID"], "route-b")


if __name__ == "__main__":
    unittest.main()
