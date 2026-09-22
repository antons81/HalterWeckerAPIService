import hashlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock
from datetime import date
from pathlib import Path
from types import SimpleNamespace

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tests"))

import run_incremental_provider_pipeline as incremental  # noqa: E402
from test_static_provider_artifact import StaticProviderArtifactTests  # noqa: E402


class IncrementalProviderPipelineTests(unittest.TestCase):
    MIXED_PROVIDER_IDS = (
        "israel-mot",
        "ttc-surface",
        "ttc-subway",
        "norway",
        "sweden",
        "poland-warsaw",
        "poland-wkd",
        "511-bay-area",
        "australia-translink-seq",
        "australia-transport-nsw",
        "cta-chicago",
        "mbta-boston",
        "stm-montreal",
    )

    @classmethod
    def _selection_environment(cls, *, selected=None, capabilities=None):
        selected = cls.MIXED_PROVIDER_IDS if selected is None else tuple(selected)
        capabilities = cls.MIXED_PROVIDER_IDS if capabilities is None else tuple(capabilities)
        configured = ",".join(capabilities)
        return {
            incremental.INCREMENTAL_PROVIDER_IDS_ENV: ",".join(selected),
            incremental.BUILD_CACHE_PROVIDER_IDS_ENV: configured,
            incremental.DEPARTURES_V3_PROVIDER_IDS_ENV: configured,
            incremental.DEPARTURE_CACHE_PROVIDER_IDS_ENV: configured,
        }

    def test_production_selection_contains_exact_mixed_thirteen(self):
        plan = incremental.provider_selection_plan(
            REPOSITORY_ROOT,
            environ=self._selection_environment(),
        )
        self.assertEqual(plan["selectedProviders"], list(self.MIXED_PROVIDER_IDS))
        self.assertEqual(
            plan["representativeReadinessProviders"],
            list(incremental.REPRESENTATIVE_READINESS_PROVIDER_IDS),
        )
        self.assertEqual(
            plan["mergeGroups"]["poland-warsaw"]["selected"],
            ["poland-warsaw", "poland-wkd"],
        )

    def test_old_three_provider_pilot_does_not_cap_selection(self):
        plan = incremental.provider_selection_plan(
            REPOSITORY_ROOT,
            environ=self._selection_environment(),
        )
        self.assertNotEqual(plan["selectedProviders"], ["israel-mot", "ttc-surface", "ttc-subway"])
        self.assertEqual(len(plan["selectedProviders"]), 13)

    def test_unknown_provider_fails_closed(self):
        environment = self._selection_environment(selected=("unknown-provider",))
        with self.assertRaisesRegex(ValueError, "unknown incremental providers"):
            incremental.provider_selection_plan(REPOSITORY_ROOT, environ=environment)

    def test_provider_without_required_capability_fails_closed(self):
        environment = self._selection_environment(
            selected=("ireland",),
            capabilities=self.MIXED_PROVIDER_IDS,
        )
        with self.assertRaisesRegex(ValueError, "required incremental capabilities"):
            incremental.provider_selection_plan(REPOSITORY_ROOT, environ=environment)

    def test_empty_allowlist_fails_closed(self):
        environment = self._selection_environment(selected=())
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            incremental.provider_selection_plan(REPOSITORY_ROOT, environ=environment)

    def test_duplicate_ids_are_normalized_deterministically(self):
        selected = ("norway", "israel-mot", "norway", "israel-mot")
        environment = self._selection_environment(selected=selected)
        plan = incremental.provider_selection_plan(
            REPOSITORY_ROOT,
            environ=environment,
        )
        self.assertEqual(plan["selectedProviders"], ["norway", "israel-mot"])

    def test_partial_poland_merge_group_fails_closed(self):
        environment = self._selection_environment(selected=("poland-warsaw",))
        with self.assertRaisesRegex(ValueError, "partial incremental merge-group"):
            incremental.provider_selection_plan(REPOSITORY_ROOT, environ=environment)

    def test_complete_poland_merge_group_passes(self):
        selected = ("poland-warsaw", "poland-wkd")
        plan = incremental.provider_selection_plan(
            REPOSITORY_ROOT,
            environ=self._selection_environment(selected=selected),
        )
        self.assertEqual(plan["selectedProviders"], list(selected))
        self.assertTrue(plan["mergeGroups"]["poland-warsaw"]["complete"])

    def test_deferred_providers_are_excluded_from_mixed_plan(self):
        plan = incremental.provider_selection_plan(
            REPOSITORY_ROOT,
            environ=self._selection_environment(),
        )
        excluded = {item["providerID"] for item in plan["excludedProviders"]}
        self.assertTrue({"finland-hsl", "ireland", "wmata-bus"}.issubset(excluded))

    def test_capability_status_is_reported_for_every_selected_provider(self):
        plan = incremental.provider_selection_plan(
            REPOSITORY_ROOT,
            environ=self._selection_environment(),
        )
        self.assertEqual(set(plan["capabilityStatus"]), set(self.MIXED_PROVIDER_IDS))
        for status in plan["capabilityStatus"].values():
            self.assertEqual(
                status,
                {
                    "normalized": True,
                    "structural": True,
                    "departuresV3": True,
                    "stopSetDigest": True,
                },
            )

    def test_artifact_strategy_reports_only_validated_normalized_providers(self):
        plan = incremental.provider_selection_plan(
            REPOSITORY_ROOT,
            environ=self._selection_environment(),
        )
        self.assertEqual(
            plan["artifactStrategies"]["israel-mot"],
            "normalized-required",
        )
        self.assertEqual(
            plan["artifactStrategies"]["ttc-surface"],
            "normalized-required",
        )
        self.assertEqual(plan["artifactStrategies"]["norway"], "unsupported")

    def test_unsupported_artifact_strategy_fails_before_provider_work(self):
        with self.assertRaisesRegex(
            ValueError,
            r"artifact strategy preflight failed before provider work: norway=unsupported",
        ):
            incremental.validate_incremental_artifact_strategies(
                REPOSITORY_ROOT,
                ("norway",),
            )

    def test_structural_cache_miss_fails_closed_without_builder_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_key = SimpleNamespace(
                value="structural-cache-key",
                builder_fingerprint="builder-fingerprint",
                city_ids=("fixture-city",),
                projection_fingerprint="projection-fingerprint",
            )
            cache = Mock()
            cache.probe.return_value = SimpleNamespace(
                status="MISS",
                reason="cache key not found",
                manifest=None,
            )
            environment = {
                "HALTEWECKER_EXTERNAL_BUILD_CACHE": "1",
                "HALTEWECKER_EXTERNAL_TRANSFORMED_BUILD_CACHE": "1",
                incremental.BUILD_CACHE_PROVIDER_IDS_ENV: "norway",
            }
            with (
                unittest.mock.patch.object(
                    incremental, "cache_key", return_value=cache_key
                ),
                unittest.mock.patch.object(
                    incremental, "ExternalBuildCache", return_value=cache
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "structural-sufficient cache is not reusable for norway: MISS",
                ):
                    incremental._load_structural_provider_context(
                        archive=Mock(),
                        repository_root=REPOSITORY_ROOT,
                        provider_id="norway",
                        raw_sha="a" * 64,
                        source={"buildTripIndex": True},
                        cities=[{"id": "fixture-city"}],
                        sources={"norway": {"mergeGroup": ""}},
                        stop_data_root=Path(temporary),
                        normalized_cache_root=Path(temporary) / "normalized",
                        environ=environment,
                    )
            cache.probe.assert_called_once_with(cache_key)

    def test_failed_initialization_preserves_original_exception(self):
        for archive in (None, Mock()):
            with self.subTest(archive_created=archive is not None):
                original = ValueError("normalized or archive initialization failed")
                with self.assertRaises(ValueError) as caught:
                    try:
                        raise original
                    finally:
                        incremental._close_provider_resources(None, archive)
                self.assertIs(caught.exception, original)
                if archive is not None:
                    archive.close.assert_called_once_with()

    def test_cleanup_failure_does_not_mask_primary_and_closes_archive(self):
        context, archive = Mock(), Mock()
        context.close.side_effect = RuntimeError("context cleanup failed")
        archive.close.side_effect = RuntimeError("archive cleanup failed")
        original = ValueError("original failure")
        with self.assertRaises(ValueError) as caught:
            try:
                raise original
            finally:
                incremental._close_provider_resources(context, archive)
        self.assertIs(caught.exception, original)
        archive.close.assert_called_once_with()

    def test_cleanup_failure_without_primary_fails_closed(self):
        context, archive = Mock(), Mock()
        context.close.side_effect = RuntimeError("cleanup failed")
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            incremental._close_provider_resources(context, archive)
        archive.close.assert_called_once_with()

    @staticmethod
    def _write_active_services(path: Path, service_dates: list[str]) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE active_services(service_id TEXT, service_date TEXT)"
            )
            connection.executemany(
                "INSERT INTO active_services VALUES (?, ?)",
                [
                    (f"service-{index}", date.fromisoformat(value).strftime("%Y%m%d"))
                    for index, value in enumerate(service_dates)
                ],
            )

    @staticmethod
    def _write_trip_databases(
        structural: Path,
        temporal: Path,
        *,
        active_service_ids: list[str],
        trips: list[tuple[str, str]],
        service_date: str = "2026-09-12",
    ) -> None:
        with sqlite3.connect(temporal) as connection:
            connection.execute(
                "CREATE TABLE active_services(service_id TEXT, service_date TEXT)"
            )
            connection.executemany(
                "INSERT INTO active_services VALUES (?, ?)",
                [
                    (service_id, date.fromisoformat(service_date).strftime("%Y%m%d"))
                    for service_id in active_service_ids
                ],
            )
        with sqlite3.connect(structural) as connection:
            connection.execute("CREATE TABLE trips(service_id TEXT, trip_id TEXT)")
            connection.executemany("INSERT INTO trips VALUES (?, ?)", trips)


    def test_readiness_provider_rows_use_explicit_provider_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            structural_database = Path(temporary) / "structural.sqlite"
            with sqlite3.connect(structural_database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE provider_city_stops(
                        provider_id TEXT NOT NULL,
                        city_id TEXT NOT NULL,
                        stop_id TEXT NOT NULL
                    );
                    CREATE TABLE provider_city_modes(
                        provider_id TEXT NOT NULL,
                        city_id TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        stop_id_prefix TEXT NOT NULL,
                        identifier_prefix TEXT NOT NULL
                    );
                    INSERT INTO provider_city_stops VALUES
                        ('israel-mot', 'israel', '100');
                    INSERT INTO provider_city_modes VALUES
                        ('israel-mot', 'israel', 'bus', 'Asia/Jerusalem', '', '');
                    """
                )

            stop_rows, mode_rows = incremental._provider_rows(
                "israel-mot",
                structural_database,
            )
            self.assertEqual(incremental._first_stop(stop_rows, "israel"), "100")
            self.assertEqual(mode_rows[0][0], "israel-mot")

            with self.assertRaisesRegex(ValueError, "provider=unknown-provider"):
                incremental._provider_rows("unknown-provider", structural_database)

    def test_common_catalog_restores_provider_stop_prefix_from_source_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider_builds = []
            for provider_id in ("ttc-surface", "ttc-subway"):
                structural_database = root / f"{provider_id}-structural.sqlite"
                with sqlite3.connect(structural_database) as connection:
                    connection.executescript(
                        """
                        CREATE TABLE provider_city_stops(
                            provider_id TEXT NOT NULL,
                            city_id TEXT NOT NULL,
                            stop_id TEXT NOT NULL
                        );
                        CREATE TABLE provider_city_modes(
                            provider_id TEXT NOT NULL,
                            city_id TEXT NOT NULL,
                            mode TEXT NOT NULL,
                            timezone TEXT NOT NULL,
                            stop_id_prefix TEXT NOT NULL,
                            identifier_prefix TEXT NOT NULL
                        );
                        """
                    )
                    connection.execute(
                        "INSERT INTO provider_city_stops VALUES (?, 'toronto', ?)",
                        (provider_id, f"{provider_id}:100"),
                    )
                    connection.execute(
                        "INSERT INTO provider_city_modes VALUES (?, 'toronto', 'canonical', 'America/Toronto', '', '')",
                        (provider_id,),
                    )

                temporal_database = root / f"{provider_id}-temporal.sqlite"
                temporal_database.touch()

                def artifact_use(
                    database: Path,
                    key: str,
                    schema_key: str,
                    schema_version: int,
                    dependencies=None,
                ):
                    return SimpleNamespace(
                        database_path=database,
                        artifact_directory=database.parent,
                        artifact_key=key,
                        manifest={
                            "sqlite": {
                                "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                                "size": database.stat().st_size,
                            },
                            schema_key: schema_version,
                            "dependencies": dependencies or {},
                        },
                    )

                provider_builds.append(
                    incremental.ProviderBuild(
                        provider_id=provider_id,
                        source={
                            "namespace": f"{provider_id}:",
                            "identifierPrefix": f"{provider_id}:",
                            "timezone": "America/Toronto",
                            "mergeGroup": "toronto",
                        },
                        cities=[{"id": "toronto"}],
                        normalized=object(),
                        structural=artifact_use(
                            structural_database,
                            f"{provider_id}-structural",
                            "structuralSchemaVersion",
                            2,
                        ),
                        temporal=artifact_use(
                            temporal_database,
                            f"{provider_id}-temporal",
                            "temporalSchemaVersion",
                            1,
                            {"validFrom": "2026-09-13", "validThrough": "2026-09-27"},
                        ),
                        provider_release_entry={},
                    )
                )

            output = root / "common.sqlite"
            incremental._build_common_catalog(
                output=output,
                release_id="release-x",
                provider_builds=provider_builds,
            )
            with sqlite3.connect(output) as connection:
                rows = connection.execute(
                    """
                    SELECT provider_id, city_id, stop_id_prefix, identifier_prefix
                    FROM provider_city_modes
                    ORDER BY provider_id
                    """
                ).fetchall()

            self.assertEqual(
                rows,
                [
                    ("ttc-subway", "toronto", "ttc-subway:", ""),
                    ("ttc-surface", "toronto", "ttc-surface:", ""),
                ],
            )
            self.assertFalse(any(row[0] == "" or row[1] == "" for row in rows))

    def test_configured_prefix_does_not_infer_ambiguous_global_prefix(self) -> None:
        self.assertEqual(
            incremental._configured_stop_id_prefix(
                {"namespace": "", "identifierPrefix": ""}
            ),
            "",
        )

    def test_probe_date_skips_window_start_without_active_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "temporal.sqlite"
            self._write_active_services(database, ["2026-09-13"])
            self.assertEqual(
                incremental._select_probe_date(
                    provider_id="israel-mot",
                    temporal_database=database,
                    dates=[date(2026, 9, 12), date(2026, 9, 13)],
                ),
                date(2026, 9, 13),
            )

    def test_probe_date_prefers_window_start_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "temporal.sqlite"
            self._write_active_services(database, ["2026-09-12", "2026-09-14"])
            kwargs = {
                "provider_id": "israel-mot",
                "temporal_database": database,
                "dates": [date(2026, 9, 12), date(2026, 9, 14)],
            }
            self.assertEqual(incremental._select_probe_date(**kwargs), date(2026, 9, 12))
            self.assertEqual(incremental._select_probe_date(**kwargs), date(2026, 9, 12))

    def test_probe_date_fails_closed_when_window_has_no_active_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "temporal.sqlite"
            self._write_active_services(database, ["2026-09-11", "2026-09-16"])
            with self.assertRaisesRegex(
                ValueError,
                "provider=israel-mot has no active service within 2026-09-12..2026-09-15",
            ):
                incremental._select_probe_date(
                    provider_id="israel-mot",
                    temporal_database=database,
                    dates=[date(2026, 9, 12), date(2026, 9, 15)],
                )

    def test_first_trip_uses_intersection_after_first_twenty_active_services(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            structural = Path(temporary) / "structural.sqlite"
            temporal = Path(temporary) / "temporal.sqlite"
            active_ids = [f"israel:{index:03d}" for index in range(25)]
            self._write_trip_databases(
                structural,
                temporal,
                active_service_ids=active_ids,
                trips=[(active_ids[20], "trip-21")],
            )
            case = incremental._first_trip_case(
                provider_id="israel-mot",
                structural_database=structural,
                temporal_database=temporal,
                city_id="israel",
                service_date=date(2026, 9, 12),
                static_root=Path(temporary) / "stop-data",
            )
            self.assertEqual(case["tripID"], "trip-21")

    def test_first_trip_selection_is_deterministic_with_many_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            structural = Path(temporary) / "structural.sqlite"
            temporal = Path(temporary) / "temporal.sqlite"
            active_ids = [f"israel:{index:04d}" for index in range(300)]
            self._write_trip_databases(
                structural,
                temporal,
                active_service_ids=active_ids,
                trips=[
                    (active_ids[250], "trip-z"),
                    (active_ids[20], "trip-b"),
                    (active_ids[20], "trip-a"),
                ],
            )
            case = incremental._first_trip_case(
                provider_id="israel-mot",
                structural_database=structural,
                temporal_database=temporal,
                city_id="israel",
                service_date=date(2026, 9, 12),
                static_root=Path(temporary) / "stop-data",
            )
            self.assertEqual(case["tripID"], "trip-a")

    def test_first_trip_fails_closed_without_structural_intersection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            structural = Path(temporary) / "structural.sqlite"
            temporal = Path(temporary) / "temporal.sqlite"
            self._write_trip_databases(
                structural,
                temporal,
                active_service_ids=["israel:001", "israel:002"],
                trips=[("israel:foreign", "trip-foreign")],
            )
            with self.assertRaisesRegex(
                ValueError,
                "provider=israel-mot date=2026-09-12 active_service_count=2 "
                "matching_structural_service_count=0",
            ):
                incremental._first_trip_case(
                    provider_id="israel-mot",
                    structural_database=structural,
                    temporal_database=temporal,
                    city_id="israel",
                    service_date=date(2026, 9, 12),
                    static_root=Path(temporary) / "stop-data",
                )

    def test_first_trip_is_scoped_to_the_supplied_provider_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            structural = Path(temporary) / "structural.sqlite"
            foreign_structural = Path(temporary) / "foreign-structural.sqlite"
            temporal = Path(temporary) / "temporal.sqlite"
            self._write_trip_databases(
                structural,
                temporal,
                active_service_ids=["israel:001", "ttc-subway:001"],
                trips=[],
            )
            self._write_trip_databases(
                foreign_structural,
                Path(temporary) / "foreign-temporal.sqlite",
                active_service_ids=["ttc-subway:001"],
                trips=[("ttc-subway:001", "foreign-trip")],
            )
            with self.assertRaisesRegex(ValueError, "matching_structural_service_count=0"):
                incremental._first_trip_case(
                    provider_id="israel-mot",
                    structural_database=structural,
                    temporal_database=temporal,
                    city_id="israel",
                    service_date=date(2026, 9, 12),
                    static_root=Path(temporary) / "stop-data",
                )

    def test_probe_date_rejects_active_service_outside_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "temporal.sqlite"
            self._write_active_services(database, ["2026-09-16"])
            with self.assertRaises(ValueError):
                incremental._select_probe_date(
                    provider_id="israel-mot",
                    temporal_database=database,
                    dates=[date(2026, 9, 12), date(2026, 9, 15)],
                )

    def test_merged_probe_date_uses_earliest_common_active_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            surface = Path(temporary) / "surface.sqlite"
            subway = Path(temporary) / "subway.sqlite"
            self._write_active_services(surface, ["2026-09-12", "2026-09-14"])
            self._write_active_services(subway, ["2026-09-13", "2026-09-14"])
            self.assertEqual(
                incremental._select_common_probe_date(
                    provider_temporal_databases={
                        "ttc-surface": surface,
                        "ttc-subway": subway,
                    },
                    dates=[date(2026, 9, 12), date(2026, 9, 15)],
                ),
                date(2026, 9, 14),
            )

    def test_merged_probe_date_fails_closed_without_intersection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            surface = Path(temporary) / "surface.sqlite"
            subway = Path(temporary) / "subway.sqlite"
            self._write_active_services(surface, ["2026-09-12"])
            self._write_active_services(subway, ["2026-09-13"])
            with self.assertRaisesRegex(ValueError, "no common active service"):
                incremental._select_common_probe_date(
                    provider_temporal_databases={
                        "ttc-surface": surface,
                        "ttc-subway": subway,
                    },
                    dates=[date(2026, 9, 12), date(2026, 9, 13)],
                )

    def test_temporal_window_requires_next_day(self) -> None:
        with self.assertRaises(ValueError):
            incremental.service_dates(
                valid_from=date(2026, 9, 10),
                valid_through=date(2026, 9, 10),
            )

    def test_temporal_window_is_anchored_to_iso_week(self) -> None:
        monday = incremental.service_dates(valid_from=date(2026, 9, 7))
        tuesday = incremental.service_dates(valid_from=date(2026, 9, 8))
        sunday = incremental.service_dates(valid_from=date(2026, 9, 13))
        next_monday = incremental.service_dates(valid_from=date(2026, 9, 14))

        self.assertEqual(monday, tuesday)
        self.assertEqual(tuesday, sunday)
        self.assertEqual(monday[0], date(2026, 9, 7))
        self.assertEqual(monday[-1], date(2026, 9, 27))
        self.assertEqual(len(monday), 21)
        self.assertNotEqual(monday, next_monday)
        self.assertEqual(next_monday[0], date(2026, 9, 14))
        self.assertEqual(next_monday[-1], date(2026, 10, 4))

    def test_temporal_window_handles_iso_year_boundary(self) -> None:
        end_of_year = incremental.service_dates(valid_from=date(2026, 12, 31))
        new_year = incremental.service_dates(valid_from=date(2027, 1, 1))
        next_week = incremental.service_dates(valid_from=date(2027, 1, 4))

        self.assertEqual(end_of_year, new_year)
        self.assertEqual(end_of_year[0], date(2026, 12, 28))
        self.assertEqual(end_of_year[-1], date(2027, 1, 17))
        self.assertNotEqual(end_of_year, next_week)

    def test_temporal_window_rejects_non_anchored_endpoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "validThrough"):
            incremental.service_dates(
                valid_from=date(2026, 9, 10),
                valid_through=date(2026, 9, 25),
            )

    def test_structural_manifest_does_not_require_temporal_window(self) -> None:
        structural_manifest = {
            "artifactType": "structural",
            "dependencies": {
                "stopDataFingerprint": "stop-data-key",
                "structuralSchemaFingerprint": "schema-key",
            },
        }
        temporal_manifest = {
            "artifactType": "temporal",
            "dependencies": {
                "validFrom": "2026-09-11",
                "validThrough": "2026-09-25",
            },
        }

        self.assertNotIn("validFrom", structural_manifest["dependencies"])
        self.assertNotIn("validThrough", structural_manifest["dependencies"])
        incremental._validate_temporal_window(
            provider_id="israel-mot",
            temporal_manifest=temporal_manifest,
            dates=[date(2026, 9, 11), date(2026, 9, 25)],
        )

    def test_temporal_window_mismatch_fails_closed(self) -> None:
        temporal_manifest = {
            "dependencies": {
                "validFrom": "2026-09-12",
                "validThrough": "2026-09-25",
            }
        }
        with self.assertRaisesRegex(ValueError, "temporal validFrom mismatch"):
            incremental._validate_temporal_window(
                provider_id="israel-mot",
                temporal_manifest=temporal_manifest,
                dates=[date(2026, 9, 11), date(2026, 9, 25)],
            )

    def test_temporal_window_missing_fields_fails_closed(self) -> None:
        temporal_manifest = {"dependencies": {"validFrom": "2026-09-11"}}
        with self.assertRaisesRegex(ValueError, "temporal validThrough mismatch"):
            incremental._validate_temporal_window(
                provider_id="israel-mot",
                temporal_manifest=temporal_manifest,
                dates=[date(2026, 9, 11), date(2026, 9, 25)],
            )

    def test_raw_artifact_provenance_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "feed.zip"
            raw.write_bytes(b"fixture")
            payload = {"external": {"israel-mot": {"path": str(raw), "sha256": "wrong", "size": 7}}}
            with self.assertRaises(ValueError):
                incremental._raw_entry(payload, "israel-mot")

    def test_provider_scope_is_fixed_to_phase_six_pilot(self) -> None:
        with self.assertRaises(ValueError):
            incremental.build_incremental_candidate(
                repository_root=REPOSITORY_ROOT,
                release_id="release-a",
                releases_root=Path(tempfile.mkdtemp()),
                stop_data_root=Path(tempfile.mkdtemp()),
                gtfs_artifacts_path=Path(tempfile.mktemp()),
                normalized_cache_root=Path(tempfile.mkdtemp()),
                static_artifact_root=Path(tempfile.mkdtemp()),
                dates=[date(2026, 9, 10), date(2026, 9, 11)],
                provider_ids=("israel-mot",),
            )

    def test_candidate_release_directory_is_distinct_from_source_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_generation = root / "releases" / "release-a"
            candidate_root = root / "releases" / "incremental"
            source_generation.mkdir(parents=True)
            candidate_root.mkdir(parents=True)
            self.assertNotEqual(source_generation, candidate_root / "release-a")
            self.assertFalse((candidate_root / "release-a").exists())

    def test_published_directory_is_used_after_staging_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / ".release-a.incremental-test"
            published = root / "candidate" / "release-a"
            staging.mkdir(parents=True)
            published.mkdir(parents=True)
            assembly = SimpleNamespace(release_directory=published)

            resolved = incremental._published_release_directory(
                assembly,
                staging_directory=staging,
            )
            shutil.rmtree(staging)

            self.assertEqual(resolved, published.resolve())
            self.assertTrue(resolved.is_dir())
            self.assertNotIn(".release-a.incremental", str(resolved))

    def test_staging_path_is_rejected_if_atomic_publish_did_not_happen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary) / ".release-a.incremental-test"
            staging.mkdir()

            with self.assertRaisesRegex(FileNotFoundError, "published incremental candidate"):
                incremental._published_release_directory(
                    SimpleNamespace(release_directory=staging),
                    staging_directory=staging,
                )

    def test_staging_parent_is_created_before_incremental_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            releases_root = Path(temporary) / "releases" / "incremental" / "run-1"

            staging = incremental._create_incremental_staging_directory(
                releases_root,
                "release-a",
            )

            self.assertTrue(staging.is_dir())
            self.assertEqual(staging.parent, releases_root.parent)
            self.assertTrue(releases_root.parent.is_dir())

    def test_provider_layers_reuse_immutable_artifacts_and_roll_temporal_only(self) -> None:
        helper = StaticProviderArtifactTests()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed, source, cities, stop_data = helper._prepare_inputs(root)
            first = helper._build_artifacts(
                root,
                feed,
                source,
                cities,
                stop_data,
                [date(2026, 9, 10), date(2026, 9, 11)],
            )
            second = helper._build_artifacts(
                root,
                feed,
                source,
                cities,
                stop_data,
                [date(2026, 9, 10), date(2026, 9, 11)],
            )
            rolling = helper._build_artifacts(
                root,
                feed,
                source,
                cities,
                stop_data,
                [date(2026, 9, 11), date(2026, 9, 12)],
            )
            self.assertEqual(first.structural.status, "MISS")
            self.assertEqual(first.temporal.status, "MISS")
            self.assertEqual(second.structural.status, "HIT")
            self.assertEqual(second.temporal.status, "HIT")
            self.assertEqual(rolling.structural.status, "HIT")
            self.assertEqual(rolling.temporal.status, "MISS")
            self.assertEqual(second.structural.bytes_written, 0)
            self.assertEqual(second.temporal.bytes_written, 0)


if __name__ == "__main__":
    unittest.main()
