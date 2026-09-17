from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

from scripts import refresh_vbb_release as refresh


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class VBBRefreshTests(unittest.TestCase):
    def _base_release(self, root: Path) -> Path:
        release = root / "releases" / "base"
        stop_data = release / "stop-data"
        (stop_data / "transit" / "vbb" / "berlin").mkdir(parents=True)
        _write_json(
            release / "release-metadata.json",
            {
                "releaseID": "base",
                "buildFingerprint": "fingerprint",
                "stopManifestVersion": "2026-09-09",
                "sourceArtifacts": {},
                "inputProvenance": {},
            },
        )
        _write_json(
            stop_data / "manifest.json",
            {
                "version": "2026-09-09",
                "releaseID": "base",
                "cities": [{"id": "berlin", "url": "stops/berlin.json"}],
                "sourceArtifacts": {},
                "inputProvenance": {},
            },
        )
        _write_json(stop_data / "stops" / "berlin.json", [])
        _write_json(stop_data / "transit-radar-cities.json", {"cities": []})
        _write_json(stop_data / "swiss-static" / "manifest.json", {})
        _write_json(stop_data / "provenance" / "input-artifacts.json", {})
        sqlite3.connect(release / "departures.sqlite").close()
        (root / "current-release").symlink_to("releases/base")
        return release

    def test_candidate_reuses_base_files_and_replaces_only_vbb(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = self._base_release(root)
            cache = root / "cache"
            (cache / "vbb").mkdir(parents=True)
            archive_path = cache / "vbb" / "current.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n")
            with mock.patch.object(refresh, "_resolve_vbb_artifact", return_value=(archive_path, "sha", archive_path.stat().st_size)), mock.patch.object(refresh, "build_vbb_network_indexes") as build, mock.patch.object(refresh, "_validate_vbb_packages", return_value={"fileCount": 2, "currentHour": "20260917-16", "previousHour": "20260917-15", "currentBytes": 1, "previousBytes": 1}), mock.patch.object(refresh, "validate_release"), mock.patch.object(refresh, "load_cities", return_value=[]), mock.patch.object(refresh, "run_readiness"):
                candidate, report = refresh.build_candidate(root, Path(temporary), cache_root=cache)
            self.assertEqual(report["baseReleaseID"], "base")
            self.assertEqual(os.stat(candidate / "departures.sqlite").st_ino, os.stat(base / "departures.sqlite").st_ino)
            self.assertEqual(os.stat(candidate / "stop-data" / "stops" / "berlin.json").st_ino, os.stat(base / "stop-data" / "stops" / "berlin.json").st_ino)
            self.assertTrue(build.called)
            self.assertEqual(json.loads((candidate / "release-metadata.json").read_text())["refreshedProviders"], ["vbb"])

    def test_missing_base_release_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "current-release").symlink_to("releases/missing")
            with self.assertRaises(refresh.VBBRefreshError):
                refresh._base_release(root)

    def test_disk_floor_blocks_before_build(self) -> None:
        with mock.patch.object(refresh.shutil, "disk_usage", return_value=mock.Mock(free=refresh.PRODUCTION_FLOOR_BYTES - 1)):
            with self.assertRaises(refresh.VBBRefreshError):
                refresh._disk_preflight(Path("/tmp"), 0)

    def test_activation_restores_all_pointers_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = self._base_release(root)
            candidate = root / "releases" / "candidate"
            candidate.mkdir()
            shutil = __import__("shutil")
            shutil.copy2(base / "release-metadata.json", candidate / "release-metadata.json")
            with mock.patch.object(refresh, "validate_release"), mock.patch.object(refresh.subprocess, "run", side_effect=RuntimeError("compose failed")):
                with self.assertRaises(RuntimeError):
                    refresh.activate_candidate(root, Path(temporary), candidate)
            self.assertEqual(os.readlink(root / "current-release"), "releases/base")


if __name__ == "__main__":
    unittest.main()
