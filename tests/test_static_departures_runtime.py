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
    ISRAEL_PROVIDER_ID,
    ReleaseSnapshot,
    RuntimeUnavailable,
    ShadowStaticDeparturesBackend,
    compare_results,
    shadow_backend_from_environment,
)
import test_static_provider_artifact as static_provider_tests  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
