import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tests"))

import run_incremental_provider_pipeline as incremental  # noqa: E402
from test_static_provider_artifact import StaticProviderArtifactTests  # noqa: E402


class IncrementalProviderPipelineTests(unittest.TestCase):
    def test_temporal_window_requires_next_day(self) -> None:
        with self.assertRaises(ValueError):
            incremental.service_dates(
                valid_from=date(2026, 9, 10),
                valid_through=date(2026, 9, 10),
            )

    def test_temporal_window_is_coherent_and_rolling(self) -> None:
        dates = incremental.service_dates(
            valid_from=date(2026, 9, 10),
            window_days=15,
        )
        self.assertEqual(dates[0], date(2026, 9, 10))
        self.assertEqual(dates[-1], date(2026, 9, 24))
        self.assertEqual(len(dates), 15)

    def test_structural_manifest_does_not_require_temporal_window(self) -> None:
        structural_manifest = {
            "artifactType": "structural",
            "dependencies": {
                "stopDataFingerprint": "stop-data-key",
                "structuralSchemaFingerprint": "schema-key",
            },
        }
        temporal_manifest = {
            "artifactType": "temporal",
            "dependencies": {
                "validFrom": "2026-09-11",
                "validThrough": "2026-09-25",
            },
        }

        self.assertNotIn("validFrom", structural_manifest["dependencies"])
        self.assertNotIn("validThrough", structural_manifest["dependencies"])
        incremental._validate_temporal_window(
            provider_id="israel-mot",
            temporal_manifest=temporal_manifest,
            dates=[date(2026, 9, 11), date(2026, 9, 25)],
        )

    def test_temporal_window_mismatch_fails_closed(self) -> None:
        temporal_manifest = {
            "dependencies": {
                "validFrom": "2026-09-12",
                "validThrough": "2026-09-25",
            }
        }
        with self.assertRaisesRegex(ValueError, "temporal validFrom mismatch"):
            incremental._validate_temporal_window(
                provider_id="israel-mot",
                temporal_manifest=temporal_manifest,
                dates=[date(2026, 9, 11), date(2026, 9, 25)],
            )

    def test_temporal_window_missing_fields_fails_closed(self) -> None:
        temporal_manifest = {"dependencies": {"validFrom": "2026-09-11"}}
        with self.assertRaisesRegex(ValueError, "temporal validThrough mismatch"):
            incremental._validate_temporal_window(
                provider_id="israel-mot",
                temporal_manifest=temporal_manifest,
                dates=[date(2026, 9, 11), date(2026, 9, 25)],
            )

    def test_raw_artifact_provenance_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "feed.zip"
            raw.write_bytes(b"fixture")
            payload = {"external": {"israel-mot": {"path": str(raw), "sha256": "wrong", "size": 7}}}
            with self.assertRaises(ValueError):
                incremental._raw_entry(payload, "israel-mot")

    def test_provider_scope_is_fixed_to_phase_six_pilot(self) -> None:
        with self.assertRaises(ValueError):
            incremental.build_incremental_candidate(
                repository_root=REPOSITORY_ROOT,
                release_id="release-a",
                releases_root=Path(tempfile.mkdtemp()),
                stop_data_root=Path(tempfile.mkdtemp()),
                gtfs_artifacts_path=Path(tempfile.mktemp()),
                normalized_cache_root=Path(tempfile.mkdtemp()),
                static_artifact_root=Path(tempfile.mkdtemp()),
                dates=[date(2026, 9, 10), date(2026, 9, 11)],
                provider_ids=("israel-mot",),
            )

    def test_candidate_release_directory_is_distinct_from_source_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_generation = root / "releases" / "release-a"
            candidate_root = root / "releases" / "incremental"
            source_generation.mkdir(parents=True)
            candidate_root.mkdir(parents=True)
            self.assertNotEqual(source_generation, candidate_root / "release-a")
            self.assertFalse((candidate_root / "release-a").exists())

    def test_provider_layers_reuse_immutable_artifacts_and_roll_temporal_only(self) -> None:
        helper = StaticProviderArtifactTests()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed, source, cities, stop_data = helper._prepare_inputs(root)
            first = helper._build_artifacts(
                root,
                feed,
                source,
                cities,
                stop_data,
                [date(2026, 9, 10), date(2026, 9, 11)],
            )
            second = helper._build_artifacts(
                root,
                feed,
                source,
                cities,
                stop_data,
                [date(2026, 9, 10), date(2026, 9, 11)],
            )
            rolling = helper._build_artifacts(
                root,
                feed,
                source,
                cities,
                stop_data,
                [date(2026, 9, 11), date(2026, 9, 12)],
            )
            self.assertEqual(first.structural.status, "MISS")
            self.assertEqual(first.temporal.status, "MISS")
            self.assertEqual(second.structural.status, "HIT")
            self.assertEqual(second.temporal.status, "HIT")
            self.assertEqual(rolling.structural.status, "HIT")
            self.assertEqual(rolling.temporal.status, "MISS")
            self.assertEqual(second.structural.bytes_written, 0)
            self.assertEqual(second.temporal.bytes_written, 0)


if __name__ == "__main__":
    unittest.main()
