import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from validate_stop_data_provenance import validate_stop_data_provenance


class StopDataProvenanceTests(unittest.TestCase):
    def _write_fixture(
        self,
        root: Path,
        *,
        stop_digest: str = "aaa",
        artifact_digest: str = "aaa",
        include_provenance: bool = True,
    ) -> None:
        stop_data = root / "stop-data"
        stop_data.mkdir()
        manifest = {"version": "v1", "releaseID": "release-a"}
        if include_provenance:
            manifest["sourceArtifacts"] = {"kyiv": {"sha256": stop_digest, "size": 10}}
        (stop_data / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (root / "artifacts.json").write_text(
            json.dumps({
                "sources": {},
                "external": {"kyiv": {"path": "/tmp/kyiv.zip", "sha256": artifact_digest, "size": 10}},
            }),
            encoding="utf-8",
        )

    def test_matching_release_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_fixture(root)
            validate_stop_data_provenance(root / "stop-data", root / "artifacts.json", "release-a")

    def test_fresh_gtfs_against_stale_stop_data_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_fixture(root, stop_digest="old", artifact_digest="fresh")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                validate_stop_data_provenance(root / "stop-data", root / "artifacts.json", "release-a")

    def test_missing_stop_data_provenance_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_fixture(root, include_provenance=False)
            with self.assertRaisesRegex(ValueError, "no sourceArtifacts provenance"):
                validate_stop_data_provenance(root / "stop-data", root / "artifacts.json", "release-a")

    def test_release_id_mismatch_fails_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_fixture(root)
            with self.assertRaisesRegex(ValueError, "release mismatch"):
                validate_stop_data_provenance(root / "stop-data", root / "artifacts.json", "release-b")


if __name__ == "__main__":
    unittest.main()
