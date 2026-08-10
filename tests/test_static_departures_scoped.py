import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import static_departures_scoped as scoped
from build_german_departure_index import (
    connect,
    populate_active_services,
    populate_gtfs,
    resolve_canonical_stops,
    update_terminal_stops,
)
from static_departures_ownership import (
    delete_provider_data,
    rebuild_city_departure_modes,
    rebuild_city_stops,
    register_city_mode,
    register_city_stops,
)
from import_static_departures_database import main as import_static_database


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCOPED_SCRIPT = REPOSITORY_ROOT / "scripts" / "run_static_departures_scoped.sh"
ACTIVE_DATE = date(2026, 8, 10)


def write_feed(path: Path, provider_prefix: str, version: str) -> None:
    stop = f"{provider_prefix}:stop-{version}"
    terminal = f"{provider_prefix}:terminal-{version}"
    route = f"route-{version}"
    trip = f"trip-{version}"
    service = f"service-{version}"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,parent_station\n"
            f"{stop},Stop {version},50.0,8.0,\n"
            f"{terminal},Terminal {version},50.1,8.1,\n",
        )
        archive.writestr(
            "routes.txt",
            "route_id,route_short_name,route_long_name\n"
            f"{route},{version},Provider {provider_prefix}\n",
        )
        archive.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,trip_headsign,direction_id\n"
            f"{route},{service},{trip},Terminal,0\n",
        )
        archive.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            f"{service},1,1,1,1,1,1,1,20200101,20301231\n",
        )
        archive.writestr(
            "calendar_dates.txt",
            "service_id,date,exception_type\n"
            f"{service},20260811,2\n",
        )
        archive.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            f"{trip},08:00:00,08:00:00,{stop},1\n"
            f"{trip},08:10:00,08:10:00,{terminal},2\n",
        )


def create_database(path: Path, feed_specs: list[tuple[str, str, Path]]) -> None:
    connection = connect(path)
    connection.executescript(
        """
        CREATE TABLE city_aliases (
            alias_city_id TEXT PRIMARY KEY,
            canonical_city_id TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE city_departure_modes (
            city_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            timezone TEXT NOT NULL,
            stop_id_prefix TEXT NOT NULL DEFAULT '',
            identifier_prefix TEXT NOT NULL DEFAULT ''
        ) WITHOUT ROWID;
        """
    )
    for provider_id, prefix, feed_path in feed_specs:
        with zipfile.ZipFile(feed_path) as archive:
            populate_gtfs(
                connection,
                archive,
                identifier_prefix=f"{prefix}:",
                stop_id_prefix=f"{prefix}:",
                provider_id=provider_id,
            )
        register_city_stops(
            connection,
            provider_id,
            (
                ("fixture-city", f"{prefix}:stop-{feed_path.stem.split('-')[-1]}"),
                ("fixture-city", f"{prefix}:terminal-{feed_path.stem.split('-')[-1]}"),
            ),
        )
        register_city_mode(
            connection,
            provider_id,
            "fixture-city",
            "canonical",
            "Europe/Berlin",
        )
    resolve_canonical_stops(connection)
    populate_active_services(connection, [ACTIVE_DATE])
    update_terminal_stops(connection)
    rebuild_city_stops(connection)
    rebuild_city_departure_modes(connection)
    connection.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        (("databaseVersion", "fixture"), ("releaseID", "fixture")),
    )
    connection.commit()
    connection.close()


class StaticDepartureScopeTests(unittest.TestCase):
    def test_provider_scope_resolves_one_canonical_provider(self) -> None:
        label, providers = scoped.resolve_scope(
            REPOSITORY_ROOT,
            provider_id="translink",
        )

        self.assertEqual(label, "provider translink")
        self.assertEqual([provider.provider_id for provider in providers], ["translink"])

    def test_country_scope_resolves_only_static_canadian_providers(self) -> None:
        _label, providers = scoped.resolve_scope(REPOSITORY_ROOT, country="ca")

        self.assertEqual(
            [provider.provider_id for provider in providers],
            ["translink", "ttc-surface", "ttc-subway"],
        )

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown static-departures provider"):
            scoped.resolve_scope(REPOSITORY_ROOT, provider_id="not-a-real-provider")

    def test_us_scope_resolves_the_511_static_source(self) -> None:
        _label, providers = scoped.resolve_scope(REPOSITORY_ROOT, country="US")
        self.assertEqual([provider.provider_id for provider in providers], ["511-bay-area"])

    def test_external_scoped_memberships_use_provider_stop_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "feed.zip"
            write_feed(feed, "native", "current")
            connection = connect(root / "departures.sqlite")
            with zipfile.ZipFile(feed) as archive:
                populate_gtfs(
                    connection,
                    archive,
                    stop_id_prefix="511-bay-area:",
                    provider_id="511-bay-area",
                )
            stop_data = root / "stop-data"
            (stop_data / "stops").mkdir(parents=True)
            (stop_data / "manifest.json").write_text(
                json.dumps(
                    {
                        "cities": [
                            {"id": "fixture-city", "url": "stops/fixture-city.json"}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (stop_data / "stops" / "fixture-city.json").write_text(
                json.dumps([{"id": "native:stop-current"}]),
                encoding="utf-8",
            )
            provider = scoped.StaticProvider(
                "511-bay-area",
                "US",
                "external",
                {
                    "identifierPrefix": "",
                    "staticStopIDPrefix": "511-bay-area:",
                },
            )
            with mock.patch.object(
                scoped,
                "add_external_gtfs",
                return_value={"fixture-city"},
            ):
                scoped.import_external(
                    connection,
                    [provider],
                    stop_data,
                    root,
                    {},
                    15,
                    artifacts={"511-bay-area": feed},
                )

            self.assertEqual(
                connection.execute(
                    """
                    SELECT stop_id
                    FROM provider_city_stops
                    WHERE provider_id=? AND city_id=?
                    """,
                    ("511-bay-area", "fixture-city"),
                ).fetchone(),
                ("511-bay-area:native:stop-current",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT stop_id FROM city_stops WHERE city_id=?",
                    ("fixture-city",),
                ).fetchone(),
                ("native:stop-current",),
            )
            connection.close()

    def test_511_static_assets_replace_only_selected_cities_in_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            config = repository / "config"
            config.mkdir(parents=True)
            cities_path = config / "cities.json"
            cities_path.write_text(
                json.dumps([
                    {
                        "id": "san-francisco",
                        "name": "San Francisco",
                        "country": "US",
                        "packageMode": "external",
                        "latitude": 37.7749,
                        "longitude": -122.4194,
                        "radiusMeters": 42_000,
                        "transitRadar": {
                            "adapter": "bayArea511",
                            "features": ["liveVehicles", "tripUpdates"],
                            "staticBaseURL": "https://api.asoftlabs.app",
                            "boardURL": "https://api.asoftlabs.app/static-departures",
                            "realtimeURL": "https://api.asoftlabs.app/511/realtime/vehicle-positions",
                            "tripUpdatesURL": "https://api.asoftlabs.app/511/realtime/trip-updates",
                            "region": {
                                "minimumLatitude": 37.2,
                                "maximumLatitude": 38.2,
                                "minimumLongitude": -122.8,
                                "maximumLongitude": -121.8,
                            },
                        },
                    }
                ]),
                encoding="utf-8",
            )
            (config / "external-gtfs-sources.json").write_text(
                json.dumps([
                    {
                        "id": "511-bay-area",
                        "scopedURL": "https://api.511.org/transit/datafeeds?operator_id=RG",
                        "cities": "config/cities.json",
                        "timezone": "America/Los_Angeles",
                        "identifierPrefix": "",
                        "namespace": "",
                        "preserveNativeIDs": True,
                        "stopIDMode": "exact",
                        "country": "US",
                    }
                ]),
                encoding="utf-8",
            )
            archive_path = root / "511.zip"
            rewritten = {
                "stops.txt": (
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    "stop-511,Market Street,37.7749,-122.4194\n"
                ),
                "routes.txt": (
                    "route_id,route_short_name,route_long_name,route_type\n"
                    "route-511,F,Market Cable Car,5\n"
                ),
                "trips.txt": (
                    "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                    "route-511,service-511,trip-511,Van Ness,0\n"
                ),
                "calendar.txt": (
                    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "service-511,1,1,1,1,1,1,1,20200101,20301231\n"
                ),
                "stop_times.txt": (
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "trip-511,08:00:00,08:00:00,stop-511,1\n"
                ),
            }
            with zipfile.ZipFile(archive_path, "w") as target:
                for name, content in rewritten.items():
                    target.writestr(name, content)

            release = root / "release-511"
            stop_data = release / "stop-data"
            stop_data.mkdir(parents=True)
            (stop_data / "manifest.json").write_text(
                json.dumps({
                    "version": "old",
                    "cities": [{"id": "existing", "url": "stops/existing.json"}],
                }),
                encoding="utf-8",
            )
            (stop_data / "transit-radar-cities.json").write_text(
                json.dumps({"cities": [{"appCityID": "existing"}]}),
                encoding="utf-8",
            )
            (stop_data / "attributions.json").write_text("[]", encoding="utf-8")

            provider = scoped.StaticProvider(
                "511-bay-area",
                "US",
                "external",
                json.loads((config / "external-gtfs-sources.json").read_text())[0],
            )
            scoped.build_external_static_assets(
                release,
                [provider],
                repository,
                {"511-bay-area": archive_path},
            )

            manifest = json.loads((stop_data / "manifest.json").read_text())
            self.assertEqual(
                {entry["id"] for entry in manifest["cities"]},
                {"existing", "san-francisco"},
            )
            routes = json.loads((stop_data / "routes" / "san-francisco.json").read_text())
            self.assertEqual(routes["route-511"]["type"], "5")
            radar = json.loads((stop_data / "transit-radar-cities.json").read_text())
            self.assertEqual(
                {city["appCityID"] for city in radar["cities"]},
                {"existing", "san-francisco"},
            )
            attributions = json.loads((stop_data / "attributions.json").read_text())
            self.assertEqual(attributions[0]["url"], "https://511.org/open-data")

    def test_dry_run_does_not_create_or_modify_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "data"
            environment_file = Path(temporary) / "empty.env"
            environment_file.write_text("", encoding="utf-8")
            result = subprocess.run(
                ["bash", str(SCOPED_SCRIPT), "--country", "CA", "--dry-run"],
                cwd=REPOSITORY_ROOT,
                env={
                    **os.environ,
                    "REPO": str(REPOSITORY_ROOT),
                    "DATA_ROOT": str(data_root),
                    "STATIC_DEPARTURES_ENV_FILE": str(environment_file),
                },
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Mode: scoped dry-run", result.stdout)
            self.assertIn("Requested scope: country CA", result.stdout)
            self.assertIn("  - translink", result.stdout)
            self.assertIn("Production switch:\n  NO", result.stdout)
            self.assertFalse(data_root.exists())

    def test_provider_replacement_preserves_provider_b_and_replaces_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_a = root / "a-old.zip"
            new_a = root / "a-new.zip"
            feed_b = root / "b-current.zip"
            write_feed(old_a, "a", "old")
            write_feed(new_a, "a", "new")
            write_feed(feed_b, "b", "current")
            database_path = root / "departures.sqlite"
            create_database(
                database_path,
                [
                    ("provider-a", "a", old_a),
                    ("provider-b", "b", feed_b),
                ],
            )

            connection = sqlite3.connect(database_path)
            delete_provider_data(connection, ["provider-a"])
            with zipfile.ZipFile(new_a) as archive:
                populate_gtfs(
                    connection,
                    archive,
                    identifier_prefix="a:",
                    stop_id_prefix="a:",
                    provider_id="provider-a",
                )
            register_city_stops(
                connection,
                "provider-a",
                (("fixture-city", "a:stop-new"), ("fixture-city", "a:terminal-new")),
            )
            register_city_mode(
                connection,
                "provider-a",
                "fixture-city",
                "canonical",
                "Europe/Berlin",
            )
            populate_active_services(connection, [ACTIVE_DATE])
            resolve_canonical_stops(connection)
            update_terminal_stops(connection)
            rebuild_city_stops(connection)
            rebuild_city_departure_modes(connection)

            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM trips WHERE trip_id='a:trip-old'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM stop_times WHERE trip_id='a:trip-old'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM calendar WHERE service_id='a:service-old'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM active_services WHERE service_id='a:service-old'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM calendar_dates WHERE service_id='a:service-old'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM trips WHERE trip_id='b:trip-current'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM trips WHERE trip_id='a:trip-new'"
                ).fetchone()[0],
                1,
            )
            city_stops = {
                row[0]
                for row in connection.execute(
                    "SELECT stop_id FROM city_stops WHERE city_id='fixture-city'"
                )
            }
            self.assertIn("a:stop-new", city_stops)
            self.assertIn("b:stop-current", city_stops)
            self.assertNotIn("a:stop-old", city_stops)
            connection.close()

    def test_full_importer_emits_internal_ownership_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "germany.zip"
            write_feed(feed, "germany", "old")
            stop_data = root / "stop-data"
            (stop_data / "stops").mkdir(parents=True)
            (stop_data / "manifest.json").write_text(
                json.dumps(
                    {
                        "cities": [
                            {
                                "id": "fixture-city",
                                "url": "stops/fixture-city.json",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (stop_data / "stops" / "fixture-city.json").write_text(
                json.dumps(
                    [
                        {"id": "germany:stop-old"},
                        {"id": "germany:terminal-old"},
                    ]
                ),
                encoding="utf-8",
            )
            empty_config = root / "empty.json"
            empty_config.write_text(
                json.dumps(
                    [
                        {
                            "id": "fixture-city",
                            "name": "Fixture City",
                            "aliases": [],
                            "latitude": 50.0,
                            "longitude": 8.0,
                            "radiusMeters": 1000,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            aliases = root / "aliases.json"
            aliases.write_text("{}", encoding="utf-8")
            database_path = root / "departures.sqlite"

            with mock.patch.object(
                sys,
                "argv",
                [
                    "import_static_departures_database.py",
                    "--gtfs-url",
                    str(feed),
                    "--stop-data",
                    str(stop_data),
                    "--next",
                    str(database_path),
                    "--cities",
                    str(empty_config),
                    "--swiss-cities",
                    str(REPOSITORY_ROOT / "config" / "swiss-cities.json"),
                    "--city-id-aliases",
                    str(aliases),
                ],
            ):
                import_static_database()

            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM ownership_metadata WHERE key='schemaVersion'"
                    ).fetchone()[0],
                    "1",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM provider_entities WHERE provider_id='germany'"
                    ).fetchone()[0],
                    6,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM metadata WHERE key='ownershipSchemaVersion'"
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()

    def test_failed_scoped_build_keeps_active_release_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            old_release = data_root / "releases" / "old"
            old_release.mkdir(parents=True)
            (old_release / "stop-data").mkdir()
            old_a = root / "a-old.zip"
            feed_b = root / "b-current.zip"
            write_feed(old_a, "a", "old")
            write_feed(feed_b, "b", "current")
            create_database(
                old_release / "departures.sqlite",
                [("provider-a", "a", old_a), ("provider-b", "b", feed_b)],
            )
            (old_release / "release-metadata.json").write_text(
                json.dumps({"releaseID": "old"}),
                encoding="utf-8",
            )
            (data_root / "current-release").symlink_to("releases/old")
            (data_root / "current").symlink_to("releases/old/stop-data")
            (data_root / "departures-current.sqlite").symlink_to(
                "releases/old/departures.sqlite"
            )

            provider = scoped.StaticProvider(
                "provider-a",
                "CA",
                "external",
                {
                    "id": "provider-a",
                    "cities": "unused",
                    "timezone": "Europe/Berlin",
                    "localPath": str(old_a),
                },
            )
            with mock.patch.object(
                scoped,
                "import_external",
                side_effect=RuntimeError("fixture import failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "fixture import failed"):
                    scoped.scoped_rebuild(
                        REPOSITORY_ROOT,
                        data_root,
                        [provider],
                        {"STATIC_DEPARTURES_DAYS": "15"},
                    )

            self.assertEqual(os.readlink(data_root / "current-release"), "releases/old")
            connection = sqlite3.connect(
                data_root / "current-release" / "departures.sqlite"
            )
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM trips WHERE trip_id='a:trip-old'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()
            self.assertEqual(
                sorted((data_root / "releases").iterdir()),
                [old_release],
            )

    def test_full_service_and_scoped_script_share_static_lock_path(self) -> None:
        service = (
            REPOSITORY_ROOT / "deploy" / "systemd" / "haltewecker-static-departures.service"
        ).read_text(encoding="utf-8")
        script = (
            REPOSITORY_ROOT / "scripts" / "run_static_departures_scoped.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("/run/lock/haltewecker-static-departures.lock", service)
        self.assertIn("/run/lock/haltewecker-static-departures.lock", script)


if __name__ == "__main__":
    unittest.main()
