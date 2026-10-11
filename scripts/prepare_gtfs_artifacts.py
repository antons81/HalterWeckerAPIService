#!/usr/bin/env python3
"""Resolve configured raw GTFS feeds to validated persistent local artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

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
from gtfs_source_cache import (
    DEFAULT_CACHE_ROOT,
    ArtifactResult,
    GTFSArtifactCache,
    validate_gtfs_archive,
)
from ireland_artifact_snapshot import (
    capture_ireland_archive_snapshot,
    capture_ireland_snapshot,
)
from raw_snapshot import load_raw_snapshot_manifest, validate_raw_snapshot_entry


KYIV_SOURCE_ID = "kyiv"
GTFS_PRIMARY_KEYS = {
    "agency.txt": ("agency_id",),
    "routes.txt": ("route_id",),
    "stops.txt": ("stop_id",),
    "trips.txt": ("trip_id",),
    "stop_times.txt": ("trip_id", "stop_sequence"),
    "calendar.txt": ("service_id",),
    "calendar_dates.txt": ("service_id", "date"),
    "shapes.txt": ("shape_id", "shape_pt_sequence"),
    "frequencies.txt": ("trip_id", "start_time"),
    "fare_attributes.txt": ("fare_id",),
    "transfers.txt": ("from_stop_id", "to_stop_id", "from_route_id", "to_route_id", "from_trip_id", "to_trip_id"),
    "feed_info.txt": ("_single_record_",),
}
REQUIRED_BUNDLE_TABLES = frozenset(
    {"agency.txt", "routes.txt", "stops.txt", "trips.txt", "stop_times.txt"}
)


def gtfs_calendar_horizon(path: Path) -> date | None:
    """Return the last service date represented by a GTFS archive."""
    horizons: list[date] = []
    with zipfile.ZipFile(path) as archive:
        members = {Path(name).name: name for name in archive.namelist() if not name.endswith("/")}
        calendar_name = members.get("calendar.txt")
        if calendar_name:
            with archive.open(calendar_name) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                reader = csv.DictReader(text)
                for row in reader:
                    if not any(str(row.get(day, "0")).strip() == "1" for day in (
                        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                    )):
                        continue
                    value = str(row.get("end_date", "")).strip()
                    if re.fullmatch(r"\d{8}", value):
                        horizons.append(datetime.strptime(value, "%Y%m%d").date())
        dates_name = members.get("calendar_dates.txt")
        if dates_name:
            with archive.open(dates_name) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                for row in csv.DictReader(text):
                    value = str(row.get("date", "")).strip()
                    if str(row.get("exception_type", "")).strip() == "1" and re.fullmatch(r"\d{8}", value):
                        horizons.append(datetime.strptime(value, "%Y%m%d").date())
    return max(horizons) if horizons else None


def merge_gtfs_feed_bundle(feed_paths: list[Path], output_path: Path) -> None:
    """Merge same-schema GTFS feeds, deduplicating identical primary keys."""
    if len(feed_paths) < 2:
        raise ValueError("GTFS feed bundle requires at least two component feeds")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as output:
            with zipfile.ZipFile(feed_paths[0]) as first:
                member_names = sorted(
                    {Path(name).name for name in first.namelist() if name.lower().endswith(".txt")}
                )
            if not REQUIRED_BUNDLE_TABLES <= set(member_names):
                raise ValueError("GTFS bundle component is missing required tables")
            for component in feed_paths:
                with zipfile.ZipFile(component) as archive:
                    names = [Path(name).name for name in archive.namelist() if name.lower().endswith(".txt")]
                    if len(names) != len(set(names)):
                        raise ValueError(f"GTFS bundle component has duplicate table basenames: {component.name}")
                    if not REQUIRED_BUNDLE_TABLES <= set(names):
                        raise ValueError(f"GTFS bundle component is missing required tables: {component.name}")

            for table in member_names:
                headers: list[str] = []
                table_members: list[tuple[Path, str]] = []
                for component in feed_paths:
                    with zipfile.ZipFile(component) as archive:
                        match = next((name for name in archive.namelist()
                                      if not name.endswith("/") and Path(name).name == table), None)
                        if match is None:
                            continue
                        table_members.append((component, match))
                        with archive.open(match) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                            reader = csv.reader(text)
                            row_headers = next(reader, None)
                        if not row_headers or len(row_headers) != len(set(row_headers)):
                            raise ValueError(f"GTFS bundle table has invalid headers: {component.name}/{table}")
                        for field in row_headers:
                            if field not in headers:
                                headers.append(field)
                if not headers:
                    continue
                key_fields = GTFS_PRIMARY_KEYS.get(table, ())
                if key_fields and key_fields != ("_single_record_",) and not set(key_fields) <= set(headers):
                    raise ValueError(f"GTFS bundle table lacks primary-key columns: {table}")
                seen: dict[tuple[str, ...], str] = {}
                with output.open(table, "w") as binary:
                    text_output = io.TextIOWrapper(binary, encoding="utf-8", newline="")
                    writer = csv.writer(text_output, lineterminator="\n")
                    writer.writerow(headers)
                    try:
                        for component, member in table_members:
                            with zipfile.ZipFile(component) as archive, archive.open(member) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                                reader = csv.DictReader(text)
                                for row in reader:
                                    values = tuple(str(row.get(field, "") or "") for field in headers)
                                    if key_fields == ("_single_record_",):
                                        key = ("feed-info",)
                                    elif key_fields and all(str(row.get(field, "") or "") for field in key_fields):
                                        key = tuple(str(row.get(field, "") or "") for field in key_fields)
                                    else:
                                        key = values
                                    digest = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()
                                    previous = seen.get(key)
                                    if previous is not None:
                                        if previous != digest:
                                            raise ValueError(f"GTFS bundle has conflicting duplicate key in {table}: {key}")
                                        continue
                                    seen[key] = digest
                                    writer.writerow(values)
                        text_output.flush()
                    finally:
                        text_output.detach()
        validate_gtfs_archive(temporary)
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)


def source_calendar_local_date(timezone_name: str) -> date:
    return datetime.now(ZoneInfo(timezone_name)).date()


def _public_source_url(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def ensure_current_source_calendar(
    cache: GTFSArtifactCache,
    result: ArtifactResult,
    *,
    source_id: str,
    url: str,
    timezone_name: str,
    allow_stale: bool,
    headers: dict[str, str] | None = None,
    state_url: str | None = None,
    metadata_probe: bool = True,
    retry_attempts: int = 1,
    validator=None,
) -> ArtifactResult:
    """Force an unconditional refresh when cached service dates have expired."""
    if result.path.is_dir():
        return result
    today = source_calendar_local_date(timezone_name)
    horizon = gtfs_calendar_horizon(result.path)
    if horizon is None:
        raise ValueError(f"source={source_id} has no active GTFS service calendar at {result.path}")
    if horizon >= today:
        return result
    if result.status in {"updated", "snapshot", "local"}:
        raise ValueError(
            f"source={source_id} supplied an expired GTFS calendar through {horizon}; "
            f"configured upstream={_public_source_url(url) if url else result.path}"
        )
    if not url:
        raise ValueError(
            f"source={source_id} calendar expired on {horizon} and has no refresh URL; "
            f"local artifact={result.path}"
        )
    try:
        refreshed = resolve_one(
            cache, source_id, url, allow_stale=False, headers=headers,
            state_url=state_url, metadata_probe=metadata_probe,
            retry_attempts=retry_attempts, validator=validator,
            force_download=True,
        )
    except Exception as error:
        raise ValueError(
            f"source={source_id} calendar expired on {horizon}; fresh download failed "
            f"from {_public_source_url(url)}: {type(error).__name__}: {error}"
        ) from error
    refreshed_horizon = gtfs_calendar_horizon(refreshed.path)
    if refreshed_horizon is None or refreshed_horizon < today:
        horizon_text = refreshed_horizon.isoformat() if refreshed_horizon else "no active service dates"
        raise ValueError(
            f"source={source_id} upstream {_public_source_url(url)} still serves an expired "
            f"GTFS calendar through {horizon_text} (today={today})"
        )
    return refreshed


def _kyiv_stale_cache_seed(
    cache_root: Path,
) -> tuple[Path | None, dict[str, object] | None]:
    """Return a validated legacy-root Kyiv seed when the canonical cache is empty."""
    canonical_dir = cache_root / KYIV_SOURCE_ID
    canonical_current = canonical_dir / "current.zip"
    canonical_state = canonical_dir / "state.json"
    if canonical_current.exists() or canonical_state.exists():
        return None, None

    fallback_root = Path(
        os.environ.get("HALTEWECKER_KYIV_STALE_CACHE_ROOT", str(DEFAULT_CACHE_ROOT))
    )
    if fallback_root.resolve() == cache_root.resolve():
        return None, None

    fallback_dir = fallback_root / KYIV_SOURCE_ID
    seed_path = fallback_dir / "current.zip"
    state_path = fallback_dir / "state.json"
    if not seed_path.exists() and not state_path.exists():
        return None, None
    if not seed_path.is_file() or not state_path.is_file():
        raise ValueError("Kyiv stale cache is incomplete")

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("Kyiv stale cache manifest is unreadable") from error
    if not isinstance(state, dict):
        raise ValueError("Kyiv stale cache manifest is invalid")

    digest, size = artifact_provenance(seed_path)
    if (
        state.get("sourceID") != KYIV_SOURCE_ID
        or state.get("artifact") != "current.zip"
        or state.get("validated") is not True
        or state.get("sha256") != digest
        or state.get("size") != size
    ):
        raise ValueError("Kyiv stale cache provenance mismatch")
    validate_kyiv_gtfs_archive(seed_path)
    return seed_path, state


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
    elif result.source_id == "ireland" and result.path.is_file():
        if release_root is None:
            raise ValueError(
                "Ireland GTFS archive requires a release-local snapshot root"
            )
        snapshot = capture_ireland_archive_snapshot(result.path, release_root)
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
    published_path = (
        result_path
        if result.source_id == "ireland"
        else immutable_file_path(result_path, digest)
    )
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
    force_download: bool = False,
    seed_path: Path | None = None,
    seed_state: dict[str, object] | None = None,
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
            force_download=force_download,
            seed_path=seed_path,
            seed_state=seed_state,
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
    if source_id == KYIV_SOURCE_ID and result.status == "preserved-stale":
        downloaded_at = (result.state or {}).get("downloadedAt")
        cache_age = "unknown"
        if isinstance(downloaded_at, str):
            try:
                downloaded = datetime.fromisoformat(downloaded_at.replace("Z", "+00:00"))
                cache_age = f"{max(0, int((datetime.now(timezone.utc) - downloaded).total_seconds()))}s"
            except ValueError:
                pass
        print(
            f"[GTFSCache] provider=kyiv upstream_status=unavailable "
            f"source=cached stale=true cache_age={cache_age} "
            "status=PASS_WITH_STALE_CACHE"
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
    parser.add_argument(
        "--raw-snapshot-manifest",
        type=Path,
        help="Use immutable pinned raw inputs for the listed external providers.",
    )
    args = parser.parse_args()

    cache = GTFSArtifactCache(args.cache_root)
    result: dict[str, object] = {"sources": {}, "external": {}, "nlFailure": None}

    for source_id, url, allow_stale in (
        ("germany", args.gtfs_url, True),
        ("swiss", args.swiss_gtfs_url, True),
    ):
        artifact = resolve_one(cache, source_id, url, allow_stale=allow_stale)
        artifact = ensure_current_source_calendar(
            cache, artifact, source_id=source_id, url=url,
            timezone_name={"germany": "Europe/Berlin", "swiss": "Europe/Zurich"}[source_id],
            allow_stale=allow_stale,
        )
        result["sources"][source_id] = artifact_payload(artifact)

    if args.nl_gtfs_url.strip():
        artifact = resolve_one(cache, "netherlands", args.nl_gtfs_url, allow_stale=True)
        artifact = ensure_current_source_calendar(
            cache, artifact, source_id="netherlands", url=args.nl_gtfs_url,
            timezone_name="Europe/Amsterdam", allow_stale=True,
        )
        result["sources"]["netherlands"] = artifact_payload(artifact)

    external_urls = parse_external_gtfs_url_args(args.external_gtfs_url)
    raw_snapshot = (
        load_raw_snapshot_manifest(args.raw_snapshot_manifest)
        if args.raw_snapshot_manifest is not None
        else {}
    )
    for source in load_external_gtfs_sources(Path(args.external_sources)):
        source_id = str(source["id"])
        classification = source_classification(source)
        if source_id in raw_snapshot:
            pinned_path, _ = validate_raw_snapshot_entry(
                source_id,
                raw_snapshot[source_id],
            )
            if source.get("buildDepartures") is True and pinned_path.is_file():
                horizon = gtfs_calendar_horizon(pinned_path)
                timezone_name = str(source.get("timezone") or "Europe/Berlin")
                today = source_calendar_local_date(timezone_name)
                if horizon is None or horizon < today:
                    horizon_text = horizon.isoformat() if horizon else "no active service dates"
                    raise ValueError(
                        f"source={source_id} pinned GTFS calendar is expired through {horizon_text}; "
                        "the immutable snapshot cannot be refreshed"
                    )
            result["external"][source_id] = artifact_payload(
                ArtifactResult(
                    source_id,
                    pinned_path,
                    "snapshot",
                    "immutable raw snapshot",
                )
            )
            print(
                f"[GTFSCache] source={source_id} stage=resolve "
                f"status=pinned-snapshot path={pinned_path}"
            )
            continue
        local_path = str(source.get("localPath") or "").strip()
        configured_url = configured_external_url(source)
        dynamic_configuration = source.get("dynamicResource")
        if not configured_url and isinstance(dynamic_configuration, dict):
            configured_url = str(dynamic_configuration.get("metadataURL") or "").strip()
        url = external_urls.get(source_id, configured_url)
        feed_bundle = source.get("feedBundle") if source_id not in external_urls else None
        if isinstance(feed_bundle, list):
            if len(feed_bundle) < 2 or not args.release_root:
                raise ValueError(f"Required GTFS feed bundle {source_id} is incomplete or has no release root.")
            component_results: list[ArtifactResult] = []
            component_payloads: list[dict[str, object]] = []
            timezone_name = str(source.get("timezone") or "Europe/Berlin")
            for component in feed_bundle:
                if not isinstance(component, dict):
                    raise ValueError(f"Invalid GTFS feed bundle entry for {source_id}.")
                component_id = str(component.get("id") or "").strip()
                component_url = str(component.get("url") or "").strip()
                if not component_id or not component_url:
                    raise ValueError(f"GTFS feed bundle {source_id} has a component without ID or URL.")
                cache_id = f"{source_id}-{component_id}"
                artifact = resolve_one(cache, cache_id, component_url, allow_stale=True)
                artifact = ensure_current_source_calendar(
                    cache, artifact, source_id=cache_id, url=component_url,
                    timezone_name=timezone_name, allow_stale=True,
                )
                component_results.append(artifact)
                component_payloads.append(
                    artifact_payload(artifact)
                    | {"componentID": component_id, "sourceURL": _public_source_url(component_url),
                       "serviceThrough": (gtfs_calendar_horizon(artifact.path).isoformat()
                                          if gtfs_calendar_horizon(artifact.path) else None)}
                )
            bundle_path = args.release_root / "source-bundles" / f"{source_id}.zip"
            merge_gtfs_feed_bundle([item.path for item in component_results], bundle_path)
            bundle = artifact_payload(ArtifactResult(source_id, bundle_path, "updated", "merged official GTFS feeds"))
            bundle["componentSources"] = component_payloads
            result["external"][source_id] = bundle
            print(
                f"[GTFSCache] source={source_id} stage=feed-bundle status=updated "
                f"components={len(component_results)}"
            )
            continue
        if local_path and source_id not in external_urls and not url:
            path = Path(local_path)
            local_kind = validate_local_gtfs_path(source_id, path)
            if source.get("buildDepartures") is True and local_kind == "file":
                horizon = gtfs_calendar_horizon(path)
                today = source_calendar_local_date(str(source.get("timezone") or "Europe/Berlin"))
                if horizon is None or horizon < today:
                    horizon_text = horizon.isoformat() if horizon else "no active service dates"
                    raise ValueError(
                        f"source={source_id} local GTFS calendar expired through {horizon_text}; "
                        f"no refresh URL is configured (local artifact={path})"
                    )
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
            if source.get("buildDepartures") is True and artifact.path.is_file():
                artifact = ensure_current_source_calendar(
                    cache, artifact, source_id=source_id, url=url,
                    timezone_name=str(source.get("timezone") or "Europe/Berlin"),
                    allow_stale=True,
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
        seed_path = None
        seed_state = None
        if source_id == KYIV_SOURCE_ID:
            seed_path, seed_state = _kyiv_stale_cache_seed(Path(args.cache_root))
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
            seed_path=seed_path,
            seed_state=seed_state,
        )
        if source.get("buildDepartures") is True:
            artifact = ensure_current_source_calendar(
                cache, artifact, source_id=source_id, url=request_url,
                timezone_name=str(source.get("timezone") or "Europe/Berlin"),
                allow_stale=(bool(resilience_policy["allowStale"])
                             if resilience_policy is not None
                             else bool(source.get("allowStale", True))),
                headers=headers, state_url=url,
                metadata_probe=(bool(resilience_policy["metadataProbe"])
                                if resilience_policy is not None
                                else preflight == "head"),
                retry_attempts=(int(resilience_policy["retryAttempts"])
                                if resilience_policy is not None else 1),
                validator=(validate_kyiv_gtfs_archive
                           if resilience_policy is not None
                           and bool(resilience_policy["requireDataRows"])
                           else None),
            )
        result["external"][source_id] = artifact_payload(
            artifact,
            release_root=args.release_root,
        )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
