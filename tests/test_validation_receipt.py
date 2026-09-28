import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = TESTS_ROOT.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "services"))

from validation_receipt import (  # noqa: E402
    ValidationReceiptError,
    validate_validation_receipt,
    write_validation_receipt,
)


class ValidationReceiptTests(unittest.TestCase):
    provider_id = "fixture-provider"

    def _build_fixture(self, root: Path) -> Path:
        structural_dir = root / "providers" / self.provider_id / "structural"
        temporal_dir = root / "providers" / self.provider_id / "temporal"
        stop_data_dir = root / "stop-data"
        structural_dir.mkdir(parents=True)
        temporal_dir.mkdir(parents=True)
        stop_data_dir.mkdir()

        common_path = root / "common.sqlite"
        common_path.write_bytes(b"common")
        (stop_data_dir / "manifest.json").write_text(
            '{"status":"complete"}\n',
            encoding="utf-8",
        )
        references = {}
        for artifact_type, directory in (
            ("structural", structural_dir),
            ("temporal", temporal_dir),
        ):
            database_path = directory / "provider.sqlite"
            manifest_path = directory / "manifest.json"
            database_path.write_bytes(artifact_type.encode("utf-8"))
            manifest_path.write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "providerID": self.provider_id,
                        "artifactType": artifact_type,
                        "artifactKey": f"{artifact_type}-key",
                    }
                ),
                encoding="utf-8",
            )
            references[artifact_type] = {
                "path": database_path.relative_to(root).as_posix(),
                "manifestPath": manifest_path.relative_to(root).as_posix(),
                "artifactKey": f"{artifact_type}-key",
                "sha256": "validated-by-upstream",
                "size": database_path.stat().st_size,
            }

        payload = {
            "releaseID": "fixture-release",
            "common": {
                "path": "common.sqlite",
                "sha256": "validated-by-upstream",
                "size": common_path.stat().st_size,
            },
            "stopData": {
                "path": "stop-data",
                "manifestPath": "stop-data/manifest.json",
            },
            "providers": {self.provider_id: references},
        }
        (root / "release.json").write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
        return root

    def test_valid_receipt_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build_fixture(Path(temporary))
            write_validation_receipt(root, provider_ids=(self.provider_id,))
            receipt = validate_validation_receipt(
                root,
                provider_ids=(self.provider_id,),
            )
            self.assertEqual(receipt["result"], "PASS")

    def test_relative_stop_data_symlink_stays_portable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build_fixture(Path(temporary))
            stop_data = root / "stop-data"
            target = root / "stop-data-target"
            shutil.move(stop_data, target)
            stop_data.symlink_to("stop-data-target", target_is_directory=True)
            write_validation_receipt(root, provider_ids=(self.provider_id,))
            receipt = json.loads((root / "validation-receipt.json").read_text())
            self.assertEqual(receipt["stopData"]["path"], "stop-data")
            validate_validation_receipt(root, provider_ids=(self.provider_id,))

    def test_changed_release_manifest_rejects_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build_fixture(Path(temporary))
            write_validation_receipt(root, provider_ids=(self.provider_id,))
            payload = json.loads((root / "release.json").read_text(encoding="utf-8"))
            payload["releaseID"] = "changed-release"
            (root / "release.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValidationReceiptError):
                validate_validation_receipt(root, provider_ids=(self.provider_id,))

    def test_changed_artifact_manifest_rejects_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build_fixture(Path(temporary))
            write_validation_receipt(root, provider_ids=(self.provider_id,))
            manifest_path = root / "providers" / self.provider_id / "structural" / "manifest.json"
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["marker"] = "changed"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValidationReceiptError):
                validate_validation_receipt(root, provider_ids=(self.provider_id,))

    def test_missing_artifact_rejects_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._build_fixture(Path(temporary))
            write_validation_receipt(root, provider_ids=(self.provider_id,))
            database_path = root / "providers" / self.provider_id / "temporal" / "provider.sqlite"
            database_path.unlink()
            with self.assertRaises(ValidationReceiptError):
                validate_validation_receipt(root, provider_ids=(self.provider_id,))


if __name__ == "__main__":
    unittest.main()
