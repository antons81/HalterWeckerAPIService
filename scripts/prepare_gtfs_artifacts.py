#!/usr/bin/env python3
"""Resolve configured raw GTFS feeds to validated persistent local artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import zipfile
from pathlib import Path

from external_gtfs import (
    authenticated_external_request,
    load_external_gtfs_sources,
    parse_external_gtfs_url_args,
)
from gtfs_source_cache import DEFAULT_CACHE_ROOT, ArtifactResult, GTFSArtifactCache


def directory_artifact_provenance(path: Path) -> tuple[str, int]:
    digest_builder = hashlib.sha256()
    total_size = 0
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        raise ValueError(f"GTFS directory artifact is empty: {path}")
    for file_path in files:
        relative_path = file_path.relative_to(path).as_posix().encode("utf-8")
        digest_builder.update(len(relative_path).to_bytes(8, "big"))
        digest_builder.update(relative_path)
        file_size = file_path.stat().st_size
        total_size += file_size
        digest_builder.update(file_size.to_bytes(8, "big"))
        with file_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest_builder.update(chunk)
    return digest_builder.hexdigest(), total_size


def artifact_payload(result: ArtifactResult) -> dict[str, object]:
    state = result.state or {}
    digest = state.get("sha256")
    size = state.get("size")
    if result.path.is_dir():
        digest, size = directory_artifact_provenance(result.path)
    if not isinstance(digest, str) or not digest:
        raise ValueError(
            f"GTFS artifact {result.source_id} has no validated SHA-256 provenance."
        )
    if not isinstance(size, int) or size <= 0:
        raise ValueError(
            f"GTFS artifact {result.source_id} has no validated size provenance."
        )
    return {
        "path": str(result.path),
        "status": result.status,
        "sha256": digest,
        "size": size,
    }


def resolve_one(
    cache: GTFSArtifactCache,
    source_id: str,
    url: str,
    *,
    allow_stale: bool,
    headers: dict[str, str] | None = None,
    source_version: dict[str, object] | None = None,
    state_url: str | None = None,
    metadata_probe: bool = True,
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
            metadata_probe=metadata_probe,
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


def validate_local_gtfs_path(source_id: str, path: Path) -> str:
    if not path.exists():
        raise ValueError(
            f"Local external GTFS source {source_id} is missing: {path}"
        )
    if path.is_file():
        if path.stat().st_size == 0:
            raise ValueError(
                f"Local external GTFS source {source_id} is empty: {path}"
            )
        try:
            with zipfile.ZipFile(path) as archive:
                corrupt_member = archive.testzip()
        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise ValueError(
                f"Local external GTFS source {source_id} is invalid: {path}"
            ) from error
        if corrupt_member:
            raise ValueError(
                f"Local external GTFS source {source_id} is corrupt: {path}"
            )
        return "file"
    if path.is_dir():
        return "directory"
    raise ValueError(
        f"Local external GTFS source {source_id} is not a file or directory: {path}"
    )


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
        result["sources"][source_id] = artifact_payload(artifact)

    if args.nl_gtfs_url.strip():
        try:
            artifact = resolve_one(cache, "netherlands", args.nl_gtfs_url, allow_stale=True)
            result["sources"]["netherlands"] = artifact_payload(artifact)
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
            local_kind = validate_local_gtfs_path(source_id, path)
            result["external"][source_id] = {
                "path": str(path),
                "status": "local",
            }
            print(
                f"[GTFSCache] source={source_id} stage=resolve "
                f"status=local path={local_kind}"
            )
            continue
        url = external_urls.get(source_id, str(configured or "")).strip()
        if not url:
            continue
        try:
            request_url, headers = authenticated_external_request(
                source_id, url, environ=os.environ
            )
        except ValueError:
            if not bool(source.get("allowStale", True)):
                raise
            cached_path = Path(args.cache_root) / source_id / "current.zip"
            artifact = cache.resolve(
                source_id,
                str(cached_path),
                allow_stale=True,
                metadata_probe=False,
            )
            print(
                f"[GTFSCache] source={source_id} stage=resolve "
                "status=preserved-stale reason=authentication unavailable"
            )
            result["external"][source_id] = artifact_payload(artifact) | {
                "reason": "authentication unavailable",
            }
            continue
        preflight = str(source.get("preflight", "head"))
        artifact = resolve_one(
            cache,
            source_id,
            request_url,
            headers=headers,
            allow_stale=bool(source.get("allowStale", True)),
            state_url=url,
            metadata_probe=preflight == "head",
        )
        result["external"][source_id] = artifact_payload(artifact)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
