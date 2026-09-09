import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

TESTS_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = TESTS_ROOT.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "services"))
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
sys.path.insert(0, str(TESTS_ROOT))

from static_departures_api import Database  # noqa: E402
from static_departures_runtime import (  # noqa: E402
    HYBRID_ENV,
    HYBRID_RELEASE_POINTER_ENV,
    ISRAEL_PROVIDER_ID,
    HybridStaticDeparturesBackend,
    ReleaseSnapshot,
    ReleaseManager,
    RuntimeUnavailable,
    ShadowStaticDeparturesBackend,
    compare_results,
    hybrid_backend_from_environment,
    shadow_backend_from_environment,
)
import test_static_provider_artifact as static_provider_tests  # noqa: E402


class _HybridFakeCatalog:
    def __init__(self, providers_by_city: dict[str, tuple[str, ...]], providers_by_stop: dict[tuple[str, str], tuple[str, ...]]):
        self.providers_by_city = providers_by_city
        self.providers_by_stop = providers_by_stop

    def resolve_city(self, city_id: str) -> str:
        return city_id

    def providers_for_city(self, city_id: str) -> tuple[str, ...]:
        return self.providers_by_city.get(city_id, ())

    def providers_for_stop(self, city_id: str, stop_id: str) -> tuple[str, ...]:
        return self.providers_by_stop.get((city_id, stop_id), self.providers_for_city(city_id))

    def provider_mode(self, city_id: str, provider_id: str):
        return type("Mode", (), {
            "mode": "canonical",
            "timezone": "UTC",
            "stop_id_prefix": f"{provider_id}:",
            "identifier_prefix": f"{provider_id}:",
        })()


class _HybridFakeProvider:
    def __init__(self, provider_id: str):
        self.provider_id = provider_id

    def trip_details(self, city_id: str, trip_id: str, static_root: str, service_date: str | None = None):
        return {"backend": "shard", "provider": self.provider_id, "tripID": trip_id}

    def trip_registry(self):
        return {f"{self.provider_id}:trip"}, {}

    def realtime_metadata(self):
        return {f"{self.provider_id}:trip"}, {f"{self.provider_id}:route"}, {}, {}

    def route_type_registry(self):
        return {}

    def route_metadata(self):
        return {}

    def stop_registry(self):
        return set()

    def trip_stop_registry(self, trip_ids: set[str]):
        return {}


class _HybridFakeSnapshot:
    def __init__(self, release_id: str, providers_by_city: dict[str, tuple[str, ...]], providers_by_stop: dict[tuple[str, str], tuple[str, ...]], *, fail_lines: bool = False):
        self.release_id = release_id
        self.catalog = _HybridFakeCatalog(providers_by_city, providers_by_stop)
        self.fail_lines = fail_lines

    def city_has_stop(self, city_id: str, stop_id: str) -> bool:
        return True

    def provider_modes(self, city_id: str):
        return tuple(self.catalog.provider_mode(city_id, provider_id) for provider_id in self.catalog.providers_for_city(city_id))

    def lines(self, city_id: str, stop_id: str):
        if self.fail_lines:
            raise RuntimeUnavailable("synthetic shard failure")
        return [{"backend": "shard", "cityID": city_id, "stopID": stop_id}]

    def external_departures_for(self, city_id: str, stop_id: str, limit: int, from_datetime, timezone_name: str, now_provider=None):
        return [{"backend": "shard", "stopID": stop_id}]

    def board(self, city_id: str, stop_id: str, limit: int, from_date=None, to_date=None):
        return [{"backend": "shard", "stopID": stop_id}]

    def provider(self, provider_id: str):
        return _HybridFakeProvider(provider_id)


class _HybridFakeLease:
    def __init__(self, snapshot: _HybridFakeSnapshot):
        self.snapshot = snapshot
        self.release_id = snapshot.release_id

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return None


class _HybridFakeManager:
    def __init__(self, snapshot: _HybridFakeSnapshot):
        self.snapshot = snapshot
        self.closed = False

    def acquire_snapshot(self):
        return _HybridFakeLease(self.snapshot)

    def switch(self, snapshot: _HybridFakeSnapshot):
        self.snapshot = snapshot

    def close(self):
        self.closed = True


class _HybridFakeLegacy:
    def __init__(self):
        self.lines_calls = 0
        self.closed = False

    def resolve_city(self, city_id: str) -> str:
        return city_id

    def city_has_stop(self, city_id: str, stop_id: str) -> bool:
        return False

    def city_departure_mode(self, city_id: str):
        return "legacy", "UTC", "", ""

    def city_departure_prefixes(self, city_id: str):
        return (), ()

    def lines(self, city_id: str, stop_id: str):
        self.lines_calls += 1
        return [{"backend": "legacy", "cityID": city_id, "stopID": stop_id}]

    def external_departures_for(self, *args, **kwargs):
        return [{"backend": "legacy"}]

    def board(self, *args, **kwargs):
        return [{"backend": "legacy"}]

    def trip_details(self, *args, **kwargs):
        return {"backend": "legacy"}

    def provider_trip_registry(self, provider_id: str):
        return {"legacy:trip"}, {}

    def provider_realtime_metadata(self, provider_id: str):
        return set(), set(), {}, {}

    def provider_realtime_registry(self, provider_id: str):
        return set(), set(), {}

    def provider_route_type_registry(self, provider_id: str):
        return {}

    def provider_route_metadata(self, provider_id: str):
        return {}

    def provider_stop_registry(self, provider_id: str):
        return set()

    def provider_trip_stop_registry(self, provider_id: str, trip_ids: set[str]):
        return {}

    def close(self):
        self.closed = True


class StaticDeparturesRuntimeTests(unittest.TestCase):
    def _build_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        helper = static_provider_tests.StaticProviderArtifactTests()
        feed, source, cities, stop_data = helper._prepare_inputs(root)
        sources_path = root / "sources.json"
        sources_path.write_text(json.dumps([source]), encoding="utf-8")
        artifacts = helper._build_artifacts(
            root,
            feed,
            source,
            cities,
            stop_data,
            [date(2026, 1, 5)],
        )
        legacy_path = helper._legacy_database(root, feed, sources_path, stop_data)

        release = root / "release-x"
        structural_dir = release / "providers" / ISRAEL_PROVIDER_ID / "structural"
        temporal_dir = release / "providers" / ISRAEL_PROVIDER_ID / "temporal"
        (release / "stop-data").mkdir(parents=True)
        structural_dir.mkdir(parents=True)
        temporal_dir.mkdir(parents=True)
        shutil.copytree(stop_data, release / "stop-data", dirs_exist_ok=True)
        shutil.copy2(artifacts.structural.database_path, structural_dir / "provider.sqlite")
        shutil.copy2(artifacts.structural.artifact_directory / "manifest.json", structural_dir / "manifest.json")
        shutil.copy2(artifacts.temporal.database_path, temporal_dir / "provider.sqlite")
        shutil.copy2(artifacts.temporal.artifact_directory / "manifest.json", temporal_dir / "manifest.json")

        common_path = release / "common.sqlite"
        connection = sqlite3.connect(common_path)
        try:
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE provider_registry (
                    provider_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    provider_order INTEGER NOT NULL,
                    merge_group TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    structural_artifact_key TEXT NOT NULL,
                    structural_schema_version INTEGER NOT NULL,
                    temporal_artifact_key TEXT NOT NULL,
                    temporal_schema_version INTEGER NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_through TEXT NOT NULL
                );
                CREATE TABLE city_aliases (
                    alias_city_id TEXT PRIMARY KEY,
                    canonical_city_id TEXT NOT NULL
                );
                CREATE TABLE city_stops (
                    city_id TEXT NOT NULL,
                    stop_id TEXT NOT NULL,
                    PRIMARY KEY(city_id, stop_id)
                );
                CREATE TABLE provider_city_stops (
                    provider_id TEXT NOT NULL,
                    city_id TEXT NOT NULL,
                    stop_id TEXT NOT NULL,
                    PRIMARY KEY(provider_id, city_id, stop_id)
                );
                CREATE TABLE provider_city_modes (
                    provider_id TEXT NOT NULL,
                    city_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    stop_id_prefix TEXT NOT NULL,
                    identifier_prefix TEXT NOT NULL,
                    PRIMARY KEY(provider_id, city_id)
                );
                """
            )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('releaseID', 'release-x')"
            )
            connection.execute(
                "INSERT INTO city_aliases(alias_city_id, canonical_city_id) VALUES (?, ?)",
                ("fixture-alias", "fixture-israel"),
            )
            connection.execute(
                "INSERT INTO city_stops(city_id, stop_id) VALUES (?, ?)",
                ("fixture-israel", "S1"),
            )
            connection.execute(
                """
                INSERT INTO provider_city_modes(
                    provider_id, city_id, mode, timezone, stop_id_prefix, identifier_prefix
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ISRAEL_PROVIDER_ID, "fixture-israel", "canonical", "Asia/Jerusalem", "israel:", "israel:"),
            )
            connection.commit()
        finally:
            connection.close()

        def provenance(path: Path) -> tuple[str, int]:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return digest, path.stat().st_size

        structural_manifest = json.loads((structural_dir / "manifest.json").read_text())
        temporal_manifest = json.loads((temporal_dir / "manifest.json").read_text())
        structural_sha, structural_size = provenance(structural_dir / "provider.sqlite")
        temporal_sha, temporal_size = provenance(temporal_dir / "provider.sqlite")
        connection = sqlite3.connect(common_path)
        try:
            connection.execute(
                """
                INSERT INTO provider_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ISRAEL_PROVIDER_ID,
                    "active",
                    0,
                    "",
                    "release-x",
                    structural_manifest["artifactKey"],
                    structural_manifest["structuralSchemaVersion"],
                    temporal_manifest["artifactKey"],
                    temporal_manifest["temporalSchemaVersion"],
                    temporal_manifest["dependencies"]["validFrom"],
                    temporal_manifest["dependencies"]["validThrough"],
                ),
            )
            connection.execute(
                "INSERT INTO provider_city_stops VALUES (?, ?, ?)",
                (ISRAEL_PROVIDER_ID, "fixture-israel", "S1"),
            )
            connection.commit()
        finally:
            connection.close()
        common_sha, common_size = provenance(common_path)
        release_manifest = {
            "formatVersion": 1,
            "releaseID": "release-x",
            "common": {"path": "common.sqlite", "sha256": common_sha, "size": common_size},
            "providers": {
                ISRAEL_PROVIDER_ID: {
                    "structural": {
                        "path": f"providers/{ISRAEL_PROVIDER_ID}/structural/provider.sqlite",
                        "manifestPath": f"providers/{ISRAEL_PROVIDER_ID}/structural/manifest.json",
                        "artifactKey": structural_manifest["artifactKey"],
                        "sha256": structural_sha,
                        "size": structural_size,
                    },
                    "temporal": {
                        "path": f"providers/{ISRAEL_PROVIDER_ID}/temporal/provider.sqlite",
                        "manifestPath": f"providers/{ISRAEL_PROVIDER_ID}/temporal/manifest.json",
                        "artifactKey": temporal_manifest["artifactKey"],
                        "sha256": temporal_sha,
                        "size": temporal_size,
                    },
                }
            },
        }
        (release / "release.json").write_text(
            json.dumps(release_manifest, indent=2),
            encoding="utf-8",
        )
        return legacy_path, release, stop_data

    def test_israel_query_families_match_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            legacy_path, release, stop_data = self._build_fixture(Path(temporary))
            legacy = Database(str(legacy_path))
            snapshot = ReleaseSnapshot.open(release)
            shadow = ShadowStaticDeparturesBackend(legacy, snapshot)
            now = datetime(2026, 1, 5, 7, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
            try:
                self.assertEqual(shadow.lines("fixture-israel", "S1"), legacy.lines("fixture-israel", "S1"))
                self.assertEqual(
                    shadow.external_departures_for(
                        "fixture-israel", "S1", 10, now, "Asia/Jerusalem", now_provider=lambda: now
                    ),
                    legacy.external_departures_for(
                        "fixture-israel", "S1", 10, now, "Asia/Jerusalem", now_provider=lambda: now
                    ),
                )
                self.assertEqual(
                    shadow.board("fixture-israel", "S1", 10, now, now),
                    legacy.board("fixture-israel", "S1", 10, now, now),
                )
                self.assertEqual(
                    shadow.trip_details("fixture-israel", "T1", str(stop_data), "2026-01-05"),
                    legacy.trip_details("fixture-israel", "T1", str(stop_data), "2026-01-05"),
                )
                for provider_query in (
                    lambda: shadow.provider_trip_registry(ISRAEL_PROVIDER_ID),
                    lambda: shadow.provider_realtime_registry(ISRAEL_PROVIDER_ID),
                    lambda: shadow.provider_route_type_registry(ISRAEL_PROVIDER_ID),
                    lambda: shadow.provider_route_metadata(ISRAEL_PROVIDER_ID),
                    lambda: shadow.provider_stop_registry(ISRAEL_PROVIDER_ID),
                    lambda: shadow.provider_trip_stop_registry(ISRAEL_PROVIDER_ID, {"israel:T1"}),
                ):
                    provider_query()
            finally:
                shadow.close()

    def test_shadow_proxy_returns_legacy_value_and_logs_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            legacy_path, release, _stop_data = self._build_fixture(Path(temporary))
            legacy = Database(str(legacy_path))
            snapshot = ReleaseSnapshot.open(release)
            shadow = ShadowStaticDeparturesBackend(legacy, snapshot)
            with self.assertLogs("haltewecker.static_departures_runtime", level="INFO") as logs:
                value = shadow.lines("fixture-israel", "S1")
            self.assertEqual(value, legacy.lines("fixture-israel", "S1"))
            self.assertTrue(any("query=lines status=MATCH" in entry for entry in logs.output))
            shadow.close()

    def test_provider_prefixed_stop_is_not_double_prefixed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _legacy_path, release, _stop_data = self._build_fixture(Path(temporary))
            now = datetime(2026, 1, 5, 7, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
            snapshot = ReleaseSnapshot.open(release)
            try:
                self.assertEqual(
                    snapshot.board("fixture-israel", "S1", 10, now, now),
                    snapshot.board("fixture-israel", "israel:S1", 10, now, now),
                )
            finally:
                snapshot.close()

    def test_invalid_release_references_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _legacy_path, release, _stop_data = self._build_fixture(Path(temporary))
            manifest_path = release / "release.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["providers"][ISRAEL_PROVIDER_ID]["structural"]["path"] = "missing.sqlite"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(RuntimeUnavailable):
                ReleaseSnapshot.open(release)

    def test_invalid_provider_manifest_and_corrupt_manifest_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _legacy_path, release, _stop_data = self._build_fixture(Path(temporary))
            provider_manifest = release / "providers" / ISRAEL_PROVIDER_ID / "structural" / "manifest.json"
            payload = json.loads(provider_manifest.read_text())
            payload["providerID"] = "other-provider"
            provider_manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(RuntimeUnavailable):
                ReleaseSnapshot.open(release)

    def test_common_release_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _legacy_path, release, _stop_data = self._build_fixture(Path(temporary))
            manifest_path = release / "release.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["releaseID"] = "other-release"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(RuntimeUnavailable):
                ReleaseSnapshot.open(release)

        with tempfile.TemporaryDirectory() as temporary:
            _legacy_path, release, _stop_data = self._build_fixture(Path(temporary))
            provider_manifest = release / "providers" / ISRAEL_PROVIDER_ID / "temporal" / "manifest.json"
            provider_manifest.write_text("{broken", encoding="utf-8")
            with self.assertRaises(RuntimeUnavailable):
                ReleaseSnapshot.open(release)

    def test_stale_temporal_window_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _legacy_path, release, _stop_data = self._build_fixture(Path(temporary))
            snapshot = ReleaseSnapshot.open(release)
            try:
                provider = snapshot.provider(ISRAEL_PROVIDER_ID)
                with self.assertRaises(RuntimeUnavailable):
                    provider.board(
                        "fixture-israel",
                        "S1",
                        10,
                        datetime(2026, 1, 6, tzinfo=ZoneInfo("Asia/Jerusalem")),
                        datetime(2026, 1, 6, tzinfo=ZoneInfo("Asia/Jerusalem")),
                    )
            finally:
                snapshot.close()

    def test_snapshot_generation_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _legacy_path, release_x, _stop_data = self._build_fixture(root)
            release_y = root / "release-y"
            shutil.copytree(release_x, release_y)
            common_y = release_y / "common.sqlite"
            connection = sqlite3.connect(common_y)
            try:
                connection.execute(
                    "UPDATE metadata SET value='release-y' WHERE key='releaseID'"
                )
                connection.execute(
                    "UPDATE provider_registry SET release_id='release-y'"
                )
                connection.commit()
            finally:
                connection.close()
            digest = hashlib.sha256(common_y.read_bytes()).hexdigest()
            manifest_path = release_y / "release.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["releaseID"] = "release-y"
            manifest["common"]["sha256"] = digest
            manifest["common"]["size"] = common_y.stat().st_size
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            snapshot_x = ReleaseSnapshot.open(release_x)
            snapshot_y = ReleaseSnapshot.open(release_y)
            try:
                self.assertEqual(snapshot_x.release_id, "release-x")
                self.assertEqual(snapshot_y.release_id, "release-y")
                self.assertEqual(snapshot_x.catalog.metadata()["releaseID"], "release-x")
                self.assertEqual(snapshot_y.catalog.metadata()["releaseID"], "release-y")
            finally:
                snapshot_x.close()
                snapshot_y.close()

    def test_comparison_is_order_and_duplicate_sensitive(self) -> None:
        self.assertEqual(compare_results([{"id": 1}, {"id": 1}], [{"id": 1}, {"id": 1}]).status, "MATCH")
        self.assertEqual(compare_results([1, 2], [2, 1]).status, "MISMATCH")
        self.assertEqual(compare_results([1], [1, 2]).legacy_count, 1)

    def test_shadow_flag_is_default_off(self) -> None:
        self.assertIsNone(shadow_backend_from_environment(object(), environ={}))

    def test_hybrid_flag_is_default_off(self) -> None:
        self.assertIsNone(hybrid_backend_from_environment(object(), environ={}))

    def test_hybrid_requires_an_atomic_release_pointer(self) -> None:
        with self.assertRaisesRegex(RuntimeUnavailable, HYBRID_RELEASE_POINTER_ENV):
            hybrid_backend_from_environment(object(), environ={HYBRID_ENV: "1"})

    def test_hybrid_factory_opens_real_release_manager_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_path, release, _stop_data = self._build_fixture(root)
            pointer = root / "current-release"
            pointer.symlink_to(release, target_is_directory=True)
            legacy = Database(str(legacy_path))
            backend = hybrid_backend_from_environment(
                legacy,
                environ={
                    HYBRID_ENV: "1",
                    "HALTEWECKER_STATIC_DEPARTURES_HYBRID_PROVIDERS": ISRAEL_PROVIDER_ID,
                    HYBRID_RELEASE_POINTER_ENV: str(pointer),
                },
            )
            self.assertIsInstance(backend, HybridStaticDeparturesBackend)
            self.assertEqual(backend.manager.active_release_id, "release-x")
            backend.close()

    def test_hybrid_routes_pilot_nonpilot_and_mixed_scopes(self) -> None:
        legacy = _HybridFakeLegacy()
        snapshot = _HybridFakeSnapshot(
            "release-a",
            {
                "israel": ("israel-mot",),
                "legacy-city": ("legacy-provider",),
                "mixed-city": ("israel-mot", "legacy-provider"),
                "toronto": ("ttc-surface", "ttc-subway"),
            },
            {
                ("israel", "stop"): ("israel-mot",),
                ("legacy-city", "stop"): ("legacy-provider",),
                ("mixed-city", "stop"): ("israel-mot", "legacy-provider"),
                ("toronto", "stop"): ("ttc-surface", "ttc-subway"),
            },
        )
        backend = HybridStaticDeparturesBackend(
            legacy,
            _HybridFakeManager(snapshot),
            ("israel-mot", "ttc-surface", "ttc-subway"),
        )
        try:
            self.assertEqual(backend.lines("israel", "stop")[0]["backend"], "shard")
            self.assertEqual(backend.lines("toronto", "stop")[0]["backend"], "shard")
            self.assertEqual(backend.lines("legacy-city", "stop")[0]["backend"], "legacy")
            self.assertEqual(backend.lines("mixed-city", "stop")[0]["backend"], "legacy")
            self.assertEqual(legacy.lines_calls, 2)
        finally:
            backend.close()

    def test_hybrid_shard_failure_is_fail_closed_without_legacy_fallback(self) -> None:
        legacy = _HybridFakeLegacy()
        snapshot = _HybridFakeSnapshot(
            "release-a",
            {"israel": ("israel-mot",)},
            {("israel", "stop"): ("israel-mot",)},
            fail_lines=True,
        )
        backend = HybridStaticDeparturesBackend(
            legacy,
            _HybridFakeManager(snapshot),
            ("israel-mot",),
        )
        try:
            with self.assertRaises(RuntimeUnavailable):
                backend.lines("israel", "stop")
            self.assertEqual(legacy.lines_calls, 0)
        finally:
            backend.close()

    def test_hybrid_mismatch_keeps_authoritative_shard_result(self) -> None:
        legacy = _HybridFakeLegacy()
        snapshot = _HybridFakeSnapshot(
            "release-a",
            {"israel": ("israel-mot",)},
            {("israel", "stop"): ("israel-mot",)},
        )
        backend = HybridStaticDeparturesBackend(
            legacy,
            _HybridFakeManager(snapshot),
            ("israel-mot",),
            compare_legacy=True,
        )
        try:
            with self.assertLogs("haltewecker.static_departures_runtime", level="INFO") as logs:
                value = backend.lines("israel", "stop")
            self.assertEqual(value[0]["backend"], "shard")
            self.assertTrue(any("status=MISMATCH" in entry for entry in logs.output))
        finally:
            backend.close()

    def test_hybrid_provider_registry_and_trip_use_shard(self) -> None:
        legacy = _HybridFakeLegacy()
        snapshot = _HybridFakeSnapshot(
            "release-a",
            {"israel": ("israel-mot",)},
            {},
        )
        backend = HybridStaticDeparturesBackend(
            legacy,
            _HybridFakeManager(snapshot),
            ("israel-mot",),
        )
        try:
            self.assertEqual(backend.provider_trip_registry("israel-mot")[0], {"israel-mot:trip"})
            self.assertEqual(
                backend.trip_details("israel", "israel-mot:trip", "/tmp"),
                {"backend": "shard", "provider": "israel-mot", "tripID": "israel-mot:trip"},
            )
        finally:
            backend.close()

    def test_hybrid_release_switch_changes_authoritative_snapshot(self) -> None:
        legacy = _HybridFakeLegacy()
        manager = _HybridFakeManager(
            _HybridFakeSnapshot(
                "release-a",
                {"israel": ("israel-mot",)},
                {("israel", "stop"): ("israel-mot",)},
            )
        )
        backend = HybridStaticDeparturesBackend(legacy, manager, ("israel-mot",))
        try:
            self.assertEqual(backend.lines("israel", "stop")[0]["backend"], "shard")
            manager.switch(
                _HybridFakeSnapshot(
                    "release-b",
                    {"israel": ("israel-mot",)},
                    {("israel", "stop"): ("israel-mot",)},
                )
            )
            with self.assertLogs("haltewecker.static_departures_runtime", level="INFO") as logs:
                backend.lines("israel", "stop")
            self.assertTrue(any("release_id=release-b" in entry for entry in logs.output))
        finally:
            backend.close()

    def test_hybrid_israel_query_families_match_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_path, release, stop_data = self._build_fixture(root)
            pointer = root / "current-release"
            pointer.symlink_to(release, target_is_directory=True)
            legacy = Database(str(legacy_path))
            manager = ReleaseManager(pointer, provider_ids=(ISRAEL_PROVIDER_ID,))
            backend = HybridStaticDeparturesBackend(
                legacy,
                manager,
                (ISRAEL_PROVIDER_ID,),
                compare_legacy=True,
            )
            now = datetime(2026, 1, 5, 7, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
            try:
                self.assertEqual(backend.lines("fixture-israel", "S1"), legacy.lines("fixture-israel", "S1"))
                self.assertEqual(
                    backend.external_departures_for(
                        "fixture-israel", "S1", 10, now, "Asia/Jerusalem", now_provider=lambda: now
                    ),
                    legacy.external_departures_for(
                        "fixture-israel", "S1", 10, now, "Asia/Jerusalem", now_provider=lambda: now
                    ),
                )
                self.assertEqual(
                    backend.board("fixture-israel", "S1", 10, now, now),
                    legacy.board("fixture-israel", "S1", 10, now, now),
                )
                self.assertEqual(
                    backend.trip_details("fixture-israel", "T1", str(stop_data), "2026-01-05"),
                    legacy.trip_details("fixture-israel", "T1", str(stop_data), "2026-01-05"),
                )
            finally:
                backend.close()


if __name__ == "__main__":
    unittest.main()
