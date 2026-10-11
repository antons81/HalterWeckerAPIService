"""Regression coverage for immutable candidate payload sharing.

Created by Anton on 2026-10-10.
"""
import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.deduplicate_stop_data import deduplicate


class StopDataDeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.candidate = self.root / 'old', self.root / 'new'
        for directory, release in ((self.source, 'old'), (self.candidate, 'new')):
            directory.mkdir()
            (directory / 'manifest.json').write_text(json.dumps({'releaseID': release}))
            (directory / 'departures-v2').mkdir()
        self.previous = self.source / 'departures-v2' / 'day.json'
        self.current = self.candidate / 'departures-v2' / 'day.json'
        self.previous.write_text('{"departures":[1,2]}')
        self.current.write_bytes(self.previous.read_bytes())

    def test_identical_payload_is_shared_without_changing_source_bytes(self):
        before = self.previous.read_bytes()
        original_inode = self.previous.stat().st_ino
        result = deduplicate(self.source, self.candidate)
        self.assertEqual(result['linkedFiles'], 1)
        self.assertGreater(result['reclaimedBytes'], 0)
        self.assertEqual(self.current.stat().st_ino, original_inode)
        self.assertEqual(self.previous.read_bytes(), before)
        self.assertEqual(json.loads((self.source / 'manifest.json').read_text())['releaseID'], 'old')
        self.assertEqual(json.loads((self.candidate / 'manifest.json').read_text())['releaseID'], 'new')

    def test_same_size_different_payload_is_not_shared(self):
        self.current.write_text('{"departures":[3,4]}')
        self.assertEqual(deduplicate(self.source, self.candidate)['linkedFiles'], 0)
        self.assertNotEqual(self.current.stat().st_ino, self.previous.stat().st_ino)

    def test_existing_cache_links_are_preserved(self):
        os.link(self.current, self.root / 'cache.json')
        before = self.current.stat().st_ino
        self.assertEqual(deduplicate(self.source, self.candidate)['linkedFiles'], 0)
        self.assertEqual(self.current.stat().st_ino, before)

    def test_generation_manifests_are_never_shared(self):
        for root in (self.source, self.candidate):
            (root / 'departures-v2' / 'manifest.json').write_text('{}')
        deduplicate(self.source, self.candidate)
        self.assertNotEqual((self.source / 'departures-v2' / 'manifest.json').stat().st_ino,
                            (self.candidate / 'departures-v2' / 'manifest.json').stat().st_ino)

    def test_source_symlink_is_skipped(self):
        self.previous.unlink()
        self.previous.symlink_to(self.current)
        self.assertEqual(deduplicate(self.source, self.candidate)['linkedFiles'], 0)

    def test_candidate_symlink_fails_closed(self):
        self.current.unlink()
        self.current.symlink_to(self.previous)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            deduplicate(self.source, self.candidate)

    def test_same_generation_is_rejected(self):
        (self.candidate / 'manifest.json').write_text('{"releaseID":"old"}')
        with self.assertRaisesRegex(ValueError, 'distinct'):
            deduplicate(self.source, self.candidate)

    def test_active_tree_cannot_be_used_as_candidate(self):
        with self.assertRaisesRegex(ValueError, 'separate'):
            deduplicate(self.source, self.source)

    def test_cross_device_link_keeps_original_candidate(self):
        before = self.current.stat().st_ino
        with patch('scripts.deduplicate_stop_data.os.link', side_effect=OSError(errno.EXDEV, 'different device')):
            result = deduplicate(self.source, self.candidate)
        self.assertEqual(result['linkedFiles'], 0)
        self.assertEqual(self.current.stat().st_ino, before)
        self.assertEqual(len(list(self.current.parent.iterdir())), 1)

    def test_failed_replace_keeps_original_and_removes_only_temporary_link(self):
        before = self.current.stat().st_ino
        with patch('scripts.deduplicate_stop_data.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                deduplicate(self.source, self.candidate)
        self.assertEqual(self.current.stat().st_ino, before)
        self.assertEqual(len(list(self.current.parent.iterdir())), 1)
        self.assertEqual(self.previous.stat().st_nlink, 1)

    def test_changed_source_during_comparison_is_rejected(self):
        def mutate(source, candidate):
            source.write_text('changed')
            return True
        with patch('scripts.deduplicate_stop_data.same_bytes', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'changed during'):
                deduplicate(self.source, self.candidate)
        self.assertNotEqual(self.current.stat().st_ino, self.previous.stat().st_ino)

    def test_replacement_directory_is_synced_before_success(self):
        with patch('scripts.deduplicate_stop_data.os.fsync') as sync:
            result = deduplicate(self.source, self.candidate)
        self.assertEqual(result['linkedFiles'], 1)
        sync.assert_called_once()
