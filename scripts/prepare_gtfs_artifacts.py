#!/usr/bin/env python3
"""Resolve configured raw GTFS feeds to validated persistent local artifacts."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from external_gtfs import (
    authenticated_external_request,
    load_external_gtfs_sources,
    parse_external_gtfs_url_args,
)
from gtfs_source_cache import DEFAULT_CACHE_ROOT, ArtifactResult, GTFSArtifactCache


def resolve_one(
    cache: GTFSArtifactCache,
    source_id: str,
    url: str,
    *,
    allow_stale: bool,
    headers: dict[str, str] | None = None,
    source_version: dict[str, object] | None = None,
    state_url: str | None = None,
) -> ArtifactResult:
    started = time.monotonic()
    try:
        result = cache.resolve(
            source_id,
            url,
            headers=headers,
            source_version=source_version,
            allow_stale=allow_stale,
            state_url=state_url,
        )
    except Exception as error:
        duration = time.monotonic() - started
        print(
            f"[GTFSCache] source={source_id} stage=resolve "
            f"status=failed duration={duration:.2f}s reason={error}"
        )
        print(
            f"[GTFSCache] source={source_id} stage=download "
            f"status=failed duration={duration:.2f}s"
        )
        raise
    duration = time.monotonic() - started
    print(
        f"[GTFSCache] source={source_id} stage=resolve "
        f"status={result.status} duration={duration:.2f}s"
        + (f" reason={result.reason}" if result.reason else "")
    )
    download_status = "skipped" if result.status == "unchanged" else result.status
    print(
        f"[GTFSCache] source={source_id} stage=download "
        f"status={download_status} duration={duration:.2f}s"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", default=os.environ.get("GTFS_CACHE_ROOT", str(DEFAULT_CACHE_ROOT)))
    parser.add_argument("--gtfs-url", required=True)
    parser.add_argument("--swiss-gtfs-url", required=True)
    parser.add_argument("--nl-gtfs-url", default="")
    parser.add_argument("--external-sources", default="config/external-gtfs-sources.json")
    parser.add_argument("--external-gtfs-url", action="append", default=[])
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    cache = GTFSArtifactCache(args.cache_root)
    result: dict[str, object] = {"sources": {}, "external": {}, "nlFailure": None}

    for source_id, url, allow_stale in (
        ("germany", args.gtfs_url, True),
        ("swiss", args.swiss_gtfs_url, True),
    ):
        artifact = resolve_one(cache, source_id, url, allow_stale=allow_stale)
        result["sources"][source_id] = {"path": str(artifact.path), "status": artifact.status}

    if args.nl_gtfs_url.strip():
        try:
            artifact = resolve_one(cache, "netherlands", args.nl_gtfs_url, allow_stale=True)
            result["sources"]["netherlands"] = {"path": str(artifact.path), "status": artifact.status}
        except Exception as error:
            result["nlFailure"] = str(error)
            result["sources"]["netherlands"] = {"path": "", "status": "failed", "reason": str(error)}
            print(f"[GTFSCache] source=netherlands stage=resolve status=failed reason={error}")

    external_urls = parse_external_gtfs_url_args(args.external_gtfs_url)
    for source in load_external_gtfs_sources(Path(args.external_sources)):
        source_id = str(source["id"])
        configured = source.get("url")
        local_path = str(source.get("localPath") or "").strip()
        if local_path and source_id not in external_urls:
            path = Path(local_path)
            if not path.is_dir():
                raise ValueError(
                    f"Local external GTFS source {source_id} is missing: {path}"
                )
            result["external"][source_id] = {
                "path": str(path),
                "status": "local",
            }
            print(
                f"[GTFSCache] source={source_id} stage=resolve "
                "status=local path=directory"
            )
            continue
        url = external_urls.get(source_id, str(configured or "")).strip()
        if not url:
            continue
        request_url, headers = authenticated_external_request(source_id, url, environ=os.environ)
        artifact = resolve_one(
            cache,
            source_id,
            request_url,
            headers=headers,
            allow_stale=True,
            state_url=url,
        )
        result["external"][source_id] = {"path": str(artifact.path), "status": artifact.status}

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
