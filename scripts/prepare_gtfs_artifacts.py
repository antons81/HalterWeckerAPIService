#!/usr/bin/env python3
"""Resolve configured raw GTFS feeds to validated persistent local artifacts."""

from __future__ import annotations

import argparse
import json
import os
import time
import zipfile
from pathlib import Path

from artifact_provenance import artifact_provenance, immutable_file_path
from external_gtfs import (
    authenticated_external_request,
    configured_external_url,
    external_gtfs_resilience_policy,
    load_external_gtfs_sources,
    parse_external_gtfs_url_args,
    source_classification,
    validate_kyiv_gtfs_archive,
)
from dynamic_resource_resolver import resolve_gtfs_resource
from gtfs_source_cache import DEFAULT_CACHE_ROOT, ArtifactResult, GTFSArtifactCache
from ireland_artifact_snapshot import capture_ireland_snapshot


def safe_error_reason(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, int):
        return f"HTTP {code}"
    if isinstance(error, TimeoutError):
        return "timeout"
    return type(error).__name__


def artifact_payload(
    result: ArtifactResult,
    *,
    release_root: Path | None = None,
) -> dict[str, object]:
    snapshot = None
    if result.source_id == "ireland" and result.path.is_dir():
        if release_root is None:
            raise ValueError(
                "Ireland local directory requires a release-local snapshot root"
            )
        snapshot = capture_ireland_snapshot(result.path, release_root)
        result_path = snapshot.path
        digest = snapshot.sha256
        size = snapshot.size
    else:
        result_path = result.path
        digest = None
        size = None
    state = result.state or {}
    if digest is None:
        digest = state.get("sha256")
        size = state.get("size")
    if result_path.is_dir() or not isinstance(digest, str) or not digest:
        digest, size = artifact_provenance(result_path)
    if not isinstance(digest, str) or not digest:
        raise ValueError(
            f"GTFS artifact {result.source_id} has no validated SHA-256 provenance."
        )
    if not isinstance(size, int) or size <= 0:
        raise ValueError(
            f"GTFS artifact {result.source_id} has no validated size provenance."
        )
    published_path = immutable_file_path(result_path, digest)
    payload = {
        "path": str(published_path),
        "status": result.status,
        "sha256": digest,
        "size": size,
    }
    return payload


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
    retry_attempts: int = 1,
    validator=None,
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
            retry_attempts=retry_attempts,
            validator=validator,
        )
    except Exception as error:
        duration = time.monotonic() - started
        print(
            f"[GTFSCache] source={source_id} stage=resolve "
            f"status=failed duration={duration:.2f}s reason={safe_error_reason(error)}"
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
    parser.add_argument(
        "--release-root",
        type=Path,
        help="Release directory used for the Ireland local-source snapshot.",
    )
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
        classification = source_classification(source)
        local_path = str(source.get("localPath") or "").strip()
        if local_path and source_id not in external_urls:
            path = Path(local_path)
            local_kind = validate_local_gtfs_path(source_id, path)
            result["external"][source_id] = artifact_payload(
                ArtifactResult(
                    source_id,
                    path,
                    "local",
                    f"local path ({local_kind})",
                ),
                release_root=args.release_root,
            )
            print(
                f"[GTFSCache] source={source_id} stage=resolve "
                f"status=local path={local_kind}"
            )
            continue
        configured_url = configured_external_url(source)
        dynamic_configuration = source.get("dynamicResource")
        if not configured_url and isinstance(dynamic_configuration, dict):
            configured_url = str(dynamic_configuration.get("metadataURL") or "").strip()
        url = external_urls.get(source_id, configured_url)
        if not url:
            reason = f"no URL or localPath configured for {source_id}"
            if classification == "required":
                raise ValueError(f"Required external GTFS source {source_id} has {reason}.")
            result["external"][source_id] = {
                "path": "",
                "status": "skipped",
                "classification": classification,
                "reason": reason,
            }
            print(
                f"[GTFSCache] source={source_id} stage=resolve status=skipped "
                f"classification={classification} reason={reason}"
            )
            continue
        dynamic_resource = None
        if source_id not in external_urls and isinstance(dynamic_configuration, dict):
            dynamic_resource = resolve_gtfs_resource(source)
        request_url = dynamic_resource.url if dynamic_resource is not None else url
        try:
            request_url, headers = authenticated_external_request(
                source_id, request_url, environ=os.environ
            )
        except ValueError:
            if classification == "required" or not bool(source.get("allowStale", True)):
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
        resilience_policy = external_gtfs_resilience_policy(source)
        artifact = resolve_one(
            cache,
            source_id,
            request_url,
            headers=headers,
            allow_stale=(
                bool(resilience_policy["allowStale"])
                if resilience_policy is not None
                else bool(source.get("allowStale", True))
            ),
            state_url=url,
            source_version=(
                {"dynamicVersion": dynamic_resource.version}
                if dynamic_resource is not None
                else None
            ),
            metadata_probe=(
                bool(resilience_policy["metadataProbe"])
                if resilience_policy is not None
                else preflight == "head"
            ),
            retry_attempts=(
                int(resilience_policy["retryAttempts"])
                if resilience_policy is not None
                else 1
            ),
            validator=(
                validate_kyiv_gtfs_archive
                if resilience_policy is not None
                and bool(resilience_policy["requireDataRows"])
                else None
            ),
        )
        result["external"][source_id] = artifact_payload(artifact)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
