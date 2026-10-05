"""Focused published artifact retention tests.

Created by Anton Stremovskiy on 01.10.2026.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.published_artifact_retention import Processes, Retention, identity, physical_accounting


class PublishedArtifactRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name).resolve()
        self.cache = self.data / "cache/gtfs/external-departure-partitions"
        self.active = self.data / "experiments/arbitrary/location"
        self.active.mkdir(parents=True)
        (self.data / "current-release").symlink_to(self.active)
        (self.active / "release.json").write_text('{}')
        self.now = 1790856000.0

    def entry(self, kind="cache", name="unrelated-name", age=24, day="20260901"):
        if kind == "cache":
            path = self.cache / "provider" / "city" / day / name
            main = "partition.json"
            manifest = dict(providerID="provider", status="complete", serviceDate=day, size=4096)
        else:
            path = self.data / "provider-artifacts/static/provider" / name
            main = "provider.sqlite"
            manifest = dict(providerID="provider", status="complete", artifactKey=name, sqlite={"size": 4096})
        path.mkdir(parents=True)
        (path / main).write_bytes(b'x' * 4096)
        (path / "manifest.json").write_text(json.dumps(manifest))
        for file in path.iterdir():
            os.utime(file, (self.now - age * 3600, self.now - age * 3600))
        return path

    def transformed_entry(self, name, age=24):
        path = self.cache.parent / 'external-build/provider' / name
        path.mkdir(parents=True)
        (path / 'export.json').write_bytes(b'x' * 4096)
        (path / 'manifest.json').write_text(json.dumps({
            'providerID': 'provider', 'status': 'complete', 'key': name,
            'cachedOutputs': [{'path': 'export.json', 'size': 4096, 'sha256': 'a' * 64}],
        }))
        for file in path.iterdir():
            os.utime(file, (self.now - age * 3600, self.now - age * 3600))
        return path

    def test_superseded_transformed_generations_are_deleted_and_latest_is_kept(self):
        oldest = self.transformed_entry('oldest', age=48)
        previous = self.transformed_entry('previous', age=24)
        latest = self.transformed_entry('latest', age=13)
        dry = self.retain()
        self.assertEqual(self.row(dry, oldest)['decision'], 'DELETE')
        self.assertEqual(self.row(dry, previous)['decision'], 'DELETE')
        self.assertEqual(self.row(dry, latest)['reason'], 'latest-transformed-generation')
        applied = self.retain(apply=True)
        self.assertFalse(oldest.exists())
        self.assertFalse(previous.exists())
        self.assertTrue(latest.exists())
        self.assertEqual(applied['applied']['skipped'], [])

    def test_transformed_runtime_dependency_and_open_file_are_protected(self):
        referenced = self.transformed_entry('referenced', age=48)
        opened = self.transformed_entry('opened', age=24)
        self.transformed_entry('latest', age=13)
        (self.active / 'release.json').write_text(json.dumps({
            'runtimeDependencies': [{'path': str(referenced)}],
        }))
        snapshot = Processes(inodes={identity(opened / 'export.json')})
        report = self.retain(processes=snapshot)
        self.assertEqual(self.row(report, referenced)['reason'], 'runtime-path-reference')
        self.assertEqual(self.row(report, opened)['reason'], 'open-fd-or-mmap')

    def test_recent_and_unvalidated_transformed_generations_are_kept(self):
        fresh = self.transformed_entry('fresh', age=1)
        invalid = self.transformed_entry('invalid', age=48)
        (invalid / 'export.json').write_bytes(b'invalid-size')
        report = self.retain()
        self.assertEqual(self.row(report, fresh)['reason'], 'inside-transformed-ttl')
        self.assertEqual(self.row(report, invalid)['reason'], 'unvalidated-transformed-generation')
        self.assertEqual(self.row(report, invalid)['decision'], 'KEEP')

    @staticmethod
    def proc_status(proc, uid=None):
        uid = os.getuid() if uid is None else uid
        proc.mkdir(parents=True, exist_ok=True)
        (proc / 'status').write_text(f'Name:\ttest\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n')

    def retain(self, *, processes=None, apply=False):
        process = Processes() if processes is None else processes
        with patch('scripts.published_artifact_retention.Processes.scan', return_value=process):
            return Retention(self.data, self.cache, now=self.now, processes=process).run(dry_run=not apply)

    @staticmethod
    def row(report, path):
        return next(row for row in report["entries"] if row["path"] == str(path))

    def set_active_key(self, key):
        (self.active / "release.json").write_text(json.dumps({"providers": {"provider": {"structural": {"artifactKey": key}}}}))

    def test_physical_accounting_deduplicates_hardlinks_across_candidate_roots(self):
        first = self.data / "release-a"
        second = self.data / "release-b"
        first.mkdir()
        second.mkdir()
        shared = first / "shared.sqlite"
        shared.write_bytes(b"x" * 8192)
        os.link(shared, second / "shared.sqlite")
        outside = self.data / "retained-provider-artifact.sqlite"
        os.link(shared, outside)
        unique = second / "unique.sqlite"
        unique.write_bytes(b"y" * 4096)

        report = physical_accounting((first, second))

        shared_bytes = shared.stat().st_blocks * 512
        unique_bytes = unique.stat().st_blocks * 512
        directory_bytes = sum(path.stat().st_blocks * 512 for path in (first, second))
        self.assertEqual(report["union_allocated_bytes"], shared_bytes + unique_bytes)
        self.assertEqual(report["shared_outside_candidate_roots_bytes"], shared_bytes)
        self.assertEqual(report["union_directory_allocated_bytes"], directory_bytes)
        self.assertEqual(report["union_reclaimable_bytes"], unique_bytes + directory_bytes)
        self.assertEqual(report["union_inode_count"], 2)
        self.assertEqual(report["candidate_roots"][0]["shared_with_other_candidate_roots_bytes"], shared_bytes)
        self.assertEqual(report["candidate_roots"][0]["shared_anywhere_bytes"], shared_bytes)

    def test_physical_accounting_excludes_retained_lock_metadata(self):
        entry = self.entry()
        lock = entry / '.lock'
        lock.write_bytes(b'lock metadata' * 1024)
        dry = self.retain()
        physical = dry['summary']['physical_accounting']
        expected = sum(path.stat().st_blocks * 512
                       for path in (entry / 'partition.json', entry / 'manifest.json'))
        self.assertEqual(physical['union_reclaimable_bytes'], expected)
        applied = self.retain(apply=True)
        self.assertTrue(lock.exists())
        self.assertEqual(applied['applied']['reclaimed_bytes'], expected)

    def test_root_process_scan_failure_keeps_candidates(self):
        entry = self.entry()
        with patch('scripts.published_artifact_retention.os.getuid', return_value=0):
            report = self.retain(processes=Processes(errors=['proc-root-denied']))
        self.assertEqual(self.row(report, entry)['reason'], 'safety-scan-incomplete')
        self.assertEqual(report['summary']['deletable_candidates'], 0)
        self.assertTrue(entry.exists())

    def test_physical_accounting_uses_allocated_blocks_for_sparse_files(self):
        candidate = self.data / "sparse-candidate"
        candidate.mkdir()
        sparse = candidate / "sparse.bin"
        with sparse.open("wb") as handle:
            handle.seek(1024 * 1024 * 1024 - 1)
            handle.write(b"x")

        report = physical_accounting((candidate,))

        self.assertEqual(report["union_allocated_bytes"], sparse.stat().st_blocks * 512)
        self.assertLess(report["union_allocated_bytes"], sparse.stat().st_size)
        self.assertEqual(
            report["union_reclaimable_bytes"],
            sparse.stat().st_blocks * 512 + candidate.stat().st_blocks * 512,
        )

    def test_referenced_cache_inode_is_kept(self):
        entry = self.entry()
        os.link(entry / 'partition.json', self.active / 'published.json')
        self.assertEqual(self.row(self.retain(), entry)['reason'], 'runtime-inode-reference')

    def test_process_fd_and_mmap_protect_the_matching_artifact(self):
        entry = self.entry()
        proc = self.data / 'proc/123'
        (proc / 'fd').mkdir(parents=True)
        self.proc_status(proc)
        (proc / 'cwd').symlink_to(self.data)
        (proc / 'cmdline').write_bytes(b'build\0')
        (proc / 'fd/5').symlink_to(entry / 'partition.json')
        value = (entry / 'partition.json').stat()
        (proc / 'maps').write_text(f'0-10 r--p 0 {os.major(value.st_dev):x}:{os.minor(value.st_dev):x} {value.st_ino} /file\n')
        snapshot = Processes.scan(proc.parent)
        self.assertFalse(snapshot.errors)
        self.assertIn(identity(entry / 'partition.json'), snapshot.inodes)
        self.assertEqual(self.row(self.retain(processes=snapshot), entry)['reason'], 'open-fd-or-mmap')
        (proc / 'fd/5').unlink()
        snapshot = Processes.scan(proc.parent)
        self.assertEqual(self.row(self.retain(processes=snapshot), entry)['reason'], 'open-fd-or-mmap')

    def test_root_scan_protects_artifacts_open_by_another_uid(self):
        entry = self.entry()
        proc = self.data / 'proc/123'
        (proc / 'fd').mkdir(parents=True)
        self.proc_status(proc, uid=1001)
        (proc / 'cmdline').write_bytes(b'provider-api\0')
        (proc / 'maps').write_text('')
        (proc / 'fd/5').symlink_to(entry / 'partition.json')
        with patch('scripts.published_artifact_retention.os.getuid', return_value=0):
            snapshot = Processes.scan(proc.parent)
        self.assertEqual(snapshot.ignored_unrelated_pids, 0)
        self.assertIn(identity(entry / 'partition.json'), snapshot.inodes)
        report = self.retain(processes=snapshot)
        self.assertEqual(self.row(report, entry)['reason'], 'open-fd-or-mmap')

    def test_unrelated_uid_permission_denied_does_not_block_cleanup(self):
        entry = self.entry()
        proc = self.data / 'proc/123'
        (proc / 'fd').mkdir(parents=True)
        self.proc_status(proc, uid=os.getuid() + 1)
        (proc / 'cmdline').write_bytes(b'unrelated-service\0')
        (proc / 'maps').write_text('')
        original_iterdir = Path.iterdir

        def iterdir(path):
            if path == proc / 'fd':
                raise PermissionError(13, 'permission denied', str(path))
            return original_iterdir(path)

        with patch.object(Path, 'iterdir', iterdir):
            snapshot = Processes.scan(proc.parent)
        self.assertEqual(snapshot.errors, [])
        self.assertEqual(snapshot.relevant_pids, 0)
        self.assertEqual(snapshot.ignored_unrelated_pids, 1)

        report = self.retain(processes=snapshot)
        self.assertEqual(self.row(report, entry)['decision'], 'DELETE')
        self.assertEqual(report['summary']['process_scan']['ignored_unrelated_pids'], 1)

        applied = self.retain(processes=snapshot, apply=True)
        self.assertFalse(entry.exists())
        self.assertEqual(applied['applied']['skipped'], [])

    def test_proc_permission_denied_does_not_block_old_safe_candidate(self):
        entry = self.entry()
        proc = self.data / 'proc/123'
        (proc / 'fd').mkdir(parents=True)
        self.proc_status(proc)
        (proc / 'cmdline').write_bytes(b'deploy-service\0')
        (proc / 'cwd').symlink_to(self.data)
        original_iterdir = Path.iterdir

        def iterdir(path):
            if path == proc / 'fd':
                raise PermissionError(13, 'permission denied', str(path))
            return original_iterdir(path)

        with patch.object(Path, 'iterdir', iterdir):
            snapshot = Processes.scan(proc.parent)
        self.assertEqual(snapshot.relevant_pids, 1)
        self.assertEqual(snapshot.permission_denied_pids, 1)
        report = self.retain(processes=snapshot)
        self.assertEqual(self.row(report, entry)['decision'], 'DELETE')
        self.assertEqual(report['summary']['deletable_candidates'], 1)
        applied = self.retain(processes=snapshot, apply=True)
        self.assertFalse(entry.exists())
        self.assertTrue(applied['summary']['errors'])

    def test_cwd_permission_error_does_not_prevent_remaining_proc_diagnostics(self):
        entry = self.entry()
        proc = self.data / 'proc/123'
        (proc / 'fd').mkdir(parents=True)
        self.proc_status(proc)
        (proc / 'cmdline').write_bytes(b'deploy-service\0')
        unrelated = self.data / 'unrelated-open-file'
        unrelated.write_text('in use elsewhere')
        (proc / 'fd/5').symlink_to(unrelated)
        (proc / 'maps').write_text('')
        original_readlink = os.readlink

        def readlink(path, *args, **kwargs):
            if Path(path) == proc / 'cwd':
                raise PermissionError(13, 'permission denied', str(path))
            return original_readlink(path, *args, **kwargs)

        with patch('scripts.published_artifact_retention.os.readlink', side_effect=readlink):
            snapshot = Processes.scan(proc.parent)
        self.assertEqual(snapshot.cwd_permission_denied, 1)
        self.assertEqual(len(snapshot.errors), 1)
        self.assertIn(identity(unrelated), snapshot.inodes)
        self.assertEqual(self.row(self.retain(processes=snapshot), entry)['decision'], 'DELETE')

    def test_process_disappearing_during_scan_is_ignored(self):
        proc = self.data / 'proc/123'
        proc.mkdir(parents=True)
        self.proc_status(proc)
        (proc / 'cwd').symlink_to(self.data)
        original_read_bytes = Path.read_bytes

        def read_bytes(path):
            if path.name == 'cmdline':
                raise FileNotFoundError(2, 'process disappeared', str(path))
            return original_read_bytes(path)

        with patch.object(Path, 'read_bytes', read_bytes):
            snapshot = Processes.scan(proc.parent)
        self.assertFalse(snapshot.errors)
        self.assertEqual(snapshot.commands, [])

    def test_cache_younger_than_twelve_hours_is_kept(self):
        entry = self.entry(age=11.9)
        self.assertEqual(self.row(self.retain(), entry)['reason'], 'inside-cache-ttl')

    def test_old_cache_is_deleted(self):
        entry = self.entry(age=12.1)
        report = self.retain(apply=True)
        self.assertEqual(len(report['applied']['deleted']), 2)
        self.assertFalse((entry / 'partition.json').exists())
        self.assertFalse(entry.exists())

    def test_old_incomplete_orphan_is_a_candidate_but_fresh_one_is_kept(self):
        old = self.entry(name='old-orphan')
        (old / 'manifest.json').unlink()
        old_time = self.now - 13 * 3600
        os.utime(old / 'partition.json', (old_time, old_time))
        os.utime(old, (old_time, old_time))

        fresh = self.entry(name='fresh-orphan')
        (fresh / 'manifest.json').unlink()

        report = self.retain()
        self.assertEqual(self.row(report, old)['reason'], 'stale-incomplete-orphan')
        self.assertEqual(self.row(report, old)['decision'], 'DELETE')
        self.assertEqual(self.row(report, fresh)['reason'], 'incomplete-too-new')
        self.assertEqual(self.row(report, fresh)['decision'], 'KEEP')

        applied = self.retain(apply=True)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(applied['applied']['skipped'], [])

    def test_old_empty_incomplete_directory_is_a_candidate(self):
        empty = self.cache / 'provider' / 'city' / '20260901' / 'empty-orphan'
        empty.mkdir(parents=True)
        old_time = self.now - 13 * 3600
        os.utime(empty, (old_time, old_time))

        report = self.retain()
        row = self.row(report, empty)
        self.assertEqual(row['decision'], 'DELETE')
        self.assertTrue(row['candidate'])
        self.assertEqual(row['reason'], 'stale-incomplete-orphan')
        self.assertEqual(row['logical_bytes'], 0)
        self.assertTrue(empty.exists())

        applied = self.retain(apply=True)
        self.assertFalse(empty.exists())
        self.assertEqual(applied['applied']['skipped'], [])

    def test_old_cache_temp_directory_is_removed_and_dry_run_matches_apply(self):
        temporary = self.cache / 'provider' / 'city' / '20260901' / ('.' + 'a' * 64 + '.abcdefgh')
        temporary.mkdir(parents=True)
        (temporary / 'partition.json').write_bytes(b'orphan')
        old_time = self.now - 13 * 3600
        os.utime(temporary / 'partition.json', (old_time, old_time))
        os.utime(temporary, (old_time, old_time))

        dry = self.retain()
        row = self.row(dry, temporary)
        self.assertEqual(row['decision'], 'DELETE')
        self.assertEqual(row['reason'], 'stale-incomplete-orphan')
        self.assertTrue(temporary.exists())

        applied = self.retain(apply=True)
        self.assertFalse(temporary.exists())
        self.assertEqual(applied['applied']['skipped'], [])

    def test_missing_and_nonregular_locks_do_not_keep_orphans(self):
        entry = self.entry()
        self.assertEqual(self.row(self.retain(), entry)['decision'], 'DELETE')
        (self.cache / 'provider/.lock').mkdir()
        self.assertEqual(self.row(self.retain(), entry)['decision'], 'DELETE')
        (entry / '.lock').mkdir()
        self.assertEqual(self.row(self.retain(), entry)['decision'], 'DELETE')

    def test_held_entry_lock_blocks_delete(self):
        import fcntl
        entry = self.entry()
        (entry / '.lock').touch()
        with (entry / '.lock').open('rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.row(self.retain(), entry)['reason'], 'running-build')
            applied = self.retain(apply=True)
            self.assertTrue((entry / 'partition.json').exists())
            self.assertEqual(applied['applied']['deleted'], [])

    def test_current_date_uses_provider_timezone(self):
        owner = Retention(self.data, self.cache, now=self.now, processes=Processes())
        owner.timezones['provider'] = 'Pacific/Auckland'
        day = owner.current_date('provider').strftime('%Y%m%d')
        entry = self.entry(day=day)
        self.assertEqual(self.row(owner.run(), entry)['decision'], 'KEEP')

    def test_parent_symlink_is_never_a_delete_candidate(self):
        entry = self.entry()
        city = entry.parents[1]
        replacement = self.data / 'elsewhere'
        city.rename(replacement)
        city.symlink_to(replacement)
        row = self.row(self.retain(), entry)
        self.assertEqual(row['decision'], 'KEEP')
        self.assertTrue(row['reason'].startswith('incomplete-or-unreadable'))

    def test_current_and_future_dates_are_kept(self):
        entries = []
        for day in ('20261001', '20990101'):
            with self.subTest(day=day):
                entry = self.entry(name=day, day=day)
                entries.append(entry)
                self.assertEqual(self.row(self.retain(), entry)['reason'], 'current-or-future-service-date')
        self.retain(apply=True)
        self.assertTrue(all(entry.exists() for entry in entries))

    def test_daily_partition_growth_plateaus_with_fixed_future_horizon_and_ttl(self):
        start = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        retained_counts = []
        daily_accounting = []
        physical_deltas = []
        self.cache.mkdir(parents=True)

        def allocated_bytes():
            report = physical_accounting((self.cache,))
            return report['union_allocated_bytes'] + report['union_directory_allocated_bytes']

        for offset in range(7):
            now = start + timedelta(days=offset)
            now_timestamp = now.timestamp()
            current_date = now.date()
            before_bytes = allocated_bytes()
            added = 0
            for future_offset in range(3):
                service_date = current_date + timedelta(days=future_offset)
                day = service_date.strftime('%Y%m%d')
                path = self.cache / 'provider' / 'city' / day / f'partition-key-{offset}'
                path.mkdir(parents=True)
                (path / 'partition.json').write_bytes(b'x' * 8192)
                (path / 'manifest.json').write_text(json.dumps({
                    'providerID': 'provider', 'status': 'complete',
                    'serviceDate': day, 'size': 8192,
                }))
                os.utime(path / 'partition.json', (now_timestamp, now_timestamp))
                os.utime(path / 'manifest.json', (now_timestamp, now_timestamp))
                os.utime(path, (now_timestamp, now_timestamp))
                added += 1

            after_build_bytes = allocated_bytes()
            planner = Retention(self.data, self.cache, now=now_timestamp, processes=Processes())
            planner.timezones['provider'] = 'UTC'
            dry = planner.run(dry_run=True)
            delete_rows = [row for row in dry['entries'] if row['decision'] == 'DELETE']
            reclaimed_bytes = dry['summary']['physical_accounting']['union_reclaimable_bytes']

            applier = Retention(self.data, self.cache, now=now_timestamp, processes=Processes())
            applier.timezones['provider'] = 'UTC'
            with patch('scripts.published_artifact_retention.Processes.scan', return_value=Processes()):
                applied = applier.run(dry_run=False)
            after_cleanup_bytes = allocated_bytes()
            physical_deltas.append((after_build_bytes - before_bytes,
                                    after_build_bytes - after_cleanup_bytes,
                                    after_cleanup_bytes - before_bytes))
            self.assertEqual(applied['applied']['reclaimed_bytes'], reclaimed_bytes)
            self.assertEqual(after_build_bytes - after_cleanup_bytes, reclaimed_bytes)
            retained = list((self.cache / 'provider' / 'city').glob('*/*'))
            retained_counts.append(len(retained))
            retained_date_directories = list((self.cache / 'provider' / 'city').glob('*'))
            daily_accounting.append((added, len(delete_rows), reclaimed_bytes,
                                     len(applied.get('applied', {}).get('deleted', [])),
                                     applied.get('applied', {}).get('skipped', []),
                                     applied.get('reclaimable_date_directories', []),
                                     applied.get('applied', {}).get('deleted_directories', [])))
            self.assertEqual(len(retained_date_directories), 3,
                             ([path.name for path in retained_date_directories], daily_accounting))

        self.assertEqual(retained_counts, [3, 5, 6, 6, 6, 6, 6], daily_accounting)
        self.assertEqual(daily_accounting[0][0:2], (3, 0))
        for created_bytes, removed_bytes, net_bytes in physical_deltas[3:]:
            self.assertGreater(created_bytes, 0)
            self.assertEqual(created_bytes, removed_bytes, physical_deltas)
            self.assertEqual(net_bytes, 0, physical_deltas)
        for index, (added, removed, reclaimed_bytes, applied_deleted, skipped, date_candidates, deleted_directories) in enumerate(daily_accounting[1:], start=1):
            self.assertEqual(added, 3)
            self.assertEqual(removed, min(index, 3))
            self.assertEqual(applied_deleted, removed * 2)
            self.assertEqual(skipped, [])
            self.assertGreater(reclaimed_bytes, 0)

    def test_shared_source_lock_is_not_reacquired_by_static_scan(self):
        self.entry()
        self.entry('static', 'first')
        old = self.entry('static', 'older', age=40)
        lock = self.cache.parent / 'provider/.lock'
        lock.parent.mkdir(parents=True)
        lock.touch()
        self.assertEqual(self.row(self.retain(), old)['decision'], 'DELETE')

    def test_host_mount_prefix_preserves_process_command_protection(self):
        snapshot = Processes(commands=['builder --workspace /srv/data/version'], filesystem_prefix='/host/root')
        self.assertTrue(snapshot.uses(Path('/host/root/srv/data/version')))

    def test_all_selected_hardlinks_are_counted_only_once(self):
        first = self.entry(name='first')
        second = self.entry(name='second')
        (second / 'partition.json').unlink()
        os.link(first / 'partition.json', second / 'partition.json')
        report = self.retain()
        expected = sum((p / 'manifest.json').stat().st_blocks * 512 for p in (first, second))
        expected += (first / 'partition.json').stat().st_blocks * 512
        self.assertEqual(report['summary']['reclaimable_bytes'], expected)

    def test_active_static_version_is_kept(self):
        entry = self.entry('static', 'opaque-active')
        self.set_active_key('opaque-active')
        self.assertEqual(self.row(self.retain(), entry)['reason'], 'active-artifact-key')

    def test_static_keeps_active_and_latest_fallback_not_third_version(self):
        active = self.entry('static', 'not-a-release', age=2)
        fallback = self.entry('static', 'older-ready', age=10)
        obsolete = self.entry('static', 'oldest-ready', age=30)
        self.set_active_key('not-a-release')
        report = self.retain()
        self.assertEqual(self.row(report, active)['decision'], 'KEEP')
        self.assertEqual(self.row(report, fallback)['reason'], 'latest-fallback')
        self.assertEqual(self.row(report, obsolete)['decision'], 'DELETE')
        applied = self.retain(apply=True)
        self.assertTrue(active.exists())
        self.assertTrue(fallback.exists())
        self.assertFalse(obsolete.exists())
        self.assertEqual(applied['applied']['skipped'], [])

    def test_active_hardlinked_inode_survives_old_static_path_unlink(self):
        obsolete = self.entry('static', 'old', age=30)
        self.entry('static', 'fallback', age=10)
        os.link(obsolete / 'provider.sqlite', self.active / 'runtime.sqlite')
        report = self.retain()
        row = self.row(report, obsolete)
        self.assertEqual(row['decision'], 'DELETE')
        self.assertEqual(row['reclaimable_bytes'], (obsolete / 'manifest.json').stat().st_blocks * 512)
        result = self.retain(apply=True)
        self.assertTrue((self.active / 'runtime.sqlite').exists())
        self.assertFalse((obsolete / 'provider.sqlite').exists())
        self.assertEqual(result['applied']['reclaimed_bytes'], row['reclaimable_bytes'])

    def test_temporal_update_does_not_change_static_fallback_order(self):
        fallback = self.entry('static', 'newer', age=10)
        obsolete = self.entry('static', 'older', age=30)
        temporal = obsolete / 'temporal'
        temporal.mkdir()
        (temporal / 'updated.sqlite').write_bytes(b'updated')
        report = self.retain()
        self.assertEqual(self.row(report, fallback)['reason'], 'latest-fallback')
        self.assertEqual(self.row(report, obsolete)['decision'], 'DELETE')

    def test_build_workspace_is_kept(self):
        entry = self.entry()
        snapshot = Processes(workspaces=[str(entry / 'work')])
        self.assertEqual(self.row(self.retain(processes=snapshot), entry)['reason'], 'running-build')

    def test_held_provider_lock_protects_build(self):
        import fcntl
        entry = self.entry()
        lock = self.cache / 'provider/.lock'
        lock.touch()
        with lock.open('rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.row(self.retain(), entry)['reason'], 'running-build')

    def test_names_do_not_determine_retention(self):
        for name in ('germany-candidate-r4', 'release-stop-data-20990101', 'opaque'):
            entry = self.entry(name=name)
            self.assertEqual(self.row(self.retain(), entry)['decision'], 'DELETE')

    def test_normalized_is_untouched_and_incomplete_static_kept(self):
        normalized = self.data / 'provider-artifacts/normalized/anything'
        normalized.mkdir(parents=True)
        (normalized / 'data').write_bytes(b'keep')
        static = self.entry('static', 'incomplete')
        (static / 'manifest.json').unlink()
        self.retain(apply=True)
        self.assertEqual((normalized / 'data').read_bytes(), b'keep')
        self.assertTrue((static / 'provider.sqlite').exists())

    def test_proc_scan_errors_are_reported_but_candidate_is_deleted(self):
        entry = self.entry()
        report = self.retain(processes=Processes(errors=['permission denied']), apply=True)
        row = self.row(report, entry)
        self.assertEqual(row['decision'], 'DELETE')
        self.assertTrue(row['candidate'])
        self.assertEqual(row['candidate_reason'], 'expired-unreferenced-cache')
        self.assertEqual(row['reason'], 'expired-unreferenced-cache')
        self.assertEqual(report['summary']['candidates'], 1)
        self.assertEqual(report['summary']['deletable_candidates'], 1)
        self.assertEqual(report['summary']['safety_blocked_candidates'], 0)
        self.assertFalse(entry.exists())

    def test_proc_scan_failure_during_apply_is_diagnostic_only(self):
        entry = self.entry()
        owner = Retention(self.data, self.cache, now=self.now, processes=Processes())
        report = owner.run()
        self.assertEqual(self.row(report, entry)['decision'], 'DELETE')
        with patch('scripts.published_artifact_retention.Processes.scan', return_value=Processes(errors=['proc-root-denied'])):
            owner.apply(report)
        self.assertFalse(entry.exists())
        self.assertEqual(report['applied']['skipped'], [])

    def test_active_artifact_remains_protected_when_other_orphan_is_removed(self):
        active = self.entry('static', 'active')
        self.set_active_key('active')
        orphan = self.entry(name='orphan')
        (orphan / 'manifest.json').unlink()
        old_time = self.now - 13 * 3600
        os.utime(orphan / 'partition.json', (old_time, old_time))
        os.utime(orphan, (old_time, old_time))

        report = self.retain(apply=True)
        self.assertTrue((active / 'provider.sqlite').exists())
        self.assertEqual(self.row(report, active)['reason'], 'active-artifact-key')
        self.assertFalse(orphan.exists())

    def test_missing_runtime_dependency_fails_closed(self):
        entry = self.entry()
        (self.active / 'release.json').write_text('{"common":{"path":"missing.sqlite"}}')
        self.assertEqual(self.row(self.retain(), entry)['decision'], 'KEEP')

    def test_changed_inode_after_plan_is_skipped(self):
        entry = self.entry()
        owner = Retention(self.data, self.cache, now=self.now, processes=Processes())
        report = owner.run()
        (entry / 'partition.json').unlink()
        (entry / 'partition.json').write_bytes(b'new')
        with patch('scripts.published_artifact_retention.Processes.scan', return_value=Processes()):
            owner.apply(report)
        self.assertEqual(len(report['applied']['deleted']), 0)
        self.assertEqual(len(report['applied']['skipped']), 1)

    def test_new_file_after_plan_skips_candidate(self):
        entry = self.entry()
        owner = Retention(self.data, self.cache, now=self.now, processes=Processes())
        report = owner.run()
        (entry / 'new-file').write_text('late write')
        old_time = self.now - 13 * 3600
        os.utime(entry / 'new-file', (old_time, old_time))
        with patch('scripts.published_artifact_retention.Processes.scan', return_value=Processes()):
            owner.apply(report)
        self.assertTrue((entry / 'partition.json').exists())
        self.assertTrue((entry / 'new-file').exists())
        self.assertEqual(report['applied']['deleted'], [])
        self.assertEqual(report['applied']['skipped'][0]['reason'], 'entry-contents-changed')

    def test_new_entry_lock_after_plan_skips_candidate(self):
        import fcntl
        entry = self.entry()
        owner = Retention(self.data, self.cache, now=self.now, processes=Processes())
        report = owner.run()
        lock = entry / '.lock'
        lock.touch()
        with lock.open('rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('scripts.published_artifact_retention.Processes.scan', return_value=Processes()):
                owner.apply(report)
        self.assertTrue((entry / 'partition.json').exists())
        self.assertEqual(report['applied']['deleted'], [])
        self.assertEqual(report['applied']['skipped'][0]['reason'], 'candidate-no-longer-eligible')

    def test_new_active_reference_after_plan_skips_candidate(self):
        entry = self.entry()
        owner = Retention(self.data, self.cache, now=self.now, processes=Processes())
        report = owner.run()
        (self.active / 'release.json').write_text(json.dumps({
            'runtimeDependencies': {'late-reference': {'path': str(entry / 'partition.json')}}
        }))
        with patch('scripts.published_artifact_retention.Processes.scan', return_value=Processes()):
            owner.apply(report)
        self.assertTrue((entry / 'partition.json').exists())
        self.assertEqual(report['applied']['deleted'], [])
        self.assertEqual(report['applied']['skipped'][0]['reason'], 'candidate-no-longer-eligible')

    def test_expired_cache_orphan_and_temp_are_removed_after_ttl(self):
        orphan = self.entry(name='orphan')
        (orphan / 'manifest.json').unlink()
        old_time = self.now - 13 * 3600
        os.utime(orphan / 'partition.json', (old_time, old_time))
        os.utime(orphan, (old_time, old_time))
        temporary = self.cache / 'provider' / 'city' / '20260901' / ('.' + 'b' * 64 + '.temp')
        temporary.mkdir(parents=True)
        (temporary / 'partition.json').write_bytes(b'temporary')
        os.utime(temporary / 'partition.json', (old_time, old_time))
        os.utime(temporary, (old_time, old_time))
        expired = self.entry(name='expired-cache')

        report = self.retain()
        self.assertEqual(self.row(report, orphan)['decision'], 'DELETE')
        self.assertEqual(self.row(report, temporary)['decision'], 'DELETE')
        self.assertEqual(self.row(report, expired)['decision'], 'DELETE')
        self.assertTrue(orphan.exists() and temporary.exists() and expired.exists())

        applied = self.retain(apply=True)
        self.assertFalse(orphan.exists())
        self.assertFalse(temporary.exists())
        self.assertFalse(expired.exists())
        self.assertEqual(applied['applied']['skipped'], [])


if __name__ == '__main__':
    unittest.main()
