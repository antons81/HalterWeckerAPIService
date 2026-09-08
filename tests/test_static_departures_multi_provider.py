import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TESTS_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = TESTS_ROOT.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "services"))
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
sys.path.insert(0, str(TESTS_ROOT))

from common_catalog import build_common_catalog  # noqa: E402
from static_departures_api import Database  # noqa: E402
from static_departures_runtime import (  # noqa: E402
    ISRAEL_PROVIDER_ID,
    ReleaseSnapshot,
    RuntimeUnavailable,
    ShadowStaticDeparturesBackend,
)
import test_static_departures_runtime as runtime_tests  # noqa: E402


class StaticDeparturesMultiProviderTests(unittest.TestCase):
    def _build_fixture(self, root: Path, provider_count: int = 2) -> tuple[Path, Path]:
        helper = runtime_tests.StaticDeparturesRuntimeTests()
        legacy_path, release, _stop_data = helper._build_fixture(root)
        source = release / "providers" / ISRAEL_PROVIDER_ID
        provider_ids = [ISRAEL_PROVIDER_ID, *[f"synthetic-{index}" for index in range(2, provider_count + 1)]]
        for provider_id in provider_ids[1:]:
            destination = release / "providers" / provider_id
            shutil.copytree(source, destination)
            for artifact_type in ("structural", "temporal"):
                manifest_path = destination / artifact_type / "manifest.json"
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                payload["providerID"] = provider_id
                manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        def artifact_info(provider_id: str, artifact_type: str) -> tuple[dict, dict]:
            directory = release / "providers" / provider_id / artifact_type
            database = directory / "provider.sqlite"
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            return manifest, {
                "path": str(database.relative_to(release)),
                "manifestPath": str((directory / "manifest.json").relative_to(release)),
                "artifactKey": manifest["artifactKey"],
                "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                "size": database.stat().st_size,
            }

        provider_inputs = {}
        provider_refs = {}
        for order, provider_id in enumerate(provider_ids):
            structural, structural_ref = artifact_info(provider_id, "structural")
            temporal, temporal_ref = artifact_info(provider_id, "temporal")
            provider_inputs[provider_id] = {
                "providerOrder": order,
                "releaseID": "release-x",
                "structural": {
                    "artifactKey": structural["artifactKey"],
                    "schemaVersion": structural["structuralSchemaVersion"],
                },
                "temporal": {
                    "artifactKey": temporal["artifactKey"],
                    "schemaVersion": temporal["temporalSchemaVersion"],
                },
                "validFrom": temporal["dependencies"]["validFrom"],
                "validThrough": temporal["dependencies"]["validThrough"],
                "mergeGroup": "synthetic-merged",
            }
            provider_refs[provider_id] = {"structural": structural_ref, "temporal": temporal_ref}

        common_build = build_common_catalog(
            release / "common.sqlite",
            release_id="release-x",
            providers=provider_inputs,
            aliases=(("fixture-alias", "fixture-israel"),),
            city_stops=(("fixture-israel", "S1"),),
            provider_city_stops=(
                *( (provider_id, "fixture-israel", "S1") for provider_id in provider_ids ),
            ),
            provider_modes=[
                {"providerID": provider_id, "cityID": "fixture-israel", "mode": "canonical", "timezone": "Asia/Jerusalem", "stopIDPrefix": "israel:", "identifierPrefix": "israel:"}
                for provider_id in provider_ids
            ],
        )
        self.last_common_build = common_build
        release_manifest = json.loads((release / "release.json").read_text(encoding="utf-8"))
        release_manifest["common"] = {
            "path": "common.sqlite",
            "sha256": hashlib.sha256(common_build.database_path.read_bytes()).hexdigest(),
            "size": common_build.size,
        }
        release_manifest["providers"] = provider_refs
        (release / "release.json").write_text(json.dumps(release_manifest, indent=2), encoding="utf-8")
        return legacy_path, release

    def test_common_catalog_has_only_routing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _legacy, release = self._build_fixture(Path(temporary))
            connection = sqlite3.connect(release / "common.sqlite")
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                self.assertNotIn("raw_stops", tables)
                self.assertNotIn("stop_times", tables)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM provider_registry").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM provider_city_modes").fetchone()[0], 2)
            finally:
                connection.close()

    def test_multi_provider_lines_board_and_departures_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            legacy_path, release = self._build_fixture(Path(temporary))
            legacy = Database(str(legacy_path))
            snapshot = ReleaseSnapshot.open(
                release,
                provider_ids=(ISRAEL_PROVIDER_ID, "synthetic-2"),
                max_provider_connections=2,
                max_parallel_provider_queries=2,
            )
            now = datetime(2026, 1, 5, 7, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
            try:
                base_lines = legacy.lines("fixture-israel", "S1")
                self.assertEqual(snapshot.lines("fixture-israel", "S1"), base_lines * 2)
                base_board = legacy.board("fixture-israel", "S1", 10, now, now)
                merged_board = snapshot.board("fixture-israel", "S1", 3, now, now)
                self.assertEqual(merged_board, [row for item in base_board for row in (item, item)][:3])
                base_departures = legacy.external_departures_for(
                    "fixture-israel", "S1", 10, now, "Asia/Jerusalem", now_provider=lambda: now
                )
                merged_departures = snapshot.external_departures_for(
                    "fixture-israel", "S1", 3, now, "Asia/Jerusalem", now_provider=lambda: now
                )
                self.assertEqual(merged_departures, [row for item in base_departures for row in (item, item)][:3])
                self.assertEqual(snapshot.catalog.providers_for_stop("fixture-israel", "S1"), (ISRAEL_PROVIDER_ID, "synthetic-2"))
                self.assertEqual(snapshot.last_fanout_metrics.provider_count, 2)
                self.assertEqual(snapshot.last_fanout_metrics.rows_fetched, len(base_departures) * 2)
            finally:
                snapshot.close()
                legacy.close()

    def test_aliases_and_provider_modes_are_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _legacy, release = self._build_fixture(Path(temporary))
            snapshot = ReleaseSnapshot.open(release, provider_ids=(ISRAEL_PROVIDER_ID, "synthetic-2"))
            try:
                self.assertTrue(snapshot.city_has_stop("fixture-alias", "S1"))
                self.assertEqual([mode.provider_id for mode in snapshot.provider_modes("fixture-alias")], [ISRAEL_PROVIDER_ID, "synthetic-2"])
                contexts = snapshot.provider_contexts("fixture-alias")
                self.assertEqual([context["providerID"] for context in contexts], [ISRAEL_PROVIDER_ID, "synthetic-2"])
                self.assertIn((ISRAEL_PROVIDER_ID, "israel:T1"), contexts[0]["trips"])
            finally:
                snapshot.close()

    def test_required_provider_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _legacy, release = self._build_fixture(Path(temporary))
            manifest_path = release / "release.json"
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["providers"].pop("synthetic-2")
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(RuntimeUnavailable):
                ReleaseSnapshot.open(release, provider_ids=(ISRAEL_PROVIDER_ID, "synthetic-2"))

    def test_shadow_keeps_legacy_authoritative_and_logs_multi_provider_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            legacy_path, release = self._build_fixture(Path(temporary))
            legacy = Database(str(legacy_path))
            snapshot = ReleaseSnapshot.open(release, provider_ids=(ISRAEL_PROVIDER_ID, "synthetic-2"))
            shadow = ShadowStaticDeparturesBackend(legacy, snapshot, provider_id=ISRAEL_PROVIDER_ID)
            try:
                with self.assertLogs("haltewecker.static_departures_runtime", level="INFO") as logs:
                    value = shadow.lines("fixture-israel", "S1")
                self.assertEqual(value, legacy.lines("fixture-israel", "S1"))
                self.assertTrue(any("city=fixture-israel" in entry and "providers=israel-mot,synthetic-2" in entry and "status=MISMATCH" in entry for entry in logs.output))
            finally:
                shadow.close()

    def test_common_a_with_provider_shard_b_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").mkdir()
            (root / "b").mkdir()
            _legacy_a, release_a = self._build_fixture(root / "a")
            _legacy_b, release_b = self._build_fixture(root / "b")
            provider_b = release_b / "providers" / "synthetic-2"
            structural_manifest_path = provider_b / "structural" / "manifest.json"
            structural_manifest = json.loads(structural_manifest_path.read_text(encoding="utf-8"))
            structural_manifest["artifactKey"] = "generation-b"
            structural_manifest_path.write_text(json.dumps(structural_manifest, indent=2), encoding="utf-8")
            temporal_manifest_path = provider_b / "temporal" / "manifest.json"
            temporal_manifest = json.loads(temporal_manifest_path.read_text(encoding="utf-8"))
            temporal_manifest["structuralArtifactKey"] = "generation-b"
            temporal_manifest_path.write_text(json.dumps(temporal_manifest, indent=2), encoding="utf-8")
            shutil.rmtree(release_a / "providers" / "synthetic-2")
            shutil.copytree(provider_b, release_a / "providers" / "synthetic-2")
            release_manifest_path = release_a / "release.json"
            release_manifest = json.loads(release_manifest_path.read_text(encoding="utf-8"))
            release_manifest["providers"]["synthetic-2"]["structural"]["artifactKey"] = "generation-b"
            release_manifest_path.write_text(json.dumps(release_manifest, indent=2), encoding="utf-8")
            with self.assertRaises(RuntimeUnavailable):
                ReleaseSnapshot.open(release_a, provider_ids=(ISRAEL_PROVIDER_ID, "synthetic-2"))


if __name__ == "__main__":
    unittest.main()
