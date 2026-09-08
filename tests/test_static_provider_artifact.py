import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = TESTS_ROOT.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
sys.path.insert(0, str(TESTS_ROOT))

import normalized_provider_artifact as normalized_artifact  # noqa: E402
import static_provider_artifact as static_artifact  # noqa: E402
import import_static_departures_database as static_importer  # noqa: E402
from build_german_departure_index import connect  # noqa: E402
from build_stop_packages import load_gtfs_archive  # noqa: E402
from external_gtfs import load_external_cities  # noqa: E402
from artifact_provenance import artifact_provenance  # noqa: E402
from test_static_departures_normalized import write_feed  # noqa: E402


class StaticProviderArtifactTests(unittest.TestCase):
    def _prepare_inputs(self, root: Path) -> tuple[Path, dict, list[dict], Path]:
        feed = root / "israel.zip"
        write_feed(feed)
        cities_path = root / "cities.json"
        cities_path.write_text(
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
        source = next(
            item
            for item in json.loads(
                (REPOSITORY_ROOT / "config" / "external-gtfs-sources.json").read_text(
                    encoding="utf-8"
                )
            )
            if item["id"] == "israel-mot"
        )
        source["cities"] = str(cities_path)
        cities = load_external_cities(source, REPOSITORY_ROOT)
        stop_data = root / "stop-data"
        (stop_data / "stops").mkdir(parents=True)
        (stop_data / "manifest.json").write_text(
            json.dumps(
                {
                    "releaseID": "fixture",
                    "version": "1",
                    "cities": [
                        {"id": "fixture-israel", "url": "stops/fixture-israel.json"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (stop_data / "stops/fixture-israel.json").write_text(
            json.dumps([{"id": "S1", "name": "Station"}]),
            encoding="utf-8",
        )
        return feed, source, cities, stop_data

    def _build_normalized(
        self,
        root: Path,
        feed: Path,
    ):
        environment = {
            normalized_artifact.CACHE_ROOT_ENV: str(root / "normalized-providers"),
        }
        archive = load_gtfs_archive(str(feed))
        raw_sha, _size = artifact_provenance(feed)
        try:
            return normalized_artifact.load_or_build(
                archive=archive,
                repository_root=REPOSITORY_ROOT,
                provider_id="israel-mot",
                raw_artifact_sha256=raw_sha,
                gtfs_cache_root=None,
                environ=environment,
            )
        finally:
            archive.close()

    def _build_artifacts(
        self,
        root: Path,
        feed: Path,
        source: dict,
        cities: list[dict],
        stop_data: Path,
        dates: list[date],
    ) -> static_artifact.StaticProviderArtifacts:
        context, normalized_use = self._build_normalized(root, feed)
        try:
            return static_artifact.load_or_build_static_provider_artifacts(
                normalized_context=context,
                normalized_artifact=normalized_use,
                repository_root=REPOSITORY_ROOT,
                provider_id="israel-mot",
                source=source,
                cities=cities,
                stop_data=stop_data,
                dates=dates,
                environ={
                    static_artifact.ARTIFACT_ROOT_ENV: str(
                        root / "static-provider-artifacts"
                    ),
                },
            )
        finally:
            context.close()

    @staticmethod
    def _rows(connection: sqlite3.Connection, table: str):
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        return sorted(
            tuple(row)
            for row in connection.execute(
                f"SELECT {', '.join(columns)} FROM {table}"
            )
        )

    @staticmethod
    def _create_city_modes(connection: sqlite3.Connection) -> None:
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

    def _legacy_database(
        self,
        root: Path,
        feed: Path,
        sources_path: Path,
        stop_data: Path,
        environ: dict[str, str] | None = None,
    ) -> Path:
        database = root / "legacy.sqlite"
        connection = connect(database)
        try:
            self._create_city_modes(connection)
            with redirect_stdout(StringIO()):
                static_importer.add_external_gtfs(
                    connection,
                    stop_data,
                    {"israel-mot": str(feed)},
                    repository_root=REPOSITORY_ROOT,
                    sources_path=sources_path,
                    dates=[date(2026, 1, 5)],
                    environ=environ or {},
                )
            connection.commit()
        finally:
            connection.close()
        return database

    def _candidate_database(
        self,
        root: Path,
        artifacts: static_artifact.StaticProviderArtifacts,
        stop_data: Path,
    ) -> Path:
        database = root / "candidate.sqlite"
        connection = connect(database)
        try:
            self._create_city_modes(connection)
            connection.executescript(
                """
                CREATE TABLE transfers (
                    from_stop_id TEXT NOT NULL,
                    to_stop_id TEXT NOT NULL,
                    from_trip_id TEXT NOT NULL DEFAULT '',
                    to_trip_id TEXT NOT NULL DEFAULT '',
                    from_route_id TEXT NOT NULL DEFAULT '',
                    to_route_id TEXT NOT NULL DEFAULT '',
                    transfer_type INTEGER NOT NULL,
                    min_transfer_time INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (
                        from_stop_id, to_stop_id, from_trip_id, to_trip_id,
                        from_route_id, to_route_id
                    )
                ) WITHOUT ROWID;
                CREATE INDEX transfers_by_stop ON transfers(from_stop_id, to_stop_id);
                CREATE TABLE pathways (
                    pathway_id TEXT PRIMARY KEY,
                    from_stop_id TEXT NOT NULL,
                    to_stop_id TEXT NOT NULL,
                    pathway_mode TEXT NOT NULL,
                    is_bidirectional INTEGER NOT NULL,
                    length TEXT NOT NULL,
                    traversal_time INTEGER NOT NULL,
                    stair_count INTEGER NOT NULL,
                    max_slope TEXT NOT NULL,
                    min_width TEXT NOT NULL,
                    signposted_as TEXT NOT NULL,
                    reversed_signposted_as TEXT NOT NULL
                ) WITHOUT ROWID;
                """
            )
            connection.execute(
                "ATTACH DATABASE ? AS israel_structural",
                (str(artifacts.structural.database_path),),
            )
            connection.execute(
                "ATTACH DATABASE ? AS israel_temporal",
                (str(artifacts.temporal.database_path),),
            )
            for table in static_artifact.STRUCTURAL_TABLES:
                if table == "ownership_metadata":
                    continue
                columns = [
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                ]
                order = " ORDER BY rowid" if table == "stop_times" else ""
                connection.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"SELECT {', '.join(columns)} FROM israel_structural.{table}{order}"
                )
            connection.execute(
                "INSERT INTO active_services SELECT * FROM israel_temporal.active_services"
            )
            stop_manifest = json.loads(
                (stop_data / "manifest.json").read_text(encoding="utf-8")
            )
            for city in stop_manifest["cities"]:
                package = json.loads(
                    (stop_data / str(city["url"])).read_text(encoding="utf-8")
                )
                connection.executemany(
                    "INSERT INTO city_stops(city_id, stop_id) VALUES (?, ?)",
                    (
                        (str(city["id"]), str(stop["id"]))
                        for stop in package
                        if isinstance(stop, dict) and stop.get("id")
                    ),
                )
            connection.execute(
                """
                INSERT INTO city_departure_modes(
                    city_id, mode, timezone, stop_id_prefix, identifier_prefix
                )
                SELECT city_id, mode, timezone, stop_id_prefix, identifier_prefix
                FROM provider_city_modes
                """
            )
            connection.commit()
        finally:
            connection.close()
        return database

    def test_legacy_and_shard_candidate_semantics_are_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed, source, cities, stop_data = self._prepare_inputs(root)
            sources_path = root / "sources.json"
            sources_path.write_text(json.dumps([source]), encoding="utf-8")
            artifacts = self._build_artifacts(
                root, feed, source, cities, stop_data, [date(2026, 1, 5)]
            )
            legacy_path = self._legacy_database(root, feed, sources_path, stop_data)
            candidate_path = self._candidate_database(root, artifacts, stop_data)
            legacy = sqlite3.connect(legacy_path)
            candidate = sqlite3.connect(candidate_path)
            try:
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
                for table in tables:
                    self.assertEqual(self._rows(legacy, table), self._rows(candidate, table), table)
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
                    legacy.execute(departure_query).fetchall(),
                    candidate.execute(departure_query).fetchall(),
                )
                self.assertEqual(
                    candidate.execute(
                        "SELECT terminal_stop_id FROM trips WHERE trip_id='israel:T1'"
                    ).fetchone(),
                    ("israel:S3",),
                )
            finally:
                legacy.close()
                candidate.close()

    def test_importer_experimental_hook_preserves_main_database_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed, source, cities, stop_data = self._prepare_inputs(root)
            sources_path = root / "sources.json"
            sources_path.write_text(json.dumps([source]), encoding="utf-8")
            self._build_artifacts(
                root, feed, source, cities, stop_data, [date(2026, 1, 5)]
            )
            legacy_path = self._legacy_database(root, feed, sources_path, stop_data)
            hooked_path = root / "hooked.sqlite"
            connection = connect(hooked_path)
            try:
                self._create_city_modes(connection)
                with redirect_stdout(StringIO()):
                    static_importer.add_external_gtfs(
                        connection,
                        stop_data,
                        {"israel-mot": str(feed)},
                        repository_root=REPOSITORY_ROOT,
                        sources_path=sources_path,
                        dates=[date(2026, 1, 5)],
                        environ={
                            normalized_artifact.STATIC_DEPARTURES_FEATURE_GATE: "1",
                            normalized_artifact.CACHE_ROOT_ENV: str(
                                root / "normalized-providers"
                            ),
                            static_artifact.EXPERIMENTAL_FEATURE_GATE: "1",
                            static_artifact.ARTIFACT_ROOT_ENV: str(
                                root / "static-provider-artifacts"
                            ),
                        },
                    )
                connection.commit()
            finally:
                connection.close()
            legacy = sqlite3.connect(legacy_path)
            hooked = sqlite3.connect(hooked_path)
            try:
                for table in (
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
                ):
                    self.assertEqual(self._rows(legacy, table), self._rows(hooked, table), table)
            finally:
                legacy.close()
                hooked.close()

    def test_structural_temporal_hit_and_structural_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed, source, cities, stop_data = self._prepare_inputs(root)
            first = self._build_artifacts(
                root,
                feed,
                source,
                cities,
                stop_data,
                [date(2026, 1, 5)],
            )
            second = self._build_artifacts(
                root,
                feed,
                source,
                cities,
                stop_data,
                [date(2026, 1, 5)],
            )
            self.assertEqual(first.structural.artifact_key, second.structural.artifact_key)
            self.assertEqual(first.temporal.artifact_key, second.temporal.artifact_key)
            self.assertEqual(second.structural.status, "HIT")
            self.assertEqual(second.temporal.status, "HIT")

            structural = sqlite3.connect(first.structural.database_path)
            try:
                self.assertEqual(
                    {
                        row[0]
                        for row in structural.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    },
                    set(static_artifact.STRUCTURAL_TABLES),
                )
                self.assertEqual(
                    structural.execute(
                        "SELECT terminal_stop_id FROM trips WHERE trip_id='israel:T1'"
                    ).fetchone(),
                    ("israel:S3",),
                )
                self.assertEqual(
                    structural.execute("SELECT COUNT(*) FROM stop_times").fetchone()[0],
                    4,
                )
                self.assertEqual(
                    structural.execute(
                        "SELECT COUNT(*) FROM provider_city_stops"
                    ).fetchone()[0],
                    2,
                )
            finally:
                structural.close()

            temporal = sqlite3.connect(first.temporal.database_path)
            try:
                self.assertEqual(
                    self._rows(temporal, "active_services"),
                    [("israel:S1", "20260105")],
                )
            finally:
                temporal.close()

    def test_rolling_window_rebuilds_only_temporal_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed, source, cities, stop_data = self._prepare_inputs(root)
            first = self._build_artifacts(
                root, feed, source, cities, stop_data, [date(2026, 1, 5)]
            )
            second = self._build_artifacts(
                root, feed, source, cities, stop_data, [date(2026, 1, 6)]
            )
            self.assertEqual(
                first.structural.artifact_key,
                second.structural.artifact_key,
            )
            self.assertEqual(second.structural.status, "HIT")
            self.assertNotEqual(first.temporal.artifact_key, second.temporal.artifact_key)
            self.assertEqual(second.temporal.status, "MISS")

    def test_structural_inputs_invalidate_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed, source, cities, stop_data = self._prepare_inputs(root)
            first = self._build_artifacts(
                root, feed, source, cities, stop_data, [date(2026, 1, 5)]
            )
            changed_stop_data = root / "changed-stop-data"
            shutil.copytree(stop_data, changed_stop_data)
            (changed_stop_data / "stops/fixture-israel.json").write_text(
                json.dumps([{"id": "S1", "name": "Changed Station"}]),
                encoding="utf-8",
            )
            changed = self._build_artifacts(
                root,
                feed,
                source,
                cities,
                changed_stop_data,
                [date(2026, 1, 5)],
            )
            self.assertNotEqual(first.structural.artifact_key, changed.structural.artifact_key)
            self.assertEqual(changed.structural.status, "MISS")

            changed_source = dict(source)
            changed_source["staticDepartureMode"] = "exact-stop-with-parent-fallback"
            changed_config = self._build_artifacts(
                root,
                feed,
                changed_source,
                cities,
                stop_data,
                [date(2026, 1, 5)],
            )
            self.assertNotEqual(
                first.structural.artifact_key,
                changed_config.structural.artifact_key,
            )

            changed_feed = root / "changed-calendar.zip"
            with zipfile.ZipFile(feed) as original, zipfile.ZipFile(changed_feed, "w") as rebuilt:
                for name in original.namelist():
                    payload = original.read(name)
                    if name == "calendar.txt":
                        payload = payload.replace(b",1,1,1,1,1,1,1,", b",0,1,1,1,1,1,1,")
                    rebuilt.writestr(name, payload)
            changed_normalized = self._build_artifacts(
                root,
                changed_feed,
                source,
                cities,
                stop_data,
                [date(2026, 1, 5)],
            )
            self.assertNotEqual(
                first.structural.artifact_key,
                changed_normalized.structural.artifact_key,
            )

    def test_corrupt_manifest_and_temporal_database_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed, source, cities, stop_data = self._prepare_inputs(root)
            artifacts = self._build_artifacts(
                root, feed, source, cities, stop_data, [date(2026, 1, 5)]
            )
            manifest_path = artifacts.structural.artifact_directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "retired"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(static_artifact.StaticProviderArtifactError):
                self._build_artifacts(
                    root, feed, source, cities, stop_data, [date(2026, 1, 5)]
                )

            manifest["status"] = "complete"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            artifacts = self._build_artifacts(
                root, feed, source, cities, stop_data, [date(2026, 1, 5)]
            )
            artifacts.temporal.database_path.write_bytes(b"not sqlite")
            with self.assertRaises(static_artifact.StaticProviderArtifactError):
                self._build_artifacts(
                    root, feed, source, cities, stop_data, [date(2026, 1, 5)]
                )

    def test_attach_prototype_proves_current_main_schema_needs_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed, source, cities, stop_data = self._prepare_inputs(root)
            artifacts = self._build_artifacts(
                root, feed, source, cities, stop_data, [date(2026, 1, 5)]
            )
            main = connect(root / "main.sqlite")
            try:
                main.execute(
                    "ATTACH DATABASE ? AS israel_structural",
                    (str(artifacts.structural.database_path),),
                )
                main.execute(
                    "ATTACH DATABASE ? AS israel_temporal",
                    (str(artifacts.temporal.database_path),),
                )
                self.assertGreater(
                    main.execute(
                        "SELECT COUNT(*) FROM israel_structural.stop_times"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    main.execute("SELECT COUNT(*) FROM stop_times").fetchone()[0],
                    0,
                )
                with self.assertRaises(sqlite3.OperationalError):
                    main.execute(
                        "CREATE VIEW provider_stop_times AS "
                        "SELECT * FROM israel_structural.stop_times"
                    )
            finally:
                main.close()


if __name__ == "__main__":
    unittest.main()
