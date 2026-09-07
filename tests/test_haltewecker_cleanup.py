import hashlib
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
            source_directory.mkdir(parents=True)
            (root / "stop.lock").touch()
            (root / "static.lock").touch()

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
                    "HALTEWECKER_CLEANUP_LOCKS": f"{root / 'stop.lock'}:{root / 'static.lock'}",
                    "SYSTEMCTL_BIN": str(systemctl),
                    "FLOCK_BIN": str(flock),
                    "HALTEWECKER_GTFS_ORPHAN_TEMP_MAX_AGE_HOURS": "1",
                    "HALTEWECKER_CLEANUP_DRY_RUN": "0",
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
            self.assertIn("source=sweden", first.stdout, first.stderr + first.stdout)
            self.assertIn("orphan_temp_removed=1", first.stdout)
            self.assertTrue(current.exists())
            self.assertTrue(current_hash.samefile(current))
            self.assertTrue((source_directory / ".lock").exists())
            self.assertTrue((source_directory / "state.json").exists())
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


if __name__ == "__main__":
    unittest.main()
