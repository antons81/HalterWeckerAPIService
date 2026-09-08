import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
sys.path.insert(0, str(REPOSITORY_ROOT / "services"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tests"))

from provider_release_assembler import (  # noqa: E402
    ReleaseAssemblyError,
    _provider_set_fingerprint,
    assemble_release,
    atomic_switch_current_release,
    readiness_probe,
    validate_candidate_release,
)
from static_departures_runtime import ReleaseManager  # noqa: E402
from test_static_departures_multi_provider import (  # noqa: E402
    StaticDeparturesMultiProviderTests,
)


class ProviderReleaseAssemblerTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, dict[str, dict[str, object]]]:
        helper = StaticDeparturesMultiProviderTests()
        root.mkdir(parents=True, exist_ok=True)
        legacy, release = helper._build_fixture(root, provider_count=2)
        providers = {
            "israel-mot": {
                "cities": ["fixture-israel"],
                "providerOrder": 0,
                "mergeGroup": "fixture",
                "structural": release / "providers" / "israel-mot" / "structural",
                "temporal": release / "providers" / "israel-mot" / "temporal",
            },
            "synthetic-2": {
                "cities": ["fixture-israel"],
                "providerOrder": 1,
                "mergeGroup": "fixture",
                "structural": release / "providers" / "synthetic-2" / "structural",
                "temporal": release / "providers" / "synthetic-2" / "temporal",
            },
        }
        return legacy, release / "common.sqlite", release / "stop-data", providers

    @staticmethod
    def _common_for_release(source: Path, destination: Path, release_id: str) -> Path:
        shutil.copy2(source, destination)
        with sqlite3.connect(destination) as connection:
            connection.execute("UPDATE metadata SET value=? WHERE key='releaseID'", (release_id,))
            connection.execute("UPDATE provider_registry SET release_id=?", (release_id,))
            connection.commit()
        return destination

    def test_assembly_validates_and_reuses_immutable_provider_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _legacy, common, stop_data, providers = self._fixture(root / "fixture")
            common_copy = self._common_for_release(common, root / "common-a.sqlite", "release-a")
            assembly = assemble_release(
                root / "releases",
                "release-a",
                common_database=common_copy,
                stop_data_root=stop_data,
                providers=providers,
            )
            payload = validate_candidate_release(assembly.release_directory)
            self.assertEqual(payload["releaseID"], "release-a")
            self.assertEqual(assembly.reused_artifacts, 4)
            self.assertTrue(
                (assembly.release_directory / "providers/israel-mot/structural/provider.sqlite").is_symlink()
            )
            self.assertEqual(
                payload["compatibility"]["runtimeSchemaVersion"],
                1,
            )

    def test_atomic_switch_and_request_leases_keep_generations_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _legacy, common, stop_data, providers = self._fixture(root / "fixture")
            common_a = self._common_for_release(common, root / "common-a.sqlite", "release-a")
            common_b = self._common_for_release(common, root / "common-b.sqlite", "release-b")
            releases = root / "releases"
            release_a = assemble_release(
                releases,
                "release-a",
                common_database=common_a,
                stop_data_root=stop_data,
                providers=providers,
            )
            release_b = assemble_release(
                releases,
                "release-b",
                common_database=common_b,
                stop_data_root=stop_data,
                providers=providers,
            )
            pointer = root / "current-release"
            atomic_switch_current_release(pointer, release_a.release_directory)
            manager = ReleaseManager(
                pointer,
                provider_ids=("israel-mot", "synthetic-2"),
                max_provider_connections=2,
            )
            lease_a = manager.acquire_snapshot()
            self.assertEqual(lease_a.release_id, "release-a")

            atomic_switch_current_release(pointer, release_b.release_directory)
            lease_b = manager.acquire_snapshot()
            self.assertEqual(lease_b.release_id, "release-b")
            self.assertEqual(lease_a.release_id, "release-a")
            self.assertFalse(lease_a.snapshot._closed)

            lease_b.release()
            self.assertFalse(lease_a.snapshot._closed)
            lease_a.release()
            manager.drain_old_snapshots()
            self.assertTrue(lease_a._generation.snapshot._closed)

            atomic_switch_current_release(pointer, release_a.release_directory)
            rollback = manager.acquire_snapshot()
            self.assertEqual(rollback.release_id, "release-a")
            rollback.release()
            manager.close()

    def test_broken_candidate_is_rejected_before_pointer_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _legacy, common, stop_data, providers = self._fixture(root / "fixture")
            common_a = self._common_for_release(common, root / "common-a.sqlite", "release-a")
            common_b = self._common_for_release(common, root / "common-b.sqlite", "release-b")
            releases = root / "releases"
            release_a = assemble_release(
                releases,
                "release-a",
                common_database=common_a,
                stop_data_root=stop_data,
                providers=providers,
            )
            release_b = assemble_release(
                releases,
                "release-b",
                common_database=common_b,
                stop_data_root=stop_data,
                providers=providers,
            )
            pointer = root / "current-release"
            atomic_switch_current_release(pointer, release_a.release_directory)
            manifest_path = release_b.release_directory / "release.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["providers"].pop("synthetic-2")
            manifest["providerSetFingerprint"] = _provider_set_fingerprint(manifest["providers"])
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ReleaseAssemblyError):
                atomic_switch_current_release(pointer, release_b.release_directory)
            self.assertEqual(pointer.resolve(), release_a.release_directory.resolve())

    def test_readiness_probe_uses_israel_toronto_and_trip_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _legacy, common, stop_data, providers = self._fixture(root / "fixture")
            common_copy = self._common_for_release(common, root / "common-a.sqlite", "release-a")
            assembly = assemble_release(
                root / "releases",
                "release-a",
                common_database=common_copy,
                stop_data_root=stop_data,
                providers=providers,
            )
            report = readiness_probe(
                assembly.release_directory,
                provider_ids=("israel-mot", "synthetic-2"),
                israel_case={
                    "providerID": "israel-mot",
                    "cityID": "fixture-israel",
                    "stopID": "S1",
                    "fromDate": "2026-01-05T07:00:00+02:00",
                    "toDate": "2026-01-05T08:00:00+02:00",
                },
                toronto_case={
                    "cityID": "fixture-israel",
                    "stopID": "S1",
                    "limit": 3,
                    "fromDate": "2026-01-05T07:00:00+02:00",
                    "toDate": "2026-01-05T08:00:00+02:00",
                },
                trip_case={
                    "providerID": "israel-mot",
                    "cityID": "fixture-israel",
                    "tripID": "israel:T1",
                    "serviceDate": "2026-01-05",
                },
            )
            self.assertEqual(report["status"], "READY")
            self.assertEqual(report["releaseID"], "release-a")
            self.assertEqual(report["toronto"]["providerIDs"], ("israel-mot", "synthetic-2"))
            self.assertTrue(report["tripDetails"]["tripID"])

    def test_concurrent_readers_never_observe_mixed_generations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _legacy, common, stop_data, providers = self._fixture(root / "fixture")
            releases = root / "releases"
            release_a = assemble_release(
                releases,
                "release-a",
                common_database=self._common_for_release(common, root / "common-a.sqlite", "release-a"),
                stop_data_root=stop_data,
                providers=providers,
            )
            release_b = assemble_release(
                releases,
                "release-b",
                common_database=self._common_for_release(common, root / "common-b.sqlite", "release-b"),
                stop_data_root=stop_data,
                providers=providers,
            )
            pointer = root / "current-release"
            atomic_switch_current_release(pointer, release_a.release_directory)
            manager = ReleaseManager(
                pointer,
                provider_ids=("israel-mot", "synthetic-2"),
                max_provider_connections=2,
            )
            barrier = threading.Barrier(5)
            errors: list[BaseException] = []

            def reader() -> None:
                try:
                    barrier.wait()
                    for _ in range(30):
                        with manager.acquire_snapshot() as lease:
                            release_id = lease.release_id
                            metadata_release_id = lease.snapshot.catalog.metadata()["releaseID"]
                            if release_id != metadata_release_id:
                                raise AssertionError((release_id, metadata_release_id))
                            lease.snapshot.lines("fixture-israel", "S1")
                        time.sleep(0.001)
                except BaseException as error:
                    errors.append(error)

            def switcher() -> None:
                try:
                    barrier.wait()
                    for index in range(30):
                        target = release_b if index % 2 else release_a
                        atomic_switch_current_release(pointer, target.release_directory)
                        manager.reload_if_changed()
                except BaseException as error:
                    errors.append(error)

            threads = [threading.Thread(target=reader) for _ in range(4)]
            threads.append(threading.Thread(target=switcher))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            manager.close()
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
