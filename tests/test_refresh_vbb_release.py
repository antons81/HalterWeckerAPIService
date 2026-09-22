from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
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
            source_state = {
                "sourceCheckedAt": refresh._now(),
                "downloadedAt": refresh._now(),
                "lastModified": "Wed, 09 Sep 2026 00:00:00 GMT",
                "etag": '"vbb"',
                "contentLength": archive_path.stat().st_size,
            }
            with mock.patch.object(refresh, "_resolve_vbb_artifact", return_value=(archive_path, "sha", archive_path.stat().st_size, source_state)), mock.patch.object(refresh, "build_vbb_network_indexes") as build, mock.patch.object(refresh, "_validate_vbb_packages", return_value={"fileCount": 2, "currentHour": "20260917-16", "previousHour": "20260917-15", "currentBytes": 1, "previousBytes": 1}), mock.patch.object(refresh, "validate_release"), mock.patch.object(refresh, "load_cities", return_value=[]), mock.patch.object(refresh, "run_readiness"):
                candidate, report = refresh.build_candidate(root, Path(temporary), cache_root=cache)
            self.assertEqual(report["baseReleaseID"], "base")
            self.assertEqual(os.stat(candidate / "departures.sqlite").st_ino, os.stat(base / "departures.sqlite").st_ino)
            self.assertEqual(os.stat(candidate / "stop-data" / "stops" / "berlin.json").st_ino, os.stat(base / "stop-data" / "stops" / "berlin.json").st_ino)
            self.assertTrue(build.called)
            self.assertEqual(json.loads((candidate / "release-metadata.json").read_text())["refreshedProviders"], ["vbb"])
            source_provenance = json.loads((candidate / "release-metadata.json").read_text())["scopedRefresh"]["vbb"]
            self.assertEqual(source_provenance["sourceUrl"], refresh.DEFAULT_VBB_URL)
            self.assertTrue(source_provenance["sourceFresh"])
            self.assertEqual(source_provenance["etag"], '"vbb"')

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
            shutil.copy2(base / "release-metadata.json", candidate / "release-metadata.json")
            with mock.patch.object(refresh, "validate_release"), mock.patch.object(refresh.subprocess, "run", side_effect=RuntimeError("compose failed")):
                with self.assertRaises(RuntimeError):
                    refresh.activate_candidate(root, Path(temporary), candidate)
            self.assertEqual(os.readlink(root / "current-release"), "releases/base")

    def test_build_failure_keeps_current_release_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._base_release(root)
            cache = root / "cache"
            archive_path = cache / "vbb" / "current.zip"
            archive_path.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive_path, "w"):
                pass
            source_state = {"sourceCheckedAt": refresh._now()}
            with mock.patch.object(
                refresh,
                "_resolve_vbb_artifact",
                return_value=(archive_path, "sha", 1, source_state),
            ), mock.patch.object(
                refresh,
                "build_vbb_network_indexes",
                side_effect=RuntimeError("synthetic build failure"),
            ), mock.patch.object(refresh, "load_cities", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "synthetic build failure"):
                    refresh.build_candidate(root, Path(temporary), cache_root=cache)

            self.assertEqual(os.readlink(root / "current-release"), "releases/base")
            self.assertEqual(list((root / "releases").glob(".vbb-refresh-*.staging")), [])

    def test_successful_activation_switches_all_runtime_pointers_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = self._base_release(root)
            candidate = root / "releases" / "candidate"
            candidate.mkdir()
            shutil.copy2(base / "release-metadata.json", candidate / "release-metadata.json")

            def run_docker(command, **kwargs):
                if "exec" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout='{"database":{"releaseID":"base"}}',
                        stderr="",
                    )
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(refresh, "validate_release"), mock.patch.object(
                refresh.subprocess,
                "run",
                side_effect=run_docker,
            ):
                refresh.activate_candidate(root, Path(temporary), candidate)

            self.assertEqual((root / "current-release").resolve(), candidate.resolve())
            self.assertEqual((root / "current").resolve(), (candidate / "stop-data").resolve())
            self.assertEqual(
                (root / "departures-current.sqlite").resolve(),
                (candidate / "departures.sqlite").resolve(),
            )

    def test_source_freshness_guard_rejects_old_upstream_check(self) -> None:
        old = (datetime.now(refresh.timezone.utc) - refresh.timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        with self.assertRaisesRegex(refresh.VBBRefreshError, "source check is stale"):
            refresh._vbb_source_provenance(
                refresh.DEFAULT_VBB_URL,
                "sha",
                1,
                {"sourceCheckedAt": old},
            )

    def test_refresh_lock_rejects_concurrent_invocation_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "vbb.lock"
            with refresh._refresh_lock(lock_path) as first:
                self.assertTrue(first)
                with refresh._refresh_lock(lock_path) as second:
                    self.assertFalse(second)

    def test_refresh_state_records_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._base_release(root)
            candidate = root / "releases" / "candidate"
            candidate.mkdir()
            args = refresh.parse_args(
                [
                    "--refresh",
                    "--data-root",
                    str(root),
                    "--repository-root",
                    str(root),
                    "--cache-root",
                    str(root / "cache"),
                ]
            )
            report = {
                "vbb": {
                    "currentHour": "20260922-10",
                    "sourceCheckedAt": refresh._now(),
                    "sha256": "sha",
                }
            }
            with mock.patch.object(refresh, "build_candidate", return_value=(candidate, report)), mock.patch.object(refresh, "activate_candidate"):
                self.assertEqual(refresh._run_operation(args), 0)

            state_path = root / refresh.REFRESH_STATE_FILENAME
            state = json.loads(state_path.read_text())
            self.assertEqual(state["status"], "success")
            self.assertEqual(state["currentRelease"], "base")
            self.assertEqual(state["currentVbbPackageHour"], "20260922-10")
            self.assertEqual(state["sourceSha256"], "sha")

            with mock.patch.object(refresh, "build_candidate", side_effect=RuntimeError("synthetic refresh failure")):
                with self.assertRaisesRegex(RuntimeError, "synthetic refresh failure"):
                    refresh._run_operation(args)

            failed_state = json.loads(state_path.read_text())
            self.assertEqual(failed_state["status"], "failed")
            self.assertIn("synthetic refresh failure", failed_state["error"])
            self.assertEqual(failed_state["lastSuccessAt"], state["lastSuccessAt"])


if __name__ == "__main__":
    unittest.main()
