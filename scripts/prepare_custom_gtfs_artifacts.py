#!/usr/bin/env python3
"""Resolve the canonical VBB/RNV inputs used by the German stop-data build."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from artifact_provenance import artifact_provenance, immutable_file_path
from gtfs_source_cache import DEFAULT_CACHE_ROOT, GTFSArtifactCache


def resolve_source(
    cache: GTFSArtifactCache,
    source_id: str,
    url: str,
) -> dict[str, object]:
    artifact = cache.resolve(
        source_id,
        url,
        allow_stale=False,
        metadata_probe=True,
    )
    state = artifact.state or {}
    digest = state.get("sha256")
    size = state.get("size")
    if not isinstance(digest, str) or not digest:
        digest, size = artifact_provenance(artifact.path)
    if not isinstance(size, int) or size <= 0:
        raise ValueError(f"Custom GTFS source {source_id} has no valid size provenance")
    published_path = immutable_file_path(artifact.path, digest)
    return {
        "sourceID": source_id,
        "path": str(published_path),
        "sha256": digest,
        "size": size,
        "status": artifact.status,
        "origin": url,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=str(DEFAULT_CACHE_ROOT))
    parser.add_argument("--vbb-url", required=True)
    parser.add_argument("--rnv-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    cache = GTFSArtifactCache(Path(args.cache_root))
    payload = {
        "sources": {
            "vbb": resolve_source(cache, "vbb", args.vbb_url.strip()),
            "rnv": resolve_source(cache, "rnv", args.rnv_url.strip()),
        }
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
