import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from validate_release_consistency import validate_release


class ReleaseConsistencyTests(unittest.TestCase):
    def _write_release(self, root: Path, manifest_version: str = "2026-08-14") -> None:
        release = root / "release-1"
        stop_data = release / "stop-data"
        stop_data.mkdir(parents=True)
        (stop_data / "manifest.json").write_text(
            json.dumps({"version": manifest_version, "releaseID": "release-1"}),
            encoding="utf-8",
        )
        (release / "release-metadata.json").write_text(
            json.dumps({
                "releaseID": "release-1",
                "buildFingerprint": "fingerprint",
                "stopManifestVersion": manifest_version,
            }),
            encoding="utf-8",
        )
        database = sqlite3.connect(release / "departures.sqlite")
        database.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        database.execute("INSERT INTO metadata VALUES ('releaseID', 'release-1')")
        database.commit()
        database.close()

    def test_matching_manifest_and_database_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_release(root)
            validate_release(root / "release-1")

    def test_manifest_version_mismatch_fails_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_release(root, manifest_version="2026-08-14")
            metadata_path = root / "release-1" / "release-metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["stopManifestVersion"] = "2026-08-10"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "stopManifestVersion.*manifest.version"):
                validate_release(root / "release-1")


if __name__ == "__main__":
    unittest.main()
