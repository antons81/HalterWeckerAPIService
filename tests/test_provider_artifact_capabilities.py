import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from provider_artifact_capabilities import (  # noqa: E402
    _incremental_sources_path,
    PERSISTENT_NORMALIZED,
    HYBRID_RUNTIME,
    SHARD_RUNTIME,
    STATIC_PROVIDER,
    STRUCTURAL_PROVIDER,
    STRUCTURAL_SUFFICIENT,
    provider_artifact_eligible,
    provider_capability,
    provider_artifact_strategy,
)


class ProviderArtifactCapabilityTests(unittest.TestCase):
    def test_flat_container_layout_includes_germany_capabilities(self) -> None:
        self.assertEqual(_incremental_sources_path(Path("/")), Path("/app/config/incremental-provider-sources.json"))
        dockerfile = (REPOSITORY_ROOT / "services/static-departures.Dockerfile").read_text()
        self.assertIn("COPY config/incremental-provider-sources.json /app/config/incremental-provider-sources.json", dockerfile)
        with patch("provider_artifact_capabilities._sources_path", return_value=REPOSITORY_ROOT / "config/external-gtfs-sources.json"), \
             patch("provider_artifact_capabilities._incremental_sources_path", return_value=REPOSITORY_ROOT / "config/incremental-provider-sources.json"):
            self.assertTrue(provider_capability(Path("/"), "germany", HYBRID_RUNTIME))

    def test_enabled_providers_are_explicit_and_unlisted_providers_fail_closed(self) -> None:
        for provider_id in ("israel-mot", "ttc-surface", "ttc-subway", "germany"):
            self.assertTrue(provider_artifact_eligible(REPOSITORY_ROOT, provider_id))
            self.assertTrue(provider_capability(REPOSITORY_ROOT, provider_id, HYBRID_RUNTIME))
        self.assertFalse(provider_capability(REPOSITORY_ROOT, "swiss", STATIC_PROVIDER))
        self.assertFalse(provider_capability(REPOSITORY_ROOT, "swiss", HYBRID_RUNTIME))
        self.assertFalse(provider_capability(REPOSITORY_ROOT, "unknown-provider", SHARD_RUNTIME))

    def test_targeted_structural_providers_do_not_require_normalized_artifacts(self) -> None:
        for provider_id in (
            "cta-chicago",
            "stm-montreal",
            "sweden",
            "mbta-boston",
            "poland-warsaw",
            "poland-wkd",
            "511-bay-area",
            "australia-translink-seq",
            "australia-transport-nsw",
            "norway",
        ):
            self.assertFalse(provider_artifact_eligible(REPOSITORY_ROOT, provider_id))
            self.assertEqual(
                provider_artifact_strategy(REPOSITORY_ROOT, provider_id),
                STRUCTURAL_SUFFICIENT,
            )

    def test_capability_strategy_regression_matrix(self) -> None:
        for provider_id in ("israel-mot", "ttc-surface", "ttc-subway", "germany"):
            self.assertEqual(
                provider_artifact_strategy(REPOSITORY_ROOT, provider_id),
                "normalized-required",
            )
        for provider_id in (
            "cta-chicago",
            "stm-montreal",
            "sweden",
            "mbta-boston",
            "poland-warsaw",
            "poland-wkd",
            "511-bay-area",
            "australia-translink-seq",
            "australia-transport-nsw",
            "norway",
        ):
            self.assertEqual(
                provider_artifact_strategy(REPOSITORY_ROOT, provider_id),
                STRUCTURAL_SUFFICIENT,
            )
        self.assertEqual(
            provider_artifact_strategy(REPOSITORY_ROOT, "ireland"),
            "unsupported",
        )

    def test_malformed_capability_configuration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            registry = root / "config" / "external-gtfs-sources.json"
            registry.write_text(
                json.dumps(
                    [{"id": "fixture", "artifactCapabilities": {"unexpected": True}}]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                provider_capability(root, "fixture", PERSISTENT_NORMALIZED)

    def test_structural_sufficient_strategy_requires_explicit_structural_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            registry = root / "config" / "external-gtfs-sources.json"
            registry.write_text(
                json.dumps(
                    [
                        {
                            "id": "fixture",
                            "artifactCapabilities": {
                                STRUCTURAL_PROVIDER: True,
                                STATIC_PROVIDER: True,
                                SHARD_RUNTIME: True,
                                HYBRID_RUNTIME: True,
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            self.assertFalse(provider_artifact_eligible(root, "fixture"))
            self.assertEqual(
                provider_artifact_strategy(root, "fixture"),
                STRUCTURAL_SUFFICIENT,
            )

            registry.write_text(
                json.dumps(
                    [
                        {
                            "id": "fixture",
                            "artifactCapabilities": {
                                STRUCTURAL_PROVIDER: True,
                                STATIC_PROVIDER: True,
                                SHARD_RUNTIME: True,
                                HYBRID_RUNTIME: False,
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                provider_artifact_strategy(root, "fixture"),
                "unsupported",
            )

            registry.write_text(
                json.dumps(
                    [
                        {
                            "id": "fixture",
                            "artifactCapabilities": {
                                PERSISTENT_NORMALIZED: "yes",
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                provider_capability(root, "fixture", PERSISTENT_NORMALIZED)


if __name__ == "__main__":
    unittest.main()
