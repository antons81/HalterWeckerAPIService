import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from release_state import ResumeError
from release_state import detect_activation_state
from release_state import inspect_resume
from release_state import write_state


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ReleaseStateTests(unittest.TestCase):
    release_id = "candidate-1"
    build_fingerprint = "test-fingerprint"

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.releases_root = self.root / "releases"
        self.releases_root.mkdir()
        self.candidate = self.releases_root / self.release_id
        self._write_candidate()
        self.current = self.root / "current"
        self.current_release = self.root / "current-release"
        self.static_departures_release = self.root / "static-departures-release"
        self.departures_current = self.root / "departures-current.sqlite"
        self.previous = self.root / "previous" / "stop-data"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write_candidate(self) -> None:
        stop_data = self.candidate / "stop-data"
        for relative in (
            "stops",
            "routes",
            "departures",
            "trips",
            "transit",
            "radar",
            "swiss-static",
            "provenance",
        ):
            (stop_data / relative).mkdir(parents=True, exist_ok=True)

        artifact_path = self.candidate / "germany.zip"
        artifact_path.write_bytes(b"germany")
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        artifact = {
            "path": str(artifact_path),
            "sha256": digest,
            "size": artifact_path.stat().st_size,
        }
        manifest = {
            "version": "test-version",
            "releaseID": self.release_id,
            "cities": [{"id": "test-city", "url": "stops/test-city.json"}],
            "sourceArtifacts": {"germany": {"sha256": digest, "size": artifact["size"]}},
            "inputProvenance": {},
        }
        (stop_data / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (stop_data / "stops" / "test-city.json").write_text("{}", encoding="utf-8")
        (stop_data / "transit-radar-cities.json").write_text("{}", encoding="utf-8")
        (stop_data / "swiss-static" / "manifest.json").write_text("{}", encoding="utf-8")
        (stop_data / "provenance" / "input-artifacts.json").write_text("{}", encoding="utf-8")
        (self.candidate / "gtfs-artifacts.json").write_text(
            json.dumps({"sources": {"germany": artifact}, "external": {}}),
            encoding="utf-8",
        )
        custom_sources = {}
        for source_id in ("vbb", "rnv"):
            custom_artifact_path = self.candidate / f"{source_id}.zip"
            custom_artifact_path.write_bytes(source_id.encode("utf-8"))
            custom_digest = hashlib.sha256(custom_artifact_path.read_bytes()).hexdigest()
            custom_sources[source_id] = {
                "sourceID": source_id,
                "path": str(custom_artifact_path),
                "sha256": custom_digest,
                "size": custom_artifact_path.stat().st_size,
            }
        (self.candidate / "custom-gtfs-artifacts.json").write_text(
            json.dumps({"sources": custom_sources}),
            encoding="utf-8",
        )
        (self.candidate / "release-metadata.json").write_text(
            json.dumps(
                {
                    "releaseID": self.release_id,
                    "buildFingerprint": self.build_fingerprint,
                    "stopManifestVersion": "test-version",
                    "sourceArtifacts": manifest["sourceArtifacts"],
                    "inputProvenance": {},
                }
            ),
            encoding="utf-8",
        )
        connection = sqlite3.connect(self.candidate / "departures.sqlite")
        connection.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [
                ("releaseID", self.release_id),
                ("stopDataReleaseID", self.release_id),
                ("stopDataManifestVersion", "test-version"),
            ],
        )
        connection.commit()
        connection.close()

    def _link(self, path: Path, target: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(os.path.relpath(target, path.parent))

    def _write_state(self, stage: str) -> None:
        write_state(
            self.candidate,
            release_id=self.release_id,
            completed_stage=stage,
            build_fingerprint=self.build_fingerprint,
        )

    def _inspect(self, *, current_fingerprint: str | None = None):
        return inspect_resume(
            releases_root=self.releases_root,
            release_id=self.release_id,
            repository_root=REPOSITORY_ROOT,
            current_fingerprint=current_fingerprint or self.build_fingerprint,
            current=self.current,
            current_release=self.current_release,
            static_departures_release=self.static_departures_release,
            departures_current=self.departures_current,
            previous=self.previous,
        )

    def _write_old_primary_pointers(self) -> None:
        old = self.releases_root / "old"
        (old / "stop-data").mkdir(parents=True)
        (old / "stop-data" / "release-marker").write_text("old", encoding="utf-8")
        (old / "departures.sqlite").write_bytes(b"old")
        self._link(self.current_release, old)
        self._link(self.current, old / "stop-data")
        self._link(self.departures_current, old / "departures.sqlite")

    def test_crash_point_a_missing_state_is_refused(self) -> None:
        with self.assertRaisesRegex(ResumeError, "release state is missing"):
            self._inspect()

    def test_crash_point_b_incomplete_build_is_refused(self) -> None:
        self._write_state("build")
        (self.candidate / "stop-data" / "routes").rmdir()

        with self.assertRaisesRegex(ResumeError, "required stop-data directory"):
            self._inspect()

    def test_crash_point_c_intact_build_resumes_at_candidate_validation(self) -> None:
        self._write_state("build")

        info = self._inspect()

        self.assertEqual(info.status, "candidate-not-activated")
        self.assertEqual(info.completed_stage, "build")
        self.assertEqual(info.next_stage, "candidate-validation")

    def test_missing_previous_with_valid_current_allows_resume(self) -> None:
        self._write_state("build")
        self._write_old_primary_pointers()

        info = self._inspect()

        self.assertEqual(info.status, "candidate-not-activated")
        self.assertEqual(info.next_stage, "candidate-validation")

    def test_dangling_previous_for_deleted_release_allows_resume(self) -> None:
        self._write_state("build")
        self._write_old_primary_pointers()
        self.previous.parent.mkdir(parents=True, exist_ok=True)
        self.previous.symlink_to(Path("../releases/deleted-release/stop-data"))

        info = self._inspect()

        self.assertEqual(info.status, "candidate-not-activated")
        self.assertEqual(info.next_stage, "candidate-validation")

    def test_dangling_previous_with_existing_release_but_missing_target_fails_closed(
        self,
    ) -> None:
        self._write_state("build")
        self._write_old_primary_pointers()
        malformed_release = self.releases_root / "malformed-release"
        malformed_release.mkdir()
        self.previous.parent.mkdir(parents=True, exist_ok=True)
        self.previous.symlink_to(Path("../releases/malformed-release/stop-data"))

        with self.assertRaisesRegex(ResumeError, "invalid or unsafe canonical pointer"):
            self._inspect()

    def test_malformed_previous_pointer_fails_closed(self) -> None:
        self._write_state("build")
        self._write_old_primary_pointers()
        outside = self.root / "outside"
        outside.mkdir()
        self.previous.parent.mkdir(parents=True, exist_ok=True)
        self.previous.symlink_to(outside / "stop-data")

        with self.assertRaisesRegex(ResumeError, "invalid or unsafe canonical pointer"):
            self._inspect()

    def test_valid_previous_release_remains_usable(self) -> None:
        self._write_state("build")
        self._write_old_primary_pointers()
        self._link(self.previous, self.releases_root / "old" / "stop-data")

        info = self._inspect()

        self.assertEqual(info.status, "candidate-not-activated")
        self.assertEqual(info.next_stage, "candidate-validation")

    def test_invalid_current_pointer_fails_closed_without_previous(self) -> None:
        self._write_state("build")
        self._write_old_primary_pointers()
        self.current_release.unlink()
        self._link(self.current_release, self.releases_root / "missing")

        with self.assertRaisesRegex(ResumeError, "invalid or unsafe canonical pointer"):
            self._inspect()

    def test_crash_point_d_provenance_mismatch_is_refused(self) -> None:
        self._write_state("candidate-validation")
        metadata_path = self.candidate / "release-metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["buildFingerprint"] = "stale-fingerprint"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        with self.assertRaisesRegex(ResumeError, "candidate integrity"):
            self._inspect()

    def test_crash_point_e_static_database_mismatch_is_refused(self) -> None:
        self._write_state("static-departures")
        database = sqlite3.connect(self.candidate / "departures.sqlite")
        database.execute(
            "UPDATE metadata SET value = ? WHERE key = ?",
            ("wrong-release", "releaseID"),
        )
        database.commit()
        database.close()

        with self.assertRaisesRegex(ResumeError, "candidate integrity"):
            self._inspect()

    def test_candidate_validation_manifest_mutation_is_refused(self) -> None:
        self._write_state("candidate-validation")
        manifest_path = self.candidate / "stop-data" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["cities"][0]["name"] = "Changed after validation"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ResumeError, "candidate integrity"):
            self._inspect()

    def test_candidate_package_corruption_is_refused(self) -> None:
        self._write_state("candidate-validation")
        package_path = self.candidate / "stop-data" / "stops" / "test-city.json"
        package_path.write_text('{"corrupted":true}', encoding="utf-8")

        with self.assertRaisesRegex(ResumeError, "candidate integrity"):
            self._inspect()

    def test_candidate_fifo_is_refused_without_reading_it(self) -> None:
        self._write_state("candidate-validation")
        fifo_path = self.candidate / "stop-data" / "routes" / "unsupported.fifo"
        os.mkfifo(fifo_path)

        with self.assertRaisesRegex(ResumeError, r"unsupported filesystem entry.*FIFO"):
            self._inspect()

    def test_candidate_symlink_is_refused(self) -> None:
        self._write_state("candidate-validation")
        symlink_path = self.candidate / "stop-data" / "routes" / "unsupported.json"
        symlink_path.symlink_to(self.candidate / "stop-data" / "stops" / "test-city.json")

        with self.assertRaisesRegex(ResumeError, r"unsupported filesystem entry.*symlink"):
            self._inspect()

    def test_current_fingerprint_mismatch_is_refused(self) -> None:
        self._write_state("candidate-validation")

        with self.assertRaisesRegex(ResumeError, "build fingerprint"):
            self._inspect(current_fingerprint="different-fingerprint")

    def test_crash_point_f_fully_active_release_is_idempotent(self) -> None:
        self._write_state("handoff-readiness")
        self._link(self.current_release, self.candidate)
        self._link(self.current, self.candidate / "stop-data")
        self._link(self.static_departures_release, self.candidate)
        self._link(self.departures_current, self.candidate / "departures.sqlite")

        info = self._inspect()

        self.assertEqual(info.status, "already-active")
        self.assertEqual(info.next_stage, "already-active")

    def test_crash_point_g_partial_activation_is_refused(self) -> None:
        self._write_state("handoff-readiness")
        self._link(self.current_release, self.candidate)

        with self.assertRaisesRegex(ResumeError, "partial activation"):
            self._inspect()

    def test_crash_point_h_inconsistent_old_pointers_are_refused(self) -> None:
        self._write_state("handoff-readiness")
        self._write_old_primary_pointers()
        other = self.releases_root / "other"
        (other / "stop-data").mkdir(parents=True)
        (other / "departures.sqlite").write_bytes(b"other")
        self.departures_current.unlink()
        self._link(self.departures_current, other / "departures.sqlite")

        with self.assertRaisesRegex(ResumeError, "canonical pointers are inconsistent"):
            self._inspect()

    def test_crash_point_i_previous_candidate_is_refused(self) -> None:
        self._write_state("handoff-readiness")
        self._link(self.previous, self.candidate / "stop-data")

        with self.assertRaisesRegex(ResumeError, "previous pointer targets candidate"):
            self._inspect()

    def test_write_state_replaces_previous_state_atomically(self) -> None:
        self._write_state("build")
        first = json.loads((self.candidate / "release-state.json").read_text())
        self._write_state("candidate-validation")
        second = json.loads((self.candidate / "release-state.json").read_text())

        self.assertEqual(first["completedStage"], "build")
        self.assertEqual(second["completedStage"], "candidate-validation")
        self.assertFalse(list(self.candidate.glob(".release-state-*.tmp")))


if __name__ == "__main__":
    unittest.main()
