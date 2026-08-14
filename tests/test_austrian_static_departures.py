import sqlite3
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from build_german_departure_index import (
    connect,
    populate_active_services,
    populate_gtfs,
    resolve_canonical_stops,
    update_terminal_stops,
)
from austrian_sources import load_austrian_sources
from import_static_departures_database import configured_austrian_static_city_ids
from static_departures_api import Database


class AustrianStaticDepartureTests(unittest.TestCase):
    def _write_pathway_feed(self, path: Path, rows: str) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("stops.txt", "stop_id,stop_name\na,Stop A\nb,Stop B\n")
            archive.writestr("routes.txt", "route_id,route_short_name,route_long_name\nr,1,Route\n")
            archive.writestr("trips.txt", "route_id,service_id,trip_id,trip_headsign,direction_id\nr,s,t,Destination,0\n")
            archive.writestr("stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt,08:00:00,08:00:00,a,1\nt,08:10:00,08:10:00,b,2\n")
            archive.writestr(
                "pathways.txt",
                "pathway_id,from_stop_id,to_stop_id,pathway_mode,is_bidirectional\n" + rows,
            )

    def test_identical_duplicate_pathways_are_deduplicated_within_feed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            feed = root / "vor.zip"
            self._write_pathway_feed(feed, "p1,a,b,1,1\np1,a,b,1,1\n")
            connection = connect(root / "departures.sqlite")
            try:
                with zipfile.ZipFile(feed) as archive:
                    populate_gtfs(connection, archive, identifier_prefix="vor:", provider_id="vor")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM pathways").fetchone()[0], 1)
            finally:
                connection.close()

    def test_conflicting_duplicate_pathways_fail_with_provider_and_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            feed = root / "vor.zip"
            self._write_pathway_feed(feed, "p1,a,b,1,1\np1,b,a,1,1\n")
            connection = connect(root / "departures.sqlite")
            try:
                with zipfile.ZipFile(feed) as archive:
                    with self.assertRaisesRegex(
                        ValueError,
                        r"provider=vor pathway_id='vor:p1'",
                    ):
                        populate_gtfs(connection, archive, identifier_prefix="vor:", provider_id="vor")
            finally:
                connection.close()

    def test_all_registry_cities_are_configured_for_static_departures(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        registry_city_ids = {
            str(city_id)
            for source in load_austrian_sources(repository_root / "config" / "austrian-sources.json")
            for city_id in source["cities"]
        }
        configured_city_ids = configured_austrian_static_city_ids(
            repository_root / "config" / "cities.json"
        )
        self.assertEqual(configured_city_ids, registry_city_ids)

    def test_vor_ids_platform_parent_and_calendar_exceptions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive_path = root / "vor.zip"
            archive_path.write_bytes(b"")
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("stops.txt", """stop_id,stop_name,parent_station,platform_code,location_type
Pat:49:1349,Wien Hauptbahnhof,,,1
at:49:1349:23,ZG KH Nord,Pat:49:1349,A,0
at:49:1349:24,Wien Hauptbahnhof,Pat:49:1349,B,0
at:49:1349:25,Wien Hauptbahnhof,Pat:49:1349,C,0
at:49:975:0:10,Wien Oper/Karlsplatz,,,0
""")
                archive.writestr("routes.txt", """route_id,route_short_name,route_long_name
tram-1,D,Tram D
bus-1,69A,Bus 69A
""")
                archive.writestr("trips.txt", """route_id,service_id,trip_id,trip_headsign,direction_id
tram-1,weekday,trip-platform,Nußdorf,0
bus-1,weekday,trip-parent,Praterstern,1
bus-1,removed,trip-removed,Oper,0
""")
                archive.writestr("calendar.txt", """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
weekday,1,1,1,1,1,1,1,20260101,20261231
removed,1,1,1,1,1,1,1,20260101,20261231
""")
                archive.writestr("calendar_dates.txt", """service_id,date,exception_type
removed,20260730,2
""")
                archive.writestr("stop_times.txt", """trip_id,arrival_time,departure_time,stop_id,stop_sequence
trip-platform,25:10:00,25:10:00,at:49:1349:23,1
trip-platform,25:20:00,25:20:00,at:49:975:0:10,2
trip-parent,10:00:00,10:00:00,Pat:49:1349,1
trip-removed,11:00:00,11:00:00,at:49:1349:25,1
""")

            database_path = root / "departures.sqlite"
            connection = connect(database_path)
            with zipfile.ZipFile(archive_path) as archive:
                populate_gtfs(connection, archive, identifier_prefix="vor:")
            resolve_canonical_stops(connection)
            populate_active_services(connection, [date(2026, 7, 30)])
            update_terminal_stops(connection)
            connection.executescript("""
                CREATE TABLE city_departure_modes (
                    city_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    timezone TEXT NOT NULL
                );
                INSERT INTO city_departure_modes VALUES ('wien', 'exact-stop-with-parent-fallback', 'Europe/Vienna');
            """)
            connection.executemany(
                "INSERT INTO city_stops VALUES ('wien', ?)",
                [
                    ("at:49:1349:23",),
                    ("at:49:1349:24",),
                    ("at:49:1349:25",),
                    ("Pat:49:1349",),
                    ("at:49:975:0:10",),
                ],
            )
            connection.commit()
            connection.close()

            database = Database(str(database_path), ttl=0)
            try:
                platform = database.board("wien", "at:49:1349:23", 10)
                self.assertEqual([item["line"] for item in platform], ["D"])
                self.assertEqual(platform[0]["scheduledTime"], "25:10:00")
                self.assertEqual(platform[0]["tripID"], "vor:trip-platform")
                self.assertEqual(platform[0]["platform"], "A")

                parent_fallback = database.board("wien", "at:49:1349:24", 10)
                self.assertEqual([item["line"] for item in parent_fallback], ["69A"])
                self.assertEqual(parent_fallback[0]["platform"], None)

                self.assertEqual(database.board("wien", "at:49:975:0:10", 10)[0]["line"], "D")
                self.assertEqual(database.board("wien", "at:49:1349:24", 10)[0]["destination"], "Praterstern")
                self.assertEqual(database.board("wien", "at:49:1349:25", 10), [])
            finally:
                database.close()
