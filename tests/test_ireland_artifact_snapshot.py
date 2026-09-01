import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ireland_artifact_snapshot
from artifact_provenance import artifact_provenance
from gtfs_source_cache import ArtifactResult
from ireland_artifact_snapshot import (
    IRELAND_SNAPSHOT_RELATIVE_PATH,
    capture_ireland_snapshot,
    validate_ireland_release_snapshot,
)
from prepare_gtfs_artifacts import artifact_payload
from release_state import ResumeError, inspect_resume, write_state
from static_departures_scoped import StaticProvider, resolve_external_artifact


class IrelandArtifactSnapshotTests(unittest.TestCase):
    def _fd_count(self) -> int:
        for candidate in (Path("/dev/fd"), Path("/proc/self/fd")):
            if not candidate.is_dir():
                continue
            try:
                return sum(1 for _ in candidate.iterdir())
            except OSError:
                continue
        self.skipTest("no portable local process FD directory available")

    def _source(self, root: Path, content: str = "A") -> Path:
        source = root / "shared" / "static"
        source.mkdir(parents=True)
        (source / "stops.txt").write_text(content, encoding="utf-8")
        (source / "nested").mkdir()
        (source / "nested" / "routes.txt").write_text(content.lower(), encoding="utf-8")
        return source

    def test_snapshot_survives_shared_source_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root, "A")
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)

            snapshot = capture_ireland_snapshot(source, release)
            before = artifact_provenance(snapshot.path)
            (source / "stops.txt").write_text("B", encoding="utf-8")
            os.replace(source, root / "shared" / "old-static")
            replacement = root / "shared" / "static"
            replacement.mkdir()
            (replacement / "stops.txt").write_text("B", encoding="utf-8")
            (replacement / "nested").mkdir()
            (replacement / "nested" / "routes.txt").write_text("b", encoding="utf-8")

            self.assertEqual(artifact_provenance(snapshot.path), before)
            self.assertEqual(snapshot.path, release / IRELAND_SNAPSHOT_RELATIVE_PATH)

    def test_manifest_payload_points_to_release_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)

            payload = artifact_payload(
                ArtifactResult("ireland", source, "local"),
                release_root=release,
            )

            self.assertEqual(
                Path(payload["path"]), release / IRELAND_SNAPSHOT_RELATIVE_PATH
            )
            self.assertEqual(
                (payload["sha256"], payload["size"]),
                artifact_provenance(Path(payload["path"])),
            )
            self.assertEqual(
                set(payload),
                {"path", "status", "sha256", "size"},
            )

    def test_scoped_resolver_uses_release_snapshot_for_ireland(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root, "A")
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            provider = StaticProvider(
                "ireland",
                "IE",
                "external",
                {"id": "ireland", "localPath": str(source)},
            )

            snapshot_path = resolve_external_artifact(
                provider,
                {},
                release_root=release,
            )
            (source / "stops.txt").write_text("B", encoding="utf-8")

            self.assertEqual((snapshot_path / "stops.txt").read_text(), "A")

    def test_mutation_during_copy_retries_without_mixed_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root, "A")
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            original_copy = ireland_artifact_snapshot._copy_regular_file_at
            mutated = False

            def copy_and_publish_new_generation(source_fd, name, destination_fd):
                nonlocal mutated
                original_copy(source_fd, name, destination_fd)
                if not mutated:
                    mutated = True
                    (source / "stops.txt").write_text("B", encoding="utf-8")
                    (source / "nested" / "routes.txt").write_text("b", encoding="utf-8")

            with mock.patch(
                "ireland_artifact_snapshot._copy_regular_file_at",
                side_effect=copy_and_publish_new_generation,
            ):
                snapshot = capture_ireland_snapshot(source, release)

            self.assertTrue(mutated)
            self.assertEqual((snapshot.path / "stops.txt").read_text(), "B")
            self.assertEqual((snapshot.path / "nested" / "routes.txt").read_text(), "b")

    def test_repeated_mutation_fails_closed_and_publishes_no_partial_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root, "A")
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            original_copy = ireland_artifact_snapshot._copy_regular_file_at
            counter = 0

            def copy_and_mutate(source_fd, name, destination_fd):
                nonlocal counter
                original_copy(source_fd, name, destination_fd)
                counter += 1
                (source / "stops.txt").write_text(f"{counter}", encoding="utf-8")

            with (
                mock.patch(
                    "ireland_artifact_snapshot._copy_regular_file_at",
                    side_effect=copy_and_mutate,
                ),
                self.assertRaisesRegex(ValueError, "changed during snapshot capture"),
            ):
                capture_ireland_snapshot(source, release, max_attempts=2)

            self.assertFalse((release / IRELAND_SNAPSHOT_RELATIVE_PATH).exists())
            self.assertEqual(list((release / "external-artifacts").iterdir()), [])

    def test_destination_child_open_failure_does_not_leak_fds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            parent = release / "external-artifacts"
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            original_open = ireland_artifact_snapshot._open_directory

            def fail_destination_child(path, **kwargs):
                if kwargs.get("label") == "Ireland staged directory":
                    raise ValueError("injected destination child open failure")
                return original_open(path, **kwargs)

            before = self._fd_count()
            for _ in range(20):
                with (
                    mock.patch(
                        "ireland_artifact_snapshot._open_directory",
                        side_effect=fail_destination_child,
                    ),
                    self.assertRaisesRegex(ValueError, "destination child"),
                ):
                    capture_ireland_snapshot(source, release, max_attempts=1)
            after = self._fd_count()

            self.assertEqual(after - before, 0)
            self.assertFalse(
                (parent / ireland_artifact_snapshot.IRELAND_SOURCE_ID).exists()
            )
            self.assertEqual(list(parent.iterdir()), [])
            self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_temp_post_open_validation_failure_does_not_leak_fds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            parent = release / "external-artifacts"
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            original_type_at = ireland_artifact_snapshot._entry_type_at

            before = self._fd_count()
            for _ in range(20):
                injected = False

                def report_temp_as_regular(name, directory_fd):
                    nonlocal injected
                    if name.startswith(".ireland-snapshot-") and not injected:
                        injected = True
                        return "regular file"
                    return original_type_at(name, directory_fd)

                with (
                    mock.patch(
                        "ireland_artifact_snapshot._entry_type_at",
                        side_effect=report_temp_as_regular,
                    ),
                    self.assertRaisesRegex(ValueError, "changed type"),
                ):
                    capture_ireland_snapshot(source, release, max_attempts=1)
            after = self._fd_count()

            self.assertEqual(after - before, 0)
            self.assertFalse(
                (parent / ireland_artifact_snapshot.IRELAND_SOURCE_ID).exists()
            )
            self.assertEqual(list(parent.iterdir()), [])
            self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_traversal_oserror_closes_pending_fds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tree"
            (root / "one").mkdir(parents=True)
            (root / "two").mkdir()
            (root / "one" / "one.txt").write_text("one", encoding="utf-8")
            (root / "two" / "two.txt").write_text("two", encoding="utf-8")
            original_scandir = os.scandir

            for traversal in (
                ireland_artifact_snapshot._validate_directory_fd,
                ireland_artifact_snapshot._relative_files,
            ):
                root_fd = ireland_artifact_snapshot._open_directory(
                    root,
                    label="Ireland traversal test",
                )
                try:
                    before = self._fd_count()
                    for _ in range(20):
                        calls = 0

                        def fail_second_scan(value):
                            nonlocal calls
                            calls += 1
                            if calls == 2:
                                raise OSError("injected scandir failure")
                            return original_scandir(value)

                        with (
                            mock.patch.object(
                                os,
                                "scandir",
                                side_effect=fail_second_scan,
                            ),
                            self.assertRaisesRegex(
                                ValueError,
                                "cannot be read safely",
                            ),
                        ):
                            if (
                                traversal
                                is ireland_artifact_snapshot._validate_directory_fd
                            ):
                                traversal(root_fd, label="Ireland traversal")
                            else:
                                traversal(root_fd)
                    after = self._fd_count()
                finally:
                    os.close(root_fd)

                self.assertEqual(after - before, 0)

    def test_symlink_and_escaping_manifest_path_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            snapshot = capture_ireland_snapshot(source, release)
            digest, size = artifact_provenance(snapshot.path)
            entry = {
                "path": str(snapshot.path),
                "sha256": digest,
                "size": size,
            }
            validate_ireland_release_snapshot(entry, release)

            escaped = dict(entry, path=str(root / "outside"))
            with self.assertRaisesRegex(ValueError, "escapes"):
                validate_ireland_release_snapshot(escaped, release)

            special_release = root / "releases" / "release-c"
            special_release.mkdir(parents=True)
            os.mkfifo(source / "unsupported.fifo")
            with self.assertRaisesRegex(ValueError, "unsupported filesystem entry"):
                capture_ireland_snapshot(source, special_release)

            invalid_release = root / "releases" / "release-b"
            invalid_release.mkdir(parents=True)
            (invalid_release / "external-artifacts").mkdir()
            (invalid_release / "external-artifacts" / "ireland").symlink_to(source)
            with self.assertRaisesRegex(ValueError, "escapes"):
                validate_ireland_release_snapshot(
                    dict(
                        entry,
                        path=str(invalid_release / IRELAND_SNAPSHOT_RELATIVE_PATH),
                    ),
                    invalid_release,
                )

            destination_release = root / "releases" / "release-d"
            destination_release.mkdir(parents=True)
            destination_parent = destination_release / "external-artifacts"
            destination_parent.mkdir()
            (destination_parent / "ireland").symlink_to(source)
            with self.assertRaisesRegex(ValueError, "already exists"):
                capture_ireland_snapshot(source, destination_release)

    def test_intermediate_parent_symlink_is_rejected_for_capture_and_validation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            (release / "external-artifacts").symlink_to(
                outside, target_is_directory=True
            )

            with self.assertRaisesRegex(ValueError, "snapshot parent"):
                capture_ireland_snapshot(source, release)
            with self.assertRaisesRegex(ValueError, "snapshot parent"):
                validate_ireland_release_snapshot(
                    {"path": str(release / IRELAND_SNAPSHOT_RELATIVE_PATH)},
                    release,
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_intermediate_parent_regular_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            (release / "external-artifacts").write_text("not a directory")

            with self.assertRaisesRegex(ValueError, "snapshot parent"):
                capture_ireland_snapshot(source, release)

    def test_existing_real_parent_continues_to_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            (release / "external-artifacts").mkdir()

            snapshot = capture_ireland_snapshot(source, release)

            self.assertEqual(snapshot.path, release / IRELAND_SNAPSHOT_RELATIVE_PATH)
            self.assertEqual(
                artifact_provenance(snapshot.path), (snapshot.sha256, snapshot.size)
            )

    def test_parent_replacement_before_publish_stays_fd_anchored(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            parent = release / "external-artifacts"
            parent.mkdir()
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            original_publish = ireland_artifact_snapshot._publish_temp_directory

            def replace_parent_then_publish(parent_fd, temporary_name):
                backup = release / "external-artifacts-original"
                parent.rename(backup)
                parent.symlink_to(outside, target_is_directory=True)
                try:
                    original_publish(parent_fd, temporary_name)
                finally:
                    parent.unlink()
                    backup.rename(parent)

            with mock.patch(
                "ireland_artifact_snapshot._publish_temp_directory",
                side_effect=replace_parent_then_publish,
            ):
                snapshot = capture_ireland_snapshot(source, release, max_attempts=1)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(list(outside.iterdir()), [sentinel])
            self.assertTrue(parent.is_dir())
            self.assertEqual(snapshot.path, release / IRELAND_SNAPSHOT_RELATIVE_PATH)

    def test_parent_replacement_before_temp_creation_stays_fd_anchored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            parent = release / "external-artifacts"
            parent.mkdir()
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            original_create = ireland_artifact_snapshot._create_temp_directory

            def replace_parent_then_create(parent_fd):
                backup = release / "external-artifacts-original"
                parent.rename(backup)
                parent.symlink_to(outside, target_is_directory=True)
                try:
                    return original_create(parent_fd)
                finally:
                    parent.unlink()
                    backup.rename(parent)

            with mock.patch(
                "ireland_artifact_snapshot._create_temp_directory",
                side_effect=replace_parent_then_create,
            ):
                snapshot = capture_ireland_snapshot(source, release, max_attempts=1)

            self.assertEqual(
                artifact_provenance(snapshot.path),
                (snapshot.sha256, snapshot.size),
            )
            self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_parent_replacement_before_cleanup_stays_fd_anchored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root, "A")
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            parent = release / "external-artifacts"
            parent.mkdir()
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            original_copy = ireland_artifact_snapshot._copy_regular_file_at
            original_cleanup = ireland_artifact_snapshot._remove_tree_at
            mutated = False
            cleanup_swapped = False

            def copy_and_mutate(source_fd, name, destination_fd):
                nonlocal mutated
                original_copy(source_fd, name, destination_fd)
                if not mutated:
                    mutated = True
                    (source / "stops.txt").write_text("B", encoding="utf-8")

            def replace_parent_then_cleanup(parent_fd, name):
                nonlocal cleanup_swapped
                if not cleanup_swapped and name.startswith(".ireland-snapshot-"):
                    cleanup_swapped = True
                    backup = release / "external-artifacts-original"
                    parent.rename(backup)
                    parent.symlink_to(outside, target_is_directory=True)
                    try:
                        original_cleanup(parent_fd, name)
                    finally:
                        parent.unlink()
                        backup.rename(parent)
                    return
                original_cleanup(parent_fd, name)

            with (
                mock.patch(
                    "ireland_artifact_snapshot._copy_regular_file_at",
                    side_effect=copy_and_mutate,
                ),
                mock.patch(
                    "ireland_artifact_snapshot._remove_tree_at",
                    side_effect=replace_parent_then_cleanup,
                ),
            ):
                snapshot = capture_ireland_snapshot(source, release, max_attempts=2)

            self.assertTrue(mutated)
            self.assertTrue(cleanup_swapped)
            self.assertEqual((snapshot.path / "stops.txt").read_text(), "B")
            self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_validation_after_parent_replacement_reads_from_anchored_fd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            release = root / "releases" / "release-a"
            release.mkdir(parents=True)
            snapshot = capture_ireland_snapshot(source, release)
            digest, size = artifact_provenance(snapshot.path)
            entry = {"path": str(snapshot.path), "sha256": digest, "size": size}
            parent = release / "external-artifacts"
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            original_validate = ireland_artifact_snapshot._validate_snapshot_fd

            def replace_parent_then_validate(parent_fd, manifest_entry):
                backup = release / "external-artifacts-original"
                parent.rename(backup)
                parent.symlink_to(outside, target_is_directory=True)
                try:
                    return original_validate(parent_fd, manifest_entry)
                finally:
                    parent.unlink()
                    backup.rename(parent)

            with mock.patch(
                "ireland_artifact_snapshot._validate_snapshot_fd",
                side_effect=replace_parent_then_validate,
            ):
                validated = validate_ireland_release_snapshot(entry, release)

            self.assertEqual(validated, snapshot.path)
            self.assertEqual(list(outside.iterdir()), [sentinel])


class IrelandResumeTests(unittest.TestCase):
    def test_resume_uses_snapshot_after_shared_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "releases" / "candidate-1"
            stop_data = release / "stop-data"
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
                (stop_data / relative).mkdir(parents=True)
            (stop_data / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": "v1",
                        "releaseID": "candidate-1",
                        "cities": [{"id": "test-city", "url": "stops/test-city.json"}],
                    }
                ),
                encoding="utf-8",
            )
            (stop_data / "transit-radar-cities.json").write_text("{}", encoding="utf-8")
            (stop_data / "swiss-static" / "manifest.json").write_text(
                "{}", encoding="utf-8"
            )
            (stop_data / "provenance" / "input-artifacts.json").write_text(
                "{}", encoding="utf-8"
            )
            (stop_data / "stops" / "test-city.json").write_text("{}", encoding="utf-8")
            (release / "custom-gtfs-artifacts.json").write_text(
                json.dumps(
                    {
                        "sources": {
                            source_id: {
                                "sourceID": source_id,
                                "path": str(release / f"{source_id}.zip"),
                                "sha256": hashlib.sha256(
                                    source_id.encode("utf-8")
                                ).hexdigest(),
                                "size": len(source_id),
                            }
                            for source_id in ("vbb", "rnv")
                        }
                    }
                ),
                encoding="utf-8",
            )
            for source_id in ("vbb", "rnv"):
                (release / f"{source_id}.zip").write_bytes(source_id.encode("utf-8"))
            (release / "release-metadata.json").write_text(
                json.dumps({"releaseID": "candidate-1", "buildFingerprint": "fp"}),
                encoding="utf-8",
            )
            source = root / "shared" / "static"
            source.mkdir(parents=True)
            (source / "stops.txt").write_text("A", encoding="utf-8")
            snapshot = capture_ireland_snapshot(source, release)
            digest, size = artifact_provenance(snapshot.path)
            (release / "gtfs-artifacts.json").write_text(
                json.dumps(
                    {
                        "sources": {},
                        "external": {
                            "ireland": {
                                "path": str(snapshot.path),
                                "sha256": digest,
                                "size": size,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            departures = release / "departures.sqlite"
            connection = sqlite3.connect(departures)
            connection.execute(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO metadata VALUES (?, ?)",
                [
                    ("releaseID", "candidate-1"),
                    ("stopDataReleaseID", "candidate-1"),
                    ("stopDataManifestVersion", "v1"),
                ],
            )
            connection.commit()
            connection.close()
            write_state(
                release,
                release_id="candidate-1",
                completed_stage="build",
                build_fingerprint="fp",
            )
            (source / "stops.txt").write_text("B", encoding="utf-8")

            info = inspect_resume(
                releases_root=root / "releases",
                release_id="candidate-1",
                repository_root=Path(__file__).resolve().parents[1],
                current_fingerprint="fp",
                current=root / "current",
                current_release=root / "current-release",
                static_departures_release=root / "static-departures-release",
                departures_current=root / "departures-current.sqlite",
                previous=root / "previous" / "stop-data",
            )
            self.assertEqual(info.next_stage, "candidate-validation")
            self.assertEqual(artifact_provenance(snapshot.path), (digest, size))

            manifest = json.loads((release / "gtfs-artifacts.json").read_text())
            manifest["external"]["ireland"]["path"] = str(source)
            (release / "gtfs-artifacts.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ResumeError, "escapes"):
                inspect_resume(
                    releases_root=root / "releases",
                    release_id="candidate-1",
                    repository_root=Path(__file__).resolve().parents[1],
                    current_fingerprint="fp",
                    current=root / "current",
                    current_release=root / "current-release",
                    static_departures_release=root / "static-departures-release",
                    departures_current=root / "departures-current.sqlite",
                    previous=root / "previous" / "stop-data",
                )


if __name__ == "__main__":
    unittest.main()
