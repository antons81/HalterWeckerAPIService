"""Coverage gates for every supported city, including non-pilot providers.

Created by Anton on 2026-10-10.
"""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.static_departures_coverage import CityCoverageError, scheduled_stop, package_generation, require_preserved_schedules, validate_city_coverage


class CityCoverageTests(unittest.TestCase):
    cities = ("wien", "helsinki", "bochum", "wuppertal", "small-non-pilot-city", "israel", "toronto", "chicago", "oslo", "stockholm", "vasteras")
    pilots = {"israel": "israel-mot", "toronto": "ttc-surface", "chicago": "cta-chicago", "oslo": "norway", "stockholm": "sweden", "vasteras": "sweden"}

    def test_stop_selection_excludes_services_beyond_http_window(self):
        self.mutate_database("UPDATE active_services SET service_date='20261101'")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(scheduled_stop(connection, "wien", ("",), today=date(2026, 10, 10), through=date(2026, 10, 24)))

    def test_prior_service_day_overflow_is_not_mistaken_for_empty_schedule(self):
        self.mutate_database("UPDATE active_services SET service_date='20261008'")
        self.mutate_database("UPDATE stop_times SET departure_seconds=180000")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(scheduled_stop(connection, "wien", ("",), today=date(2026, 10, 10), through=date(2026, 10, 10)), "wien-stop")

    def test_last_service_day_overflow_outside_window_is_not_expected(self):
        self.mutate_database("UPDATE active_services SET service_date='20261010'")
        self.mutate_database("UPDATE stop_times SET departure_seconds=180000")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(scheduled_stop(connection, "wien", ("",), today=date(2026, 10, 10), through=date(2026, 10, 10)))

    def test_spring_dst_service_origin_before_midnight_respects_window(self):
        self.mutate_database("UPDATE active_services SET service_date='20260329'")
        self.mutate_database("UPDATE stop_times SET departure_seconds=1800")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(scheduled_stop(connection, "wien", ("",), today=date(2026, 3, 29), through=date(2026, 3, 29), timezone_name="Europe/Vienna"))
            self.assertEqual(scheduled_stop(connection, "wien", ("",), today=date(2026, 3, 28), through=date(2026, 3, 28), timezone_name="Europe/Vienna"), "wien-stop")

    def test_stop_selection_is_scoped_to_requested_provider(self):
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(scheduled_stop(connection, "wien", ("",), catalog="provider_city_stops", today=date(2026, 10, 10), provider_id="unrelated"))
            self.assertEqual(scheduled_stop(connection, "wien", ("",), catalog="provider_city_stops", today=date(2026, 10, 10), provider_id="non-pilot-wien"), "wien-stop")

    def test_provider_schedule_probe_fails_closed_without_ownership_metadata(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TABLE provider_entities")
            with self.assertRaisesRegex(CityCoverageError, "ownership metadata is missing"):
                scheduled_stop(
                    connection,
                    "wien",
                    ("",),
                    catalog="provider_city_stops",
                    today=date(2026, 10, 10),
                    provider_id="non-pilot-wien",
                )

    def test_vasteras_ignores_german_canonical_collision_and_selects_owned_swedish_stop(self):
        report = self.validate()
        check = next(row for row in report["checks"] if row["cityID"] == "vasteras")

        self.assertTrue(check["hasSchedule"])
        self.assertEqual(check["stopID"], "3049")

    def test_vasteras_does_not_pass_from_german_schedule_when_swedish_has_none(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM stop_times WHERE raw_stop_id='se:3049'")
            connection.commit()
        self.pin_database()

        report = self.validate()
        check = next(row for row in report["checks"] if row["cityID"] == "vasteras")

        self.assertFalse(check["hasSchedule"])
        self.assertEqual(check["stopID"], "vasteras-stop")

    def test_vasteras_rejects_swedish_stop_with_foreign_trip_owner(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "DELETE FROM provider_entities "
                "WHERE entity_type='trips' AND provider_id='sweden' "
                "AND key_1='sweden-3049-trip'"
            )
            connection.execute(
                "INSERT INTO provider_entities(entity_type,provider_id,key_1) "
                "VALUES ('trips','germany','sweden-3049-trip')"
            )
            connection.commit()
        self.pin_database()

        report = self.validate()
        check = next(row for row in report["checks"] if row["cityID"] == "vasteras")

        self.assertFalse(check["hasSchedule"])

    def test_fixed_stop_expectation_does_not_use_other_city_departures(self):
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(scheduled_stop(connection, "wien", ("",), today=date(2026, 10, 10), stop_id="not-served"))
            self.assertEqual(scheduled_stop(connection, "wien", ("",), today=date(2026, 10, 10), stop_id="wien-stop"), "wien-stop")

    def test_package_dates_respect_local_day_and_compact_format(self):
        self.assertEqual(package_generation({"generatedAt": "2026-10-09T23:57:51Z", "timezone": "Europe/Dublin"}), "2026-10-10")
        self.assertEqual(package_generation({"generatedAt": "20261010"}), "2026-10-10")
        self.assertEqual(package_generation({"generatedAt": "invalid"}), "")

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.package_cities = set()
        self.release = self.root / "candidate"
        self.stop_root = self.release / "stop-data"
        (self.stop_root / "stops").mkdir(parents=True)
        self.release_manifest = {"releaseID": "candidate", "stopData": {"path": "stop-data"}, "providers": {}}
        self.stop_manifest = {"releaseID": "candidate", "version": "2026-10-10", "cities": []}
        for city in self.cities:
            (self.stop_root / "stops" / f"{city}.json").write_text(json.dumps([{"id": city + "-stop"}]))
            self.stop_manifest["cities"].append({"id": city, "url": f"stops/{city}.json", "stopCount": 1})
        (self.stop_root / "manifest.json").write_text(json.dumps(self.stop_manifest))
        self.database = self.release / "departures.sqlite"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript("""
                CREATE TABLE metadata(key TEXT, value TEXT);
                CREATE TABLE city_stops(city_id TEXT, stop_id TEXT);
                CREATE TABLE provider_city_modes(provider_id TEXT, city_id TEXT, timezone TEXT, stop_id_prefix TEXT, mode TEXT DEFAULT 'canonical');
                CREATE TABLE city_departure_modes(city_id TEXT, mode TEXT);
                CREATE TABLE provider_city_stops(provider_id TEXT, city_id TEXT, stop_id TEXT);
                CREATE TABLE provider_entities(entity_type TEXT, provider_id TEXT, key_1 TEXT, key_2 TEXT DEFAULT '', key_3 TEXT DEFAULT '');
                CREATE TABLE raw_stops(stop_id TEXT, canonical_stop_id TEXT);
                CREATE TABLE stop_times(raw_stop_id TEXT, trip_id TEXT, departure_seconds INTEGER DEFAULT 28800);
                CREATE TABLE trips(trip_id TEXT, service_id TEXT);
                CREATE TABLE active_services(service_id TEXT, service_date TEXT);
            """)
            connection.executemany("INSERT INTO metadata VALUES (?,?)", {
                "releaseID": "candidate", "stopDataReleaseID": "candidate",
                "stopDataManifestVersion": "2026-10-10", "databaseVersion": "fixture",
                "validFrom": "2026-10-10", "validThrough": "2026-10-24",
            }.items())
            for city in self.cities:
                stop = city + "-stop"
                provider = self.pilots.get(city, "non-pilot-" + city)
                connection.execute("INSERT INTO city_stops VALUES (?,?)", (city, stop))
                connection.execute("INSERT INTO provider_city_stops VALUES (?,?,?)", (provider, city, stop))
                prefix = "se:" if city == "vasteras" else ""
                connection.execute("INSERT INTO provider_city_modes(provider_id,city_id,timezone,stop_id_prefix) VALUES (?,?,?,?)", (provider, city, "UTC", prefix))
                connection.execute("INSERT INTO raw_stops VALUES (?,?)", (stop, stop))
                connection.execute("INSERT INTO trips VALUES (?,?)", (city + "-trip", "service"))
                connection.execute("INSERT INTO stop_times(raw_stop_id,trip_id) VALUES (?,?)", (stop, city + "-trip"))
                connection.executemany("INSERT INTO provider_entities(entity_type,provider_id,key_1) VALUES (?,?,?)", (
                    ("raw_stops", provider, stop), ("trips", provider, city + "-trip"),
                ))
            connection.execute("DELETE FROM stop_times WHERE trip_id='vasteras-trip'")
            connection.execute("INSERT INTO city_stops VALUES ('vasteras','1049')")
            connection.execute("INSERT INTO provider_city_stops VALUES ('sweden','vasteras','1049')")
            connection.executemany("INSERT INTO raw_stops VALUES (?,?)", (
                ("se:1049", "1049"), ("de:1049", "1049"), ("se:3049", "3049"),
            ))
            connection.execute("INSERT INTO provider_city_stops VALUES ('sweden','vasteras','3049')")
            connection.execute("INSERT INTO city_stops VALUES ('vasteras','3049')")
            connection.execute("INSERT INTO trips VALUES ('germany-1049-trip','service')")
            connection.execute("INSERT INTO trips VALUES ('sweden-3049-trip','service')")
            connection.executemany("INSERT INTO stop_times(raw_stop_id,trip_id) VALUES (?,?)", (
                ("de:1049", "germany-1049-trip"), ("se:3049", "sweden-3049-trip"),
            ))
            connection.executemany(
                "INSERT INTO provider_entities(entity_type,provider_id,key_1) VALUES (?,?,?)",
                (
                    ("raw_stops", "sweden", "se:1049"),
                    ("raw_stops", "germany", "de:1049"),
                    ("trips", "germany", "germany-1049-trip"),
                    ("raw_stops", "sweden", "se:3049"),
                    ("trips", "sweden", "sweden-3049-trip"),
                ),
            )
            connection.execute("INSERT INTO active_services VALUES ('service','20261010')")
            connection.commit()
        self.common = self.release / "common.sqlite"
        with closing(sqlite3.connect(self.common)) as connection:
            connection.executescript("""
                CREATE TABLE provider_city_modes(provider_id TEXT, city_id TEXT, timezone TEXT, stop_id_prefix TEXT, mode TEXT DEFAULT 'canonical');
                CREATE TABLE provider_city_stops(provider_id TEXT, city_id TEXT, stop_id TEXT);
            """)
            for city, provider in self.pilots.items():
                connection.execute("INSERT INTO provider_city_modes(provider_id,city_id,timezone,stop_id_prefix) VALUES (?,?,?,?)", (provider, city, "UTC", ""))
                connection.execute("INSERT INTO provider_city_stops VALUES (?,?,?)", (provider, city, city + "-stop"))
            connection.commit()
        self.refs = SimpleNamespace(structural=SimpleNamespace(database_path=self.database), temporal=SimpleNamespace(database_path=self.database, valid_from="2026-10-10", valid_through="2026-10-24"))
        self.manifest = SimpleNamespace(common_database_path=self.common, providers={provider: self.refs for provider in self.pilots.values()})
        self.sources = [{"id": "non-pilot-" + city, "importIntoStaticDepartures": True, "cities": [{"id": city}]} for city in self.cities if city not in self.pilots]
        self.pin_database()

    def pin_database(self):
        self.release_manifest["fallbackDatabase"] = {"path": "departures.sqlite", "size": self.database.stat().st_size, "sha256": hashlib.sha256(self.database.read_bytes()).hexdigest()}
        (self.release / "release.json").write_text(json.dumps(self.release_manifest))

    def mutate_database(self, sql, parameters=()):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(sql, parameters)
            connection.commit()
        self.pin_database()

    def validate(self, **kwargs):
        runtime = kwargs.pop("runtime", tuple(self.pilots.values()))
        use_default_calendar_policy = kwargs.pop("use_default_calendar_policy", False)
        if not use_default_calendar_policy:
            kwargs.setdefault("require_current_schedules", True)
        with patch("scripts.static_departures_coverage._package_cities", return_value=self.package_cities), \
             patch("scripts.static_departures_coverage.load_release_manifest", return_value=self.manifest), \
             patch("scripts.static_departures_coverage.load_external_gtfs_sources", return_value=self.sources), \
             patch("scripts.static_departures_coverage.load_external_cities", side_effect=lambda source, _repository: source["cities"]):
            return validate_city_coverage(self.release, runtime, self.root, validation_date=date(2026, 10, 10), **kwargs)

    def test_full_database_checks_every_city_including_provider_pilot_cities(self):
        report = self.validate()
        self.assertEqual(report["supportedCities"], len(self.cities))
        checks = {row["cityID"]: row for row in report["checks"]}
        self.assertEqual(set(checks), set(self.cities))
        for city in self.cities:
            self.assertTrue(checks[city]["hasSchedule"], city)
            self.assertEqual(checks[city]["backend"], "fallback", city)

    def test_release_metadata_candidate_uses_only_the_full_sqlite(self):
        (self.release / "release.json").unlink()
        (self.release / "release-metadata.json").write_text(json.dumps({"releaseID": "candidate"}))

        report = self.validate(runtime=())

        self.assertEqual(report["databaseMode"], "full-sqlite")
        self.assertEqual(report["supportedCities"], len(self.cities))
        self.assertEqual({row["backend"] for row in report["checks"]}, {"sqlite"})
        self.assertTrue(all(row["hasSchedule"] for row in report["checks"]))
        vasteras = next(row for row in report["checks"] if row["cityID"] == "vasteras")
        self.assertEqual(vasteras["stopID"], "3049")

    def test_full_sqlite_candidate_rejects_provider_runtime_dependency(self):
        (self.release / "release.json").unlink()
        (self.release / "release-metadata.json").write_text(json.dumps({"releaseID": "candidate"}))

        with self.assertRaisesRegex(CityCoverageError, "must not depend on provider runtime"):
            self.validate(runtime=("germany",))

    def test_full_database_cannot_borrow_pilot_coverage_from_provider_shards(self):
        self.mutate_database("DELETE FROM city_stops WHERE city_id='israel'")
        with self.assertRaisesRegex(CityCoverageError, "cannot route.*israel"):
            self.validate()

    def mark_compact(self, provider_ids=None):
        values = {"fallbackProfile": "hybrid-only", "shardProviderIDs": json.dumps(provider_ids or list(self.pilots.values())), "excludedProviderIDs": json.dumps(list(self.pilots.values()))}
        for key, value in values.items():
            self.mutate_database("INSERT INTO metadata VALUES (?,?)", (key, value))

    def test_compact_fallback_is_rejected_for_candidate_activation(self):
        self.mark_compact()
        with self.assertRaisesRegex(CityCoverageError, "candidate departures database omits providers"):
            self.validate()

    def test_compact_rollback_can_use_shards_for_structural_coverage(self):
        self.mark_compact()
        with patch("scripts.static_departures_coverage.excluded_fallback_providers", return_value=tuple(self.pilots.values())):
            report = self.validate(require_current_schedules=False)
        self.assertEqual(report["supportedCities"], len(self.cities))
        checks = {row["cityID"]: row for row in report["checks"]}
        self.assertEqual(checks["israel"]["backend"], "shard")

    def test_compact_rollback_cannot_use_a_smaller_runtime_provider_list(self):
        self.mark_compact(["germany"])
        with patch("scripts.static_departures_coverage.excluded_fallback_providers", return_value=tuple(self.pilots.values())):
            with self.assertRaisesRegex(CityCoverageError, "provider contract"):
                self.validate(require_current_schedules=False)

    def test_missing_full_database_cannot_pass_with_valid_provider_shards(self):
        self.database.rename(self.release / "unpublished.sqlite")
        with self.assertRaisesRegex(CityCoverageError, "fallback database is missing"):
            self.validate()

    def test_nonpilot_stop_registry_gap_is_rejected(self):
        self.mutate_database("DELETE FROM city_stops WHERE city_id='small-non-pilot-city'")
        with self.assertRaisesRegex(CityCoverageError, "cannot route.*small-non-pilot-city"):
            self.validate()

    def test_dropped_nonpilot_provider_mapping_is_rejected(self):
        self.mutate_database("DELETE FROM provider_city_modes WHERE city_id='helsinki'")
        with self.assertRaisesRegex(CityCoverageError, "lost configured city/provider"):
            self.validate()

    def test_mismatched_fallback_generation_is_rejected(self):
        self.mutate_database("UPDATE metadata SET value='old' WHERE key='releaseID'")
        with self.assertRaisesRegex(CityCoverageError, "generation does not match"):
            self.validate()

    def test_modified_database_is_rejected_even_when_other_receipts_pass(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM stop_times")
            connection.commit()
        with self.assertRaisesRegex(CityCoverageError, "hash/size"):
            self.validate()

    def test_calendar_that_expires_today_is_rejected(self):
        self.mutate_database("UPDATE metadata SET value='2026-10-10' WHERE key='validThrough'")
        with self.assertRaisesRegex(CityCoverageError, "today and tomorrow"):
            self.validate()

    def test_yesterday_calendar_passes_default_candidate_policy_with_empty_today_board(self):
        self.mutate_database("UPDATE metadata SET value='2026-10-09' WHERE key='validFrom'")
        self.mutate_database("UPDATE metadata SET value='2026-10-09' WHERE key='validThrough'")
        self.mutate_database("UPDATE active_services SET service_date='20261009'")

        report = self.validate(use_default_calendar_policy=True)

        self.assertEqual(report["supportedCities"], len(self.cities))
        self.assertTrue(all(not row["hasSchedule"] for row in report["checks"]))

    def test_expired_rollback_calendar_keeps_structural_city_coverage(self):
        self.mutate_database("UPDATE metadata SET value='2026-09-10' WHERE key='validFrom'")
        self.mutate_database("UPDATE metadata SET value='2026-09-30' WHERE key='validThrough'")
        self.mutate_database("UPDATE active_services SET service_date='20260930'")
        report = self.validate(require_current_schedules=False)
        self.assertEqual(report["supportedCities"], len(self.cities))
        self.assertFalse(any(row["hasSchedule"] for row in report["checks"]))
        with self.assertRaisesRegex(CityCoverageError, "today and tomorrow"):
            self.validate()

    def test_candidate_city_set_may_shrink_or_grow_relative_to_previous_release(self):
        reference = self.root / "rollback"
        (reference / "stop-data").mkdir(parents=True)
        (reference / "release.json").write_text('{"stopData":{"path":"stop-data"}}')
        for reference_cities in ([{"id": "old-only-city"}],
                                 [{"id": city} for city in self.cities] + [{"id": "removed-city"}]):
            (reference / "stop-data/manifest.json").write_text(
                json.dumps({"cities": reference_cities})
            )
            report = self.validate(reference=reference)
            self.assertEqual(report["supportedCities"], len(self.cities))

    def test_schedule_preservation_helper_detects_loss_when_explicitly_requested(self):
        rollback = self.validate()
        self.mutate_database("DELETE FROM stop_times WHERE trip_id='helsinki-trip'")
        candidate = self.validate()
        with self.assertRaisesRegex(
            CityCoverageError, "lost schedules for retained cities: helsinki"
        ):
            require_preserved_schedules(candidate, rollback)

    def test_preserved_schedule_gate_allows_removed_city_ids(self):
        candidate = self.validate()
        rollback = {
            "checks": candidate["checks"] + [{"cityID": "removed-city", "hasSchedule": True}]
        }

        require_preserved_schedules(candidate, rollback)

    def test_raw_id_collision_does_not_claim_canonical_board_is_available(self):
        self.mutate_database("UPDATE raw_stops SET canonical_stop_id='another-stop' WHERE stop_id='bochum-stop'")
        check = next(row for row in self.validate()["checks"] if row["cityID"] == "bochum")
        self.assertFalse(check["hasSchedule"])

    def test_yesterday_only_service_is_not_a_working_schedule(self):
        self.mutate_database("UPDATE active_services SET service_date='20261009'")
        self.assertFalse(any(row["hasSchedule"] for row in self.validate()["checks"]))

    def test_package_city_with_only_empty_departure_arrays_is_rejected(self):
        self.package_cities.add("small-non-pilot-city")
        (self.stop_root / "departures").mkdir()
        (self.stop_root / "departures/small-non-pilot-city.json").write_text(json.dumps({
            "generatedAt": "20261010", "stops": {"small-non-pilot-city-stop": []},
        }))
        with self.assertRaisesRegex(CityCoverageError, "package-only city has no schedule"):
            self.validate()

    def add_berlin_overlay(self, *, tomorrow: bool):
        city = "berlin"
        stop = "476861"
        native = "de:11000:900110505::7"
        (self.stop_root / "stops/berlin.json").write_text(json.dumps([{"id": stop}]))
        self.stop_manifest["cities"].append({"id": city, "url": "stops/berlin.json", "stopCount": 1})
        (self.stop_root / "manifest.json").write_text(json.dumps(self.stop_manifest))
        directory = self.stop_root / "transit/vbb/berlin"
        directory.mkdir(parents=True)
        line_root = self.stop_root / "transit/city-lines"
        line_root.mkdir(parents=True)
        (line_root / "berlin.json").write_text(json.dumps({"lines": [
            {"routeID": "M1", "names": ["M1"], "routeType": 0},
        ]}))
        for day in ("20261010", "20261011") if tomorrow else ("20261010",):
            payload = {"stops": [{"id": native}], "trips": [{"id": "trip-" + day,
                "routeID": "M1", "lineName": "M1", "serviceDate": day,
                "stopTimes": [{"stopID": native, "departureSeconds": 3600, "stopSequence": 1}]}]}
            (directory / f"{day}-00.json").write_text(json.dumps(payload))

    def test_berlin_overlay_must_cover_today_and_tomorrow(self):
        self.add_berlin_overlay(tomorrow=True)
        check = next(row for row in self.validate()["checks"] if row["cityID"] == "berlin")
        self.assertEqual(check["backend"], "vbb-overlay")
        self.assertTrue(check["hasSchedule"])

    def test_berlin_tomorrow_service_after_six_hours_is_valid(self):
        self.add_berlin_overlay(tomorrow=True)
        path = self.stop_root / "transit/vbb/berlin/20261011-00.json"
        payload = json.loads(path.read_text())
        payload["trips"][0]["stopTimes"][0]["departureSeconds"] = 14 * 3600
        path.write_text(json.dumps(payload))
        self.assertEqual(self.validate()["status"], "PASS")

    def test_berlin_missing_tomorrow_cannot_pass_other_database_checks(self):
        self.add_berlin_overlay(tomorrow=False)
        with self.assertRaisesRegex(RuntimeError, "stale VBB overlay"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
