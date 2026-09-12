import sys
import sqlite3
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tests"))

import run_incremental_provider_pipeline as incremental  # noqa: E402
from test_static_provider_artifact import StaticProviderArtifactTests  # noqa: E402


class IncrementalProviderPipelineTests(unittest.TestCase):
    def test_readiness_provider_rows_use_explicit_provider_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            structural_database = Path(temporary) / "structural.sqlite"
            with sqlite3.connect(structural_database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE provider_city_stops(
                        provider_id TEXT NOT NULL,
                        city_id TEXT NOT NULL,
                        stop_id TEXT NOT NULL
                    );
                    CREATE TABLE provider_city_modes(
                        provider_id TEXT NOT NULL,
                        city_id TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        stop_id_prefix TEXT NOT NULL,
                        identifier_prefix TEXT NOT NULL
                    );
                    INSERT INTO provider_city_stops VALUES
                        ('israel-mot', 'israel', '100');
                    INSERT INTO provider_city_modes VALUES
                        ('israel-mot', 'israel', 'bus', 'Asia/Jerusalem', '', '');
                    """
                )

            stop_rows, mode_rows = incremental._provider_rows(
                "israel-mot",
                structural_database,
            )
            self.assertEqual(incremental._first_stop(stop_rows, "israel"), "100")
            self.assertEqual(mode_rows[0][0], "israel-mot")

            with self.assertRaisesRegex(ValueError, "provider=unknown-provider"):
                incremental._provider_rows("unknown-provider", structural_database)

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

    def test_published_directory_is_used_after_staging_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / ".release-a.incremental-test"
            published = root / "candidate" / "release-a"
            staging.mkdir(parents=True)
            published.mkdir(parents=True)
            assembly = SimpleNamespace(release_directory=published)

            resolved = incremental._published_release_directory(
                assembly,
                staging_directory=staging,
            )
            shutil.rmtree(staging)

            self.assertEqual(resolved, published.resolve())
            self.assertTrue(resolved.is_dir())
            self.assertNotIn(".release-a.incremental", str(resolved))

    def test_staging_path_is_rejected_if_atomic_publish_did_not_happen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary) / ".release-a.incremental-test"
            staging.mkdir()

            with self.assertRaisesRegex(FileNotFoundError, "published incremental candidate"):
                incremental._published_release_directory(
                    SimpleNamespace(release_directory=staging),
                    staging_directory=staging,
                )

    def test_staging_parent_is_created_before_incremental_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            releases_root = Path(temporary) / "releases" / "incremental" / "run-1"

            staging = incremental._create_incremental_staging_directory(
                releases_root,
                "release-a",
            )

            self.assertTrue(staging.is_dir())
            self.assertEqual(staging.parent, releases_root.parent)
            self.assertTrue(releases_root.parent.is_dir())

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
