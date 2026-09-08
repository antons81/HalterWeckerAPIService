import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import import_static_departures_database as static_importer  # noqa: E402
import normalized_provider_artifact as artifact  # noqa: E402
from build_german_departure_index import connect  # noqa: E402
from build_stop_packages import load_gtfs_archive  # noqa: E402
from artifact_provenance import artifact_provenance  # noqa: E402


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def write_feed(path: Path) -> None:
    files = {
        "agency.txt": "agency_id,agency_name\nA1,Israel Transit\n",
        "stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type,stop_desc,platform_code\n"
            "S1,Station,31.0,34.0,,1,Main station,\n"
            "S2,Platform,31.0,34.0,S1,0,Platform,2\n"
            "S3,Terminal,31.1,34.1,,0,,\n"
        ),
        "routes.txt": "route_id,route_short_name,route_long_name,route_type,agency_id\nR1,1,Route 1,3,A1\n",
        "trips.txt": "route_id,service_id,trip_id,trip_headsign,direction_id\nR1,S1,T1,Terminal,0\n",
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,08:00:00,08:00:00,S1,1\n"
            "T1,08:00:00,08:00:00,S1,1\n"
            "T1,08:10:00,08:10:00,S2,2\n"
            "T1,08:11:00,08:11:00,S3,2\n"
        ),
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "S1,1,1,1,1,1,1,1,20200101,20301231\n"
        ),
        "calendar_dates.txt": "service_id,date,exception_type\nS1,20260105,1\n",
        "transfers.txt": (
            "from_stop_id,to_stop_id,from_trip_id,to_trip_id,from_route_id,to_route_id,transfer_type,min_transfer_time\n"
            "S1,S2,,, , ,2,60\n"
        ).replace(", ,", ",,"),
        "pathways.txt": (
            "pathway_id,from_stop_id,to_stop_id,pathway_mode,is_bidirectional,length,traversal_time,stair_count,max_slope,min_width,signposted_as,reversed_signposted_as\n"
            "P1,S1,S2,1,1,10,30,0,,,Platform,\n"
        ),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, (filename, contents) in enumerate(files.items()):
            info = zipfile.ZipInfo(filename)
            info.date_time = (2020, 1, 1, 0, index, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, contents)


class StaticDeparturesNormalizedTests(unittest.TestCase):
    def _prepare_inputs(self, root: Path) -> tuple[Path, Path, Path]:
        feed = root / "israel.zip"
        write_feed(feed)

        cities = root / "cities.json"
        cities.write_text(
            json.dumps(
                [
                    {
                        "id": "fixture-israel",
                        "name": "Fixture Israel",
                        "country": "IL",
                        "timezone": "Asia/Jerusalem",
                        "latitude": 31.0,
                        "longitude": 34.0,
                        "radiusMeters": 10_000,
                        "packageMode": "external",
                        "externalGTFSProvider": "israel-mot",
                    }
                ]
            ),
            encoding="utf-8",
        )
        sources = root / "sources.json"
        source = next(
            item
            for item in json.loads(
                (REPOSITORY_ROOT / "config" / "external-gtfs-sources.json").read_text(
                    encoding="utf-8"
                )
            )
            if item["id"] == "israel-mot"
        )
        source["cities"] = str(cities)
        sources.write_text(json.dumps([source]), encoding="utf-8")

        stop_data = root / "stop-data"
        (stop_data / "stops").mkdir(parents=True)
        (stop_data / "manifest.json").write_text(
            json.dumps(
                {
                    "releaseID": "fixture",
                    "version": "1",
                    "cities": [
                        {
                            "id": "fixture-israel",
                            "url": "stops/fixture-israel.json",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (stop_data / "stops/fixture-israel.json").write_text(
            json.dumps([{"id": "S1", "name": "Station"}]),
            encoding="utf-8",
        )
        return feed, sources, stop_data

    def _build(self, root: Path, feed: Path, sources: Path, stop_data: Path, persistent: bool):
        database = root / ("persistent.sqlite" if persistent else "legacy.sqlite")
        environment = {
            artifact.STATIC_DEPARTURES_FEATURE_GATE: "1" if persistent else "0",
            artifact.CACHE_ROOT_ENV: str(root / "normalized-providers"),
        }
        if persistent:
            archive = load_gtfs_archive(str(feed))
            raw_sha, _raw_size = artifact_provenance(feed)
            context, _usage = artifact.load_or_build(
                archive=archive,
                repository_root=REPOSITORY_ROOT,
                provider_id="israel-mot",
                raw_artifact_sha256=raw_sha,
                gtfs_cache_root=None,
                environ=environment,
            )
            context.close()
            archive.close()
        connection = connect(database)
        try:
            connection.execute(
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
            with redirect_stdout(StringIO()):
                static_importer.add_external_gtfs(
                    connection,
                    stop_data,
                    {"israel-mot": str(feed)},
                    repository_root=REPOSITORY_ROOT,
                    sources_path=sources,
                    dates=[date(2026, 1, 5)],
                    environ=environment,
                )
            connection.commit()
        finally:
            connection.close()
        return database

    def _table_rows(self, connection: sqlite3.Connection, table: str):
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        return sorted(
            tuple(row)
            for row in connection.execute(
                f"SELECT {', '.join(columns)} FROM {table}"
            )
        )

    def test_old_and_normalized_imports_are_semantically_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed, sources, stop_data = self._prepare_inputs(root)
            old_path = self._build(root, feed, sources, stop_data, False)
            new_path = self._build(root, feed, sources, stop_data, True)
            tables = (
                "agencies",
                "raw_stops",
                "routes",
                "trips",
                "calendar",
                "calendar_dates",
                "active_services",
                "stop_times",
                "transfers",
                "pathways",
                "provider_entities",
                "provider_city_stops",
                "provider_city_modes",
                "city_stops",
                "city_departure_modes",
            )
            old = sqlite3.connect(old_path)
            new = sqlite3.connect(new_path)
            try:
                for table in tables:
                    self.assertEqual(
                        self._table_rows(old, table),
                        self._table_rows(new, table),
                        table,
                    )
                self.assertEqual(
                    old.execute(
                        "SELECT terminal_stop_id FROM trips WHERE trip_id='israel:T1'"
                    ).fetchone(),
                    ("israel:S3",),
                )
                departure_query = """
                    SELECT city_stops.city_id, raw_stops.canonical_stop_id,
                           active_services.service_date, stop_times.departure_time,
                           trips.trip_id, trips.route_id, routes.short_name,
                           trips.headsign, trips.direction_id
                    FROM stop_times
                    JOIN trips ON trips.trip_id=stop_times.trip_id
                    JOIN active_services ON active_services.service_id=trips.service_id
                    JOIN raw_stops ON raw_stops.stop_id=stop_times.raw_stop_id
                    JOIN city_stops ON city_stops.stop_id=raw_stops.canonical_stop_id
                    JOIN routes ON routes.route_id=trips.route_id
                    ORDER BY 1,2,3,stop_times.departure_seconds,5,stop_times.stop_sequence
                """
                self.assertEqual(
                    old.execute(departure_query).fetchall(),
                    new.execute(departure_query).fetchall(),
                )
            finally:
                old.close()
                new.close()

    def test_enabled_path_fails_closed_without_artifact_and_does_not_open_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed, sources, stop_data = self._prepare_inputs(root)
            connection = connect(root / "missing.sqlite")
            try:
                with mock.patch.object(
                    static_importer,
                    "load_gtfs_archive",
                    side_effect=AssertionError("ZIP fallback must not be used"),
                ):
                    with self.assertRaises(artifact.NormalizedArtifactError):
                        static_importer.add_external_gtfs(
                            connection,
                            stop_data,
                            {"israel-mot": str(feed)},
                            repository_root=REPOSITORY_ROOT,
                            sources_path=sources,
                            dates=[date(2026, 1, 5)],
                            environ={
                                artifact.STATIC_DEPARTURES_FEATURE_GATE: "1",
                                artifact.CACHE_ROOT_ENV: str(root / "normalized-providers"),
                            },
                        )
            finally:
                connection.close()

    def test_warm_hit_does_not_open_gtfs_zip_in_static_importer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed, sources, stop_data = self._prepare_inputs(root)
            environment = {
                artifact.STATIC_DEPARTURES_FEATURE_GATE: "1",
                artifact.CACHE_ROOT_ENV: str(root / "normalized-providers"),
            }
            archive = load_gtfs_archive(str(feed))
            raw_sha, _raw_size = artifact_provenance(feed)
            context, _usage = artifact.load_or_build(
                archive=archive,
                repository_root=REPOSITORY_ROOT,
                provider_id="israel-mot",
                raw_artifact_sha256=raw_sha,
                gtfs_cache_root=None,
                environ=environment,
            )
            context.close()
            archive.close()

            connection = connect(root / "warm.sqlite")
            connection.execute(
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
            try:
                with mock.patch.object(
                    static_importer,
                    "load_gtfs_archive",
                    side_effect=AssertionError("warm HIT must not open GTFS ZIP"),
                ) as loader:
                    static_importer.add_external_gtfs(
                        connection,
                        stop_data,
                        {"israel-mot": str(feed)},
                        repository_root=REPOSITORY_ROOT,
                        sources_path=sources,
                        dates=[date(2026, 1, 5)],
                        environ=environment,
                    )
                    loader.assert_not_called()
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
