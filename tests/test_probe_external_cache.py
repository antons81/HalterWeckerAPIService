import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import external_build_cache  # noqa: E402
import probe_external_cache  # noqa: E402


class ProbeExternalCacheTests(unittest.TestCase):
    def test_probe_uses_the_same_merge_group_inputs_as_runtime(self) -> None:
        source = {"id": "poland-warsaw", "mergeGroup": "warsaw-group"}
        sources = {
            "poland-wkd": {"id": "poland-wkd", "mergeGroup": "warsaw-group"},
            "poland-warsaw": source,
        }
        self.assertEqual(
            probe_external_cache._merge_group_members(source, sources),
            ("poland-wkd", "poland-warsaw"),
        )

    def test_ttc_group_uses_registry_order_without_probe_sorting(self) -> None:
        source = {"id": "ttc-surface", "mergeGroup": "toronto"}
        sources = {
            "ttc-surface": source,
            "ttc-subway": {"id": "ttc-subway", "mergeGroup": "toronto"},
        }
        self.assertEqual(
            probe_external_cache._merge_group_members(source, sources),
            ("ttc-surface", "ttc-subway"),
        )

    def test_snapshot_sha_is_used_instead_of_mutable_current_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = Path(temporary)
            provider_id = "israel-mot"
            expected_sha = "a" * 64
            snapshot = {provider_id: {"sha256": expected_sha}}
            self.assertEqual(
                probe_external_cache._raw_sha256(
                    cache_root,
                    provider_id,
                    snapshot,
                ),
                expected_sha,
            )

    def test_probe_and_runtime_build_the_same_key(self) -> None:
        source = {
            "id": "fixture",
            "namespace": "fixture",
            "cities": "config/cities.json",
        }
        sources = {"fixture": source}
        raw_sha = "b" * 64
        with mock.patch.object(
            external_build_cache,
            "builder_fingerprint",
            return_value="c" * 64,
        ):
            runtime_key = external_build_cache.cache_key(
                repository_root=REPOSITORY_ROOT,
                provider_id="fixture",
                raw_sha256=raw_sha,
                source=source,
                city_id="fixture",
                city_ids=("fixture",),
                merge_group_members=(),
            )
            probe_key = external_build_cache.cache_key(
                repository_root=REPOSITORY_ROOT,
                provider_id="fixture",
                raw_sha256=raw_sha,
                source=source,
                city_id="fixture",
                city_ids=("fixture",),
                merge_group_members=probe_external_cache._merge_group_members(
                    source,
                    sources,
                ),
            )
        self.assertEqual(probe_key, runtime_key)


if __name__ == "__main__":
    unittest.main()
