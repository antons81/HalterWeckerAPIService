"""Provider allocation forecasts, including reuse and isolated changes.

Created by Anton on 2026-10-11.
"""
import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts.estimate_provider_disk import (
    forecast_full_sqlite_build_cache_misses,
    forecast_provider_allocations,
    MIB,
)


class ProviderDiskForecastTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        raw = self.root / "feed.zip"
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("stop_times.txt", b"x" * MIB)
            archive.writestr("calendar.txt", b"x" * 100)
        provider = {"providerID": "a", "raw": {"status": "HIT", "path": str(raw)},
                    **{layer: {"status": "HIT"} for layer in ("normalized", "structural", "temporal")}}
        second = copy.deepcopy(provider)
        second["providerID"] = "b"
        self.plan = {"providers": [provider, second],
                     "dates": {"validFrom": "2026-10-05", "validThrough": "2026-10-25"}}

    def test_no_change_has_no_additional_provider_allocations(self):
        self.assertEqual(forecast_provider_allocations(self.plan, None)["additionalBytes"], 0)

    def test_changed_provider_does_not_charge_other_provider(self):
        self.plan["providers"][0]["structural"]["status"] = "MISS"
        result = forecast_provider_allocations(self.plan, None)
        self.assertGreaterEqual(result["additionalBytes"], 2 * MIB)
        self.assertEqual(result["providers"][1]["additionalBytes"], 0)

    def test_calendar_rollover_does_not_charge_structural_rebuild(self):
        self.plan["providers"][0]["temporal"]["status"] = "MISS"
        result = forecast_provider_allocations(self.plan, None)
        self.assertEqual(result["additionalBytes"], MIB)
        self.assertEqual(result["providers"][0]["layers"]["structural"], 0)

    def test_previous_database_is_lower_bound_not_new_allocation_on_hit(self):
        database = self.root / "provider.sqlite"
        with database.open("wb") as stream:
            stream.truncate(10 * MIB)
        (self.root / "release.json").write_text(json.dumps({"providers": {
            "a": {"structural": {"path": "provider.sqlite"}}, "b": {}}}))
        self.assertEqual(forecast_provider_allocations(self.plan, self.root)["additionalBytes"], 0)
        self.plan["providers"][0]["structural"]["status"] = "MISS"
        self.assertEqual(forecast_provider_allocations(self.plan, self.root)["additionalBytes"], 125 * MIB // 10)

    def test_invalid_cache_or_unavailable_provider_cannot_be_zero_cost_pass(self):
        for layer in ("raw", "normalized", "structural", "temporal"):
            with self.subTest(layer=layer):
                plan = copy.deepcopy(self.plan)
                plan["providers"][0][layer]["status"] = "INVALID"
                with self.assertRaises(ValueError):
                    forecast_provider_allocations(plan, None)

    def test_full_sqlite_forecast_charges_only_transformed_cache_misses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            stop_data = root / "stop-data"
            normalized = root / "provider-artifacts" / "normalized"
            cache_root = root / "cache" / "external-build"
            raw = root / "feed.zip"
            with zipfile.ZipFile(raw, "w") as archive:
                archive.writestr("stops.txt", b"stop")

            cached_generation = cache_root / "provider" / "old-generation"
            output = cached_generation / "stops" / "city.json"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"s" * (2 * MIB))
            manifest = {
                "providerID": "provider",
                "complete": True,
                "status": "complete",
                "cachedOutputs": [
                    {"path": "stops/city.json", "size": output.stat().st_size}
                ],
            }
            (cached_generation / "manifest.json").write_text(json.dumps(manifest))

            source = {
                "id": "provider",
                "artifactGroup": "external",
                "buildTripIndex": False,
            }
            environment = {
                "HALTEWECKER_INCREMENTAL_PROVIDER_IDS": "provider",
                "HALTEWECKER_EXTERNAL_BUILD_CACHE_PROVIDERS": "provider",
                "HALTEWECKER_EXTERNAL_BUILD_CACHE": "1",
                "HALTEWECKER_EXTERNAL_TRANSFORMED_BUILD_CACHE": "1",
                "HALTEWECKER_EXTERNAL_BUILD_CACHE_ROOT": str(cache_root),
            }
            lookup = SimpleNamespace(status="MISS", reason="new raw SHA")
            key = SimpleNamespace(value="new-cache-key")
            artifacts = {
                "external": {
                    "provider": {
                        "path": str(raw),
                        "sha256": "f" * 64,
                    }
                }
            }

            with (
                mock.patch(
                    "scripts.run_incremental_provider_pipeline.provider_selection_plan",
                    return_value={"selectedProviders": ["provider"]},
                ),
                mock.patch(
                    "scripts.run_incremental_provider_pipeline._source_map",
                    return_value={"provider": source},
                ),
                mock.patch(
                    "scripts.run_incremental_provider_pipeline._raw_entry",
                    return_value=(raw, "f" * 64),
                ),
                mock.patch(
                    "scripts.run_incremental_provider_pipeline._provider_cities",
                    return_value=[{"id": "city"}],
                ),
                mock.patch(
                    "scripts.run_incremental_provider_pipeline._probe_external_build_cache",
                    return_value=(key, lookup, ("city",)),
                ),
            ):
                result = forecast_full_sqlite_build_cache_misses(
                    repository_root=repository,
                    stop_data_root=stop_data,
                    gtfs_artifacts=artifacts,
                    normalized_cache_root=normalized,
                    runtime_provider_ids="",
                    environ=environment,
                )

            baseline = (
                (output.stat().st_size + 4095) // 4096 * 4096
                + ((cached_generation / "manifest.json").stat().st_size + 4095)
                // 4096
                * 4096
            )
            expected = ((baseline * 5 + 3) // 4 + 4095) // 4096 * 4096
            self.assertEqual(result["mode"], "full-sqlite-build-cache-only")
            self.assertEqual(result["runtimeProviders"], [])
            self.assertEqual(result["additionalBytes"], expected)
            self.assertEqual(result["providers"][0]["status"], "MISS")

    def test_full_sqlite_forecast_does_not_charge_a_valid_cache_hit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "feed.zip"
            with zipfile.ZipFile(raw, "w") as archive:
                archive.writestr("stops.txt", b"stop")
            source = {
                "id": "provider",
                "artifactGroup": "external",
                "buildTripIndex": False,
            }
            environment = {
                "HALTEWECKER_INCREMENTAL_PROVIDER_IDS": "provider",
                "HALTEWECKER_EXTERNAL_BUILD_CACHE_PROVIDERS": "provider",
                "HALTEWECKER_EXTERNAL_BUILD_CACHE": "1",
                "HALTEWECKER_EXTERNAL_TRANSFORMED_BUILD_CACHE": "1",
                "HALTEWECKER_EXTERNAL_BUILD_CACHE_ROOT": str(root / "cache"),
            }
            artifacts = {"external": {"provider": {"path": str(raw)}}}

            with (
                mock.patch(
                    "scripts.run_incremental_provider_pipeline.provider_selection_plan",
                    return_value={"selectedProviders": ["provider"]},
                ),
                mock.patch(
                    "scripts.run_incremental_provider_pipeline._source_map",
                    return_value={"provider": source},
                ),
                mock.patch(
                    "scripts.run_incremental_provider_pipeline._raw_entry",
                    return_value=(raw, "f" * 64),
                ),
                mock.patch(
                    "scripts.run_incremental_provider_pipeline._provider_cities",
                    return_value=[{"id": "city"}],
                ),
                mock.patch(
                    "scripts.run_incremental_provider_pipeline._probe_external_build_cache",
                    return_value=(
                        SimpleNamespace(value="hit-key"),
                        SimpleNamespace(status="HIT", reason="validated cache"),
                        ("city",),
                    ),
                ),
            ):
                result = forecast_full_sqlite_build_cache_misses(
                    repository_root=root / "repo",
                    stop_data_root=root / "stop-data",
                    gtfs_artifacts=artifacts,
                    normalized_cache_root=root / "provider-artifacts" / "normalized",
                    runtime_provider_ids="",
                    environ=environment,
                )

            self.assertEqual(result["additionalBytes"], 0)
            self.assertEqual(result["providers"][0]["status"], "HIT")
