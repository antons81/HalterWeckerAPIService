import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CLEANUP_SCRIPT = REPOSITORY_ROOT / "ops" / "haltewecker-cleanup.sh"


def write_gtfs(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            archive.writestr(name, "marker,fixture\n")


class HalteWeckerCleanupTests(unittest.TestCase):
    def run_cleaner(
        self, root: Path, data_root: Path, dry_run: bool = True,
        open_paths: tuple[Path, ...] = (),
        retention_count: int = 1,
    ) -> subprocess.CompletedProcess[str]:
        systemctl = root / "systemctl"
        systemctl.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        systemctl.chmod(0o755)
        flock = root / "flock"
        flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        flock.chmod(0o755)
        lsof = root / "lsof"
        lsof.write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            f"paths = {tuple(str(path.resolve()) for path in open_paths)!r}\n"
            "if '-Fn' in sys.argv:\n"
            "    for path in paths: print('n' + path)\n"
            "    raise SystemExit(0 if paths else 1)\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        lsof.chmod(0o755)
        # Emulate GNU readlink consistently on macOS and Linux.
        readlink = root / "readlink"
        readlink.write_text(
            "#!/usr/bin/env python3\nimport os, sys\n"
            "path = sys.argv[-1]\n"
            "if '-m' in sys.argv or '-f' in sys.argv:\n"
            "    resolved = os.path.realpath(path)\n"
            "    if '-f' in sys.argv and not os.path.isdir(os.path.dirname(resolved)):\n"
            "        raise SystemExit(1)\n"
            "    print(resolved)\n"
            "else:\n"
            "    print(os.readlink(path))\n",
            encoding="utf-8",
        )
        readlink.chmod(0o755)
        stat = root / "stat"
        stat.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "if len(sys.argv) == 5 and sys.argv[1:4] == ['-c', '%Y', '--']:\n"
            "    path = sys.argv[4]\n"
            "    print(int(os.stat(path).st_mtime))\n"
            "else:\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        stat.chmod(0o755)
        du = root / "du"
        du.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "path = sys.argv[-1]\n"
            "if os.path.isfile(path):\n"
            "    size = os.path.getsize(path)\n"
            "else:\n"
            "    size = sum(os.path.getsize(os.path.join(base, name)) for base, _, files in os.walk(path) for name in files)\n"
            "print(f'{size}\\t{path}')\n",
            encoding="utf-8",
        )
        du.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "DATA_ROOT": str(data_root),
                "GTFS_CACHE_ROOT": str(root / "cache" / "gtfs"),
                "HALTEWECKER_PIPELINE_REPO": str(REPOSITORY_ROOT),
                "HALTEWECKER_CLEANUP_LOCKS": f"{root / 'stop.lock'}:{root / 'static.lock'}:{root / 'vbb.lock'}",
                "SYSTEMCTL_BIN": str(systemctl),
                "FLOCK_BIN": str(flock),
                "LSOF_BIN": str(lsof),
                "PATH": f"{root}:{environment.get('PATH', '')}",
                "HALTEWECKER_CLEANUP_DRY_RUN": "1" if dry_run else "0",
                "HALTEWECKER_RELEASE_RETENTION_COUNT": str(retention_count),
            }
        )
        for lock in (root / "stop.lock", root / "static.lock", root / "vbb.lock"):
            lock.touch()
        return subprocess.run(
            [str(CLEANUP_SCRIPT)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def make_old(path: Path, age_days: int = 2) -> None:
        old_time = time.time() - age_days * 24 * 60 * 60
        os.utime(path, (old_time, old_time))

    @staticmethod
    def make_age_hours(path: Path, age_hours: float) -> None:
        old_time = time.time() - age_hours * 60 * 60
        os.utime(path, (old_time, old_time))

    @staticmethod
    def make_incremental_release(path: Path, release_id: str) -> None:
        path.mkdir(parents=True)
        (path / "stop-data").mkdir()
        (path / "release.json").write_text(json.dumps({"releaseID": release_id}), encoding="utf-8")
        (path / "validation-receipt.json").write_text(
            json.dumps({"releaseID": release_id, "result": "PASS"}), encoding="utf-8"
        )

    def test_cleanup_and_pipeline_share_the_canonical_default_root(self) -> None:
        cleanup_source = CLEANUP_SCRIPT.read_text(encoding="utf-8")
        pipeline_source = (REPOSITORY_ROOT / "scripts" / "run_stop_data_pipeline.sh").read_text(encoding="utf-8")

        self.assertIn('GTFS_CACHE_ROOT="${GTFS_CACHE_ROOT:-$DATA/cache/gtfs}"', cleanup_source)
        self.assertIn('CACHE_ROOT="${GTFS_CACHE_ROOT:-$DATA_ROOT/cache/gtfs}"', pipeline_source)

    def test_published_artifact_retention_is_wired_to_locked_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / 'data'
            active = data / 'releases/active'
            active.mkdir(parents=True)
            (data / 'current-release').symlink_to(active)
            result = self.run_cleaner(root, data)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('[ArtifactRetention] summary=', result.stdout)
            self.assertIn('"dry_run":true', result.stdout)

    def test_repo_cleanup_uses_data_root_cache_when_gtfs_override_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            current_release = releases / "current-fixture"
            current_release.mkdir(parents=True)
            (data_root / "current-release").symlink_to("releases/current-fixture")
            published_release = releases / "vbb-refresh-20260928T000000Z-100-fixture"
            (published_release / "stop-data").mkdir(parents=True)
            (published_release / "departures.sqlite").write_bytes(b"fixture")
            (published_release / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            cache_root = data_root / "cache" / "gtfs"
            source_directory = cache_root / "sweden"
            source_directory.mkdir(parents=True)
            (source_directory / ".lock").touch()
            systemctl = root / "systemctl"
            systemctl.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            systemctl.chmod(0o755)
            flock = root / "flock"
            flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            flock.chmod(0o755)

            environment = os.environ.copy()
            environment.pop("GTFS_CACHE_ROOT", None)
            environment.update(
                {
                    "DATA_ROOT": str(data_root),
                    "HALTEWECKER_PIPELINE_REPO": str(REPOSITORY_ROOT),
                    "HALTEWECKER_CLEANUP_LOCKS": f"{root / 'stop.lock'}:{root / 'static.lock'}:{root / 'vbb.lock'}",
                    "SYSTEMCTL_BIN": str(systemctl),
                    "FLOCK_BIN": str(flock),
                    "HALTEWECKER_CLEANUP_DRY_RUN": "1",
                }
            )
            for lock_name in ("stop.lock", "static.lock", "vbb.lock"):
                (root / lock_name).touch()

            result = subprocess.run(
                [str(CLEANUP_SCRIPT)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn(
                f"GTFS cache cleanup root={cache_root.resolve()}",
                result.stdout,
                f"returncode={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
            )
            self.assertIn("source=sweden status=skipped reason=current-missing-or-nonregular", result.stdout)

    def test_nonstandard_abandoned_build_classification_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            releases.mkdir(parents=True)

            current = releases / "20260923T000000Z-current"
            current.joinpath("stop-data").mkdir(parents=True)
            (current / "departures.sqlite").write_bytes(b"fixture")
            (current / "release-metadata.json").write_text('{"status":"published"}\n', encoding="utf-8")
            (data_root / "current-release").symlink_to("releases/20260923T000000Z-current")

            failed = releases / "build-failed-1"
            failed.mkdir()
            (failed / "build-state.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_old(failed)

            incomplete = releases / "staging-old-1"
            incomplete.mkdir()
            self.make_old(incomplete)

            recent = releases / "candidate-recent"
            recent.mkdir()
            (recent / "state.json").write_text('{"status":"incomplete"}\n', encoding="utf-8")

            unknown = releases / "mystery-directory"
            unknown.mkdir()
            (unknown / "payload.bin").write_bytes(b"unknown")
            self.make_old(unknown)

            published_nonstandard = releases / "candidate-published"
            published_nonstandard.joinpath("stop-data").mkdir(parents=True)
            (published_nonstandard / "departures.sqlite").write_bytes(b"fixture")
            (published_nonstandard / "release-metadata.json").write_text(
                '{"status":"published"}\n', encoding="utf-8"
            )
            self.make_old(published_nonstandard)

            current_target = releases / "build-current"
            current_target.mkdir()
            (current_target / "state.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_old(current_target)
            (data_root / "active-release").symlink_to("releases/build-current")

            pilot_target = releases / "incremental-packaged" / "20260923T000000Z-proof"
            pilot_target.mkdir(parents=True)
            (releases / "pilot-current").symlink_to("incremental-packaged/20260923T000000Z-proof")

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("DELETE-CANDIDATE " + str(failed.resolve()) + " reason=abandoned-build", output)
            self.assertIn("DELETE-CANDIDATE " + str(incomplete.resolve()) + " reason=abandoned-build", output)
            self.assertIn("KEEP   " + str(recent.resolve()) + " reason=recent-abandoned-build", output)
            self.assertIn("KEEP   " + str(unknown.resolve()) + " reason=unknown-state", output)
            self.assertIn("KEEP   " + str(published_nonstandard.resolve()) + " reason=published-state", output)
            self.assertIn("KEEP   " + str(current_target.resolve()) + " reason=active-symlink:", output)
            self.assertIn(
                "KEEP   " + str((releases / "pilot-current").parent.resolve() / "pilot-current")
                + " reason=protected-pointer/symlink",
                output,
            )
            self.assertIn("KEEP   " + str((releases / "incremental-packaged").resolve()) + " reason=protected-by-pilot-current", output)
            self.assertTrue(failed.exists())
            self.assertTrue(pilot_target.exists())

    def test_repo_cleanup_removes_gtfs_history_preserves_services_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            cache_root = root / "cache" / "gtfs"
            source_directory = cache_root / "sweden"
            release = data_root / "releases" / "20260907T000000Z-fixture"
            release.joinpath("stop-data").mkdir(parents=True)
            (release / "departures.sqlite").write_bytes(b"fixture")
            (release / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            (data_root / "current-release").symlink_to("releases/20260907T000000Z-fixture")
            vbb_releases = (
                "vbb-refresh-20260921T230000Z-100-aaaa1111",
                "vbb-refresh-20260922T000000Z-101-bbbb2222",
                "vbb-refresh-20260922T010000Z-102-cccc3333",
            )
            for vbb_name in vbb_releases:
                vbb_release = data_root / "releases" / vbb_name
                vbb_release.joinpath("stop-data").mkdir(parents=True)
                (vbb_release / "departures.sqlite").write_bytes(b"vbb-fixture")
                (vbb_release / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            source_directory.mkdir(parents=True)
            (root / "stop.lock").touch()
            (root / "static.lock").touch()
            (root / "vbb.lock").touch()

            current = source_directory / "current.zip"
            write_gtfs(current)
            digest = hashlib.sha256(current.read_bytes()).hexdigest()
            current_hash = source_directory / f"{digest}.zip"
            os.link(current, current_hash)
            stale_hash = source_directory / ("a" * 64 + ".zip")
            stale_hash.write_bytes(b"stale-history")
            orphan_temp = source_directory / ".download-orphan.zip"
            orphan_temp.write_bytes(b"")
            old_time = time.time() - 2 * 24 * 60 * 60
            os.utime(orphan_temp, (old_time, old_time))
            (source_directory / ".lock").touch()
            (source_directory / "state.json").write_text(
                (
                    '{"schemaVersion":1,"sourceID":"sweden",'
                    '"artifact":"current.zip","sha256":"'
                    + digest
                    + '","size":'
                    + str(current.stat().st_size)
                    + ',"validated":true}\n'
                ),
                encoding="utf-8",
            )

            systemctl = root / "systemctl"
            systemctl.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            systemctl.chmod(0o755)
            flock = root / "flock"
            flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            flock.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "DATA_ROOT": str(data_root),
                    "GTFS_CACHE_ROOT": str(cache_root),
                    "HALTEWECKER_PIPELINE_REPO": str(REPOSITORY_ROOT),
                    "HALTEWECKER_CLEANUP_LOCKS": f"{root / 'stop.lock'}:{root / 'static.lock'}:{root / 'vbb.lock'}",
                    "SYSTEMCTL_BIN": str(systemctl),
                    "FLOCK_BIN": str(flock),
                    "HALTEWECKER_GTFS_ORPHAN_TEMP_MAX_AGE_HOURS": "1",
                    "HALTEWECKER_CLEANUP_DRY_RUN": "0",
                    "HALTEWECKER_RELEASE_RETENTION_COUNT": "2",
                }
            )

            first = subprocess.run(
                [str(CLEANUP_SCRIPT)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            self.assertIn(f"GTFS cache cleanup root={cache_root}", first.stdout)
            self.assertIn("source=sweden", first.stdout, first.stderr + first.stdout)
            self.assertIn("orphan_temp_removed=1", first.stdout)
            self.assertTrue(current.exists())
            self.assertTrue(current_hash.samefile(current))
            self.assertTrue((source_directory / ".lock").exists())
            self.assertTrue((source_directory / "state.json").exists())
            self.assertTrue((data_root / "releases" / vbb_releases[-1]).exists())
            self.assertFalse((data_root / "releases" / vbb_releases[0]).exists())
            self.assertFalse((data_root / "releases" / vbb_releases[1]).exists())
            self.assertFalse(stale_hash.exists())
            self.assertFalse(orphan_temp.exists())

            second = subprocess.run(
                [str(CLEANUP_SCRIPT)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            self.assertIn("removed=0", second.stdout)
            self.assertIn("orphan_temp_removed=0", second.stdout)

    def test_transitive_current_release_dependency_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            releases.mkdir(parents=True)

            current_root = releases / "incremental" / "20260927T000000Z-current"
            current_root.mkdir(parents=True)
            (current_root / "release.json").write_text(json.dumps({"releaseID": current_root.name}), encoding="utf-8")
            (current_root / "validation-receipt.json").write_text(
                json.dumps({"releaseID": current_root.name, "result": "PASS"}), encoding="utf-8"
            )
            target = releases / "20260926T000000Z-stale"
            target.joinpath("stop-data").mkdir(parents=True)
            (target / "departures.sqlite").write_bytes(b"fixture")
            (target / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            (current_root / "stop-data").symlink_to("../../20260926T000000Z-stale/stop-data")
            (data_root / "current-release").symlink_to("releases/incremental/20260927T000000Z-current")

            newer = releases / "20260928T000000Z-newer"
            newer.joinpath("stop-data").mkdir(parents=True)
            (newer / "departures.sqlite").write_bytes(b"fixture")
            (newer / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            self.make_old(newer)
            self.make_old(target)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("KEEP   " + str(target.resolve()), output)
            self.assertNotIn("WOULD_DELETE " + str(target), output)
            self.assertIn("WOULD_DELETE " + str(newer.resolve()), output)

    def test_incremental_release_retention_one_deletes_older_unreferenced_generations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            self.make_incremental_release(releases / "incremental" / "20260925T000000Z-a", "20260925T000000Z-a")
            self.make_incremental_release(releases / "incremental" / "20260926T000000Z-b", "20260926T000000Z-b")
            current = releases / "incremental" / "20260927T000000Z-c"
            self.make_incremental_release(current, current.name)
            (data_root / "current-release").symlink_to("releases/incremental/" + current.name)

            result = self.run_cleaner(root, data_root, dry_run=False)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertFalse((releases / "incremental" / "20260925T000000Z-a").exists())
            self.assertFalse((releases / "incremental" / "20260926T000000Z-b").exists())
            self.assertTrue(current.exists())

    def test_retention_slot_prioritizes_current_when_newer_unreferenced_generation_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            current = releases / "incremental" / "20260927T000000Z-current"
            newer = releases / "20260928T000000Z-newer"
            self.make_incremental_release(current, current.name)
            newer.joinpath("stop-data").mkdir(parents=True)
            (newer / "departures.sqlite").write_bytes(b"fixture")
            (newer / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            (data_root / "current-release").symlink_to("releases/incremental/" + current.name)

            result = self.run_cleaner(root, data_root, dry_run=True)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("KEEP   " + str(current.resolve()), output)
            self.assertNotIn("WOULD_DELETE " + str(current), output)
            self.assertIn("WOULD_DELETE " + str(newer.resolve()) + " reason=outside-retention-and-unreferenced", output)

    def test_same_id_direct_build_does_not_displace_current_incremental_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            release_id = "20260927T000000Z-sameid"
            direct = releases / release_id
            direct.joinpath("stop-data").mkdir(parents=True)
            (direct / "departures.sqlite").write_bytes(b"direct")
            (direct / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            nested = releases / "incremental" / release_id
            self.make_incremental_release(nested, release_id)
            (data_root / "current-release").symlink_to("releases/incremental/" + release_id)

            result = self.run_cleaner(root, data_root, dry_run=False)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertFalse(direct.exists(), output)
            self.assertTrue(nested.exists())

    def test_nested_runtime_manifest_path_is_resolved_relative_to_its_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            current = releases / "incremental" / "20260927T000000Z-current"
            self.make_incremental_release(current, current.name)
            stale = releases / "20260926T000000Z-stale"
            stale.mkdir(parents=True)
            (stale / "stop-data").mkdir()
            (stale / "departures.sqlite").write_bytes(b"fixture")
            (stale / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            runtime_manifest = current / "providers" / "runtime" / "manifest.json"
            runtime_manifest.parent.mkdir(parents=True)
            (runtime_manifest).write_text(json.dumps({
                "runtimeDependencies": [{"path": "../../../../20260926T000000Z-stale/stop-data"}]
            }), encoding="utf-8")
            (data_root / "current-release").symlink_to("releases/incremental/" + current.name)

            result = self.run_cleaner(root, data_root, dry_run=True)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("KEEP   " + str(stale.resolve()), output)

    def test_nested_previous_rollback_and_pilot_references_are_kept(self) -> None:
        for pointer_name in ('previous/stop-data', 'rollback', 'pilot-current'):
            with self.subTest(pointer=pointer_name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                data_root = root / 'data'
                releases = data_root / 'releases/incremental'
                previous = releases / '20260926T000000Z-previous'
                current = releases / '20260927T000000Z-current'
                self.make_incremental_release(previous, previous.name)
                self.make_incremental_release(current, current.name)
                (data_root / 'current-release').symlink_to(current)
                pointer = data_root / pointer_name
                pointer.parent.mkdir(parents=True, exist_ok=True)
                pointer.symlink_to(previous / 'stop-data' if pointer_name.startswith('previous/') else previous)
                result = self.run_cleaner(root, data_root)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertIn('KEEP   ' + str(previous.resolve()), output)
                self.assertNotIn('WOULD_DELETE ' + str(previous.resolve()), output)

    def test_unfinished_rollback_marker_protects_nested_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / 'data'
            releases = data_root / 'releases/incremental'
            previous = releases / '20260926T000000Z-previous'
            current = releases / '20260927T000000Z-current'
            self.make_incremental_release(previous, previous.name)
            self.make_incremental_release(current, current.name)
            (data_root / 'current-release').symlink_to(current)
            marker = data_root / 'temp/current-rollback' / previous.name
            marker.mkdir(parents=True)
            (marker / 'state.json').write_text('{"state":"pending"}')
            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn('KEEP   ' + str(previous.resolve()) + ' reason=rollback-release:', output)
            self.assertNotIn('WOULD_DELETE ' + str(previous.resolve()), output)

    def test_broken_previous_pointer_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            current = data_root / "releases" / "20260927T000000Z-current"
            current.mkdir(parents=True)
            (current / "stop-data").mkdir()
            (current / "departures.sqlite").write_bytes(b"fixture")
            (current / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            (data_root / "current-release").symlink_to("releases/" + current.name)
            (data_root / "previous").mkdir()
            (data_root / "previous" / "stop-data").symlink_to("../releases/missing/stop-data")

            result = self.run_cleaner(root, data_root, dry_run=True)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("CLEANUP_SKIPPED reason=dependency-graph-unresolved", output)

    def test_unresolved_protected_dependency_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            releases.mkdir(parents=True)

            current_root = releases / "20260927T000000Z-current"
            current_root.mkdir()
            (current_root / "stop-data").symlink_to("../20260926T000000Z-missing/stop-data")
            (data_root / "current-release").symlink_to("releases/20260927T000000Z-current")

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("unresolved-dependency", output)
            self.assertIn("CLEANUP_SKIPPED reason=dependency-graph-unresolved", output)
            self.assertNotIn("DELETE ", output)

    def test_active_pass_validation_is_cleanup_eligible_but_active_release_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            (data_root / "releases").mkdir(parents=True)
            active_release_id = "20260930T102204Z-germany-candidate-r4"
            active_release = data_root / "experiments" / "candidate" / "releases" / active_release_id
            active_release.mkdir(parents=True)
            (active_release / "release.json").write_text(
                '{"releaseID":"20260930T102204Z-germany-candidate-r4"}\n', encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active_release)
            validation = data_root / "validation" / "active-check"
            validation.mkdir(parents=True)
            (validation / "validation-receipt.json").write_text(
                '{"result":"PASS","releaseID":"20260930T102204Z-germany-candidate-r4"}\n',
                encoding="utf-8",
            )
            self.make_age_hours(validation, 0.1)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("WOULD_DELETE " + str(validation.resolve()) + " reason=expired-validation", output)
            self.assertTrue(active_release.exists())

    def test_missing_provenance_allows_obsolete_release_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-r4"
            active.mkdir(parents=True)
            (data_root / "current-release").symlink_to(active)
            obsolete = data_root / "releases" / "build-obsolete"
            obsolete.mkdir()
            (obsolete / "build-state.json").write_text('{"status":"failed"}')
            self.make_age_hours(obsolete, 13)
            missing = obsolete / "external-artifacts" / "ireland"
            record = {"path": str(missing), "sourceID": "ireland"}
            (active / "manifest.json").write_text(json.dumps({"inputProvenance": {"ireland": record}}))
            (active / "provenance").mkdir()
            (active / "provenance" / "input-artifacts.json").write_text(json.dumps({"sources": {"ireland": record}}))

            result = self.run_cleaner(root, data_root, dry_run=False)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertEqual(output.count("reason=missing-provenance-source"), 2, output)
            self.assertNotIn("dependency-graph-unresolved", output)
            self.assertFalse(obsolete.exists(), output)
            self.assertTrue(active.exists())
            self.assertTrue((active / "manifest.json").exists())

    def test_missing_relative_runtime_database_blocks_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-r4"
            active.mkdir(parents=True)
            (data_root / "current-release").symlink_to(active)
            (active / "release.json").write_text(json.dumps({"common": {"path": "common.sqlite"}}))

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("CLEANUP_SKIPPED reason=dependency-graph-unresolved", output)
            self.assertIn("common.sqlite", output)
            self.assertNotIn("DELETE ", output)

    def test_runtime_database_and_open_provenance_artifact_are_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-r4"
            active.mkdir(parents=True)
            (data_root / "current-release").symlink_to(active)
            runtime = data_root / "releases" / "build-runtime"
            opened = data_root / "releases" / "build-opened"
            for directory in (runtime, opened):
                directory.mkdir()
                (directory / "provider.sqlite").write_bytes(b"fixture")
                (directory / "build-state.json").write_text('{"status":"failed"}')
                self.make_age_hours(directory, 13)
            (active / "release.json").write_text(json.dumps({"common": {"path": str(runtime / "provider.sqlite")}}))
            (active / "manifest.json").write_text(json.dumps({"inputProvenance": {"path": str(opened / "provider.sqlite")}}))

            result = self.run_cleaner(root, data_root, dry_run=False, open_paths=(opened / "provider.sqlite",))
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertNotIn("dependency-graph-unresolved", output)
            self.assertTrue(active.exists())
            self.assertTrue(runtime.exists(), output)
            self.assertTrue(opened.exists(), output)
            self.assertIn("active-runtime-open-file", output)

    def test_successful_orphan_staging_is_immediate_and_failed_staging_has_six_hour_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            (data_root / "releases").mkdir(parents=True)
            successful = data_root / "staging" / "completed-build"
            successful.mkdir(parents=True)
            (successful / "build-state.json").write_text('{"status":"published"}\n', encoding="utf-8")
            self.make_age_hours(successful, 0.1)

            failed = data_root / "staging" / "failed-build"
            failed.mkdir(parents=True)
            (failed / "build-state.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_age_hours(failed, 5.9)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("WOULD_DELETE " + str(successful.resolve()) + " reason=expired-staging", output)
            self.assertIn("KEEP   " + str(failed.resolve()) + " reason=recent-staging", output)

    def test_release_scoped_staging_uses_success_state_or_six_hour_failure_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            published = releases / "20260930T000000Z-published"
            published.mkdir(parents=True)
            (published / "state.json").write_text('{"status":"published"}\n', encoding="utf-8")
            successful_staging = published / "departures-next.sqlite"
            successful_staging.write_bytes(b"staged")
            self.make_age_hours(successful_staging, 0.1)

            failed = releases / "20260930T000001Z-failed"
            failed.mkdir(parents=True)
            (failed / "state.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            failed_staging = failed / "departures-next.sqlite"
            failed_staging.write_bytes(b"staged")
            self.make_age_hours(failed_staging, 5.9)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn(
                "WOULD_DELETE " + str(successful_staging.resolve())
                + " reason=successful-release-publication",
                output,
            )
            self.assertIn(
                "ACTIVE STAGING " + str(failed_staging.resolve())
                + " status=KEEP reason=inside-failed-staging-window",
                output,
            )

    def test_failed_validation_is_retained_until_twelve_hour_diagnostic_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            (data_root / "releases").mkdir(parents=True)
            recent = data_root / "validation" / "failed-recent"
            recent.mkdir(parents=True)
            (recent / "result.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_age_hours(recent, 11.9)
            expired = data_root / "validation" / "failed-expired"
            expired.mkdir(parents=True)
            (expired / "result.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_age_hours(expired, 12.1)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("KEEP   " + str(recent.resolve()) + " reason=recent-validation", output)
            self.assertIn("WOULD_DELETE " + str(expired.resolve()) + " reason=expired-validation", output)

    def test_abandoned_build_uses_twelve_hour_ttl_and_protects_symlink_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            releases.mkdir(parents=True)

            active = data_root / "experiments" / "germany-candidate" / "releases" / "active-r4"
            active.mkdir(parents=True)
            target = releases / "incremental" / "20260930T000000Z-active-reference"
            target.mkdir(parents=True)
            (active / "release.json").write_text(
                '{"artifactPath":"' + str(target) + '"}\n', encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active)
            referenced = releases / "20260929T000000Z-symlink-referenced"
            referenced.mkdir(parents=True)
            (data_root / "active-artifact").symlink_to(referenced)

            recent = releases / "candidate-recent"
            recent.mkdir()
            (recent / "state.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_age_hours(recent, 11.9)

            expired = releases / "build-expired"
            expired.mkdir()
            (expired / "state.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_age_hours(expired, 12.1)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("KEEP   " + str(recent.resolve()) + " reason=recent-abandoned-build", output)
            self.assertIn("DELETE-CANDIDATE " + str(expired.resolve()) + " reason=abandoned-build", output)
            self.assertTrue(target.exists())
            self.assertIn(
                "KEEP   " + str(referenced.resolve()) + " reason=active-symlink:",
                output,
            )

    def test_legacy_standalone_departure_uses_twelve_hour_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            release = data_root / "releases" / "20260930T000000Z-current"
            release.mkdir(parents=True)
            (release / "release-metadata.json").write_text('{"status":"published"}\n', encoding="utf-8")
            (data_root / "current-release").symlink_to(release)
            legacy = data_root / "departures" / "releases" / "legacy.sqlite"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"obsolete")
            self.make_age_hours(legacy, 12.1)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn(
                "WOULD_DELETE " + str(legacy.resolve()) + " reason=obsolete-unbound-departures-backup",
                output,
            )


if __name__ == "__main__":
    unittest.main()
