"""Focused published artifact retention tests.

Created by Anton Stremovskiy on 01.10.2026.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.published_artifact_retention import Processes, Retention, identity


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
            path = self.cache / "provider" / "city" / "unrelated-date-directory" / name
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

    def retain(self, *, processes=None, apply=False):
        process = Processes() if processes is None else processes
        with patch('scripts.published_artifact_retention.Processes.scan', return_value=process):
            return Retention(self.data, self.cache, now=self.now, processes=process).run(dry_run=not apply)

    @staticmethod
    def row(report, path):
        return next(row for row in report["entries"] if row["path"] == str(path))

    def set_active_key(self, key):
        (self.active / "release.json").write_text(json.dumps({"providers": {"provider": {"structural": {"artifactKey": key}}}}))

    def test_referenced_cache_inode_is_kept(self):
        entry = self.entry()
        os.link(entry / 'partition.json', self.active / 'published.json')
        self.assertEqual(self.row(self.retain(), entry)['reason'], 'runtime-inode-reference')

    def test_fd_and_mmap_scanner_protects_cache(self):
        entry = self.entry()
        proc = self.data / 'proc/123'
        (proc / 'fd').mkdir(parents=True)
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

    def test_cache_younger_than_twelve_hours_is_kept(self):
        entry = self.entry(age=11.9)
        self.assertEqual(self.row(self.retain(), entry)['reason'], 'inside-cache-ttl')

    def test_old_cache_is_deleted(self):
        entry = self.entry(age=12.1)
        report = self.retain(apply=True)
        self.assertEqual(len(report['applied']['deleted']), 2)
        self.assertFalse((entry / 'partition.json').exists())

    def test_missing_and_nonregular_locks_do_not_keep_orphans(self):
        entry = self.entry()
        self.assertEqual(self.row(self.retain(), entry)['decision'], 'DELETE')
        (self.cache / 'provider/.lock').mkdir()
        self.assertEqual(self.row(self.retain(), entry)['decision'], 'DELETE')
        (entry / '.lock').mkdir()
        self.assertEqual(self.row(self.retain(), entry)['decision'], 'DELETE')

    def test_held_entry_lock_protects_in_progress_build(self):
        import fcntl
        entry = self.entry()
        (entry / '.lock').touch()
        with (entry / '.lock').open('rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.row(self.retain(), entry)['reason'], 'running-build')

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
        for day in ('20261001', '20990101'):
            with self.subTest(day=day):
                entry = self.entry(name=day, day=day)
                self.assertEqual(self.row(self.retain(), entry)['reason'], 'current-or-future-service-date')

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

    def test_incomplete_process_scan_fails_closed(self):
        entry = self.entry()
        report = self.retain(processes=Processes(errors=['permission denied']), apply=True)
        self.assertEqual(self.row(report, entry)['reason'], 'safety-scan-incomplete')
        self.assertTrue((entry / 'partition.json').exists())

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


if __name__ == '__main__':
    unittest.main()
