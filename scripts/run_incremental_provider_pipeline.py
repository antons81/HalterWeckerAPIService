#!/usr/bin/env python3
"""Build and validate the pilot provider release without activating it."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping

try:
    from .artifact_provenance import artifact_provenance
    from .build_stop_packages import load_gtfs_archive
    from .common_catalog import build_common_catalog
    from .external_build_cache import (
        CACHEABLE_PROVIDER_CITY_IDS,
        DeparturePartitionCache,
        TRANSFORMED_CACHE_PROVIDER_IDS,
        ExternalBuildCache,
        cache_enabled,
        cache_key,
        cache_provider_allowed,
        canonical_merge_group_members,
        departure_partition_key,
        departure_stop_set_digest,
        transformed_cache_enabled,
    )
    from .external_gtfs import (
        _departure_service_dates,
        load_external_cities,
        load_external_gtfs_sources,
    )
    from .external_staging import StructuralProviderContext
    from .normalized_provider_artifact import (
        load_or_build as load_or_build_normalized,
        probe_existing_for_raw_sha,
    )
    from .provider_release_assembler import (
        ReleaseAssembly,
        assemble_release,
        readiness_probe,
        validate_candidate_release,
    )
    from .provider_artifact_capabilities import (
        NORMALIZED_REQUIRED,
        STRUCTURAL_SUFFICIENT,
        provider_artifact_strategy,
    )
    from .raw_snapshot import (
        load_raw_snapshot_manifest,
        validate_raw_snapshot_entry,
    )
    from .static_provider_artifact import (
        load_or_build_static_provider_artifacts,
        probe_static_provider_artifacts,
    )
except ImportError:
    from artifact_provenance import artifact_provenance
    from build_stop_packages import load_gtfs_archive
    from common_catalog import build_common_catalog
    from external_build_cache import (
        CACHEABLE_PROVIDER_CITY_IDS,
        DeparturePartitionCache,
        TRANSFORMED_CACHE_PROVIDER_IDS,
        ExternalBuildCache,
        cache_enabled,
        cache_key,
        cache_provider_allowed,
        canonical_merge_group_members,
        departure_partition_key,
        departure_stop_set_digest,
        transformed_cache_enabled,
    )
    from external_gtfs import (
        _departure_service_dates,
        load_external_cities,
        load_external_gtfs_sources,
    )
    from external_staging import StructuralProviderContext
    from normalized_provider_artifact import (
        load_or_build as load_or_build_normalized,
        probe_existing_for_raw_sha,
    )
    from provider_release_assembler import (
        ReleaseAssembly,
        assemble_release,
        readiness_probe,
        validate_candidate_release,
    )
    from provider_artifact_capabilities import (
        NORMALIZED_REQUIRED,
        STRUCTURAL_SUFFICIENT,
        provider_artifact_strategy,
    )
    from raw_snapshot import load_raw_snapshot_manifest, validate_raw_snapshot_entry
    from static_provider_artifact import (
        load_or_build_static_provider_artifacts,
        probe_static_provider_artifacts,
    )


INCREMENTAL_PROVIDER_IDS_ENV = "HALTEWECKER_INCREMENTAL_PROVIDER_IDS"
BUILD_CACHE_PROVIDER_IDS_ENV = "HALTEWECKER_EXTERNAL_BUILD_CACHE_PROVIDERS"
DEPARTURES_V3_PROVIDER_IDS_ENV = "HALTEWECKER_EXTERNAL_DEPARTURES_V3_PROVIDERS"
DEPARTURE_CACHE_PROVIDER_IDS_ENV = "HALTEWECKER_EXTERNAL_DEPARTURE_CACHE_PROVIDERS"
REPRESENTATIVE_READINESS_PROVIDER_IDS = ("israel-mot", "ttc-surface", "ttc-subway")
DEFAULT_WINDOW_DAYS = 21
ANCHORED_WINDOW_DAYS = DEFAULT_WINDOW_DAYS


def _close_provider_resources(*resources: object) -> None:
    """Close every acquired resource without masking an active exception."""
    primary = sys.exc_info()[1]
    cleanup_error = None
    for resource in resources:
        if resource is None:
            continue
        try:
            resource.close()
        except BaseException as error:
            if primary is not None or cleanup_error is not None:
                traceback.print_exception(error, file=sys.stderr)
            else:
                cleanup_error = error
    if primary is None and cleanup_error is not None:
        raise cleanup_error


@dataclass(frozen=True)
class ProviderBuild:
    provider_id: str
    source: dict[str, object]
    cities: list[dict[str, object]]
    normalized: object
    structural: object
    temporal: object
    provider_release_entry: dict[str, object]
    strategy: str = NORMALIZED_REQUIRED


def _stage(name: str, callback):
    started = time.monotonic()
    print(f"[NightlyIncremental] stage={name} status=started", flush=True)
    try:
        result = callback()
    except Exception as error:
        print(
            f"[NightlyIncremental] stage={name} status=ERROR "
            f"duration_ms={(time.monotonic() - started) * 1000:.1f} "
            f"reason={type(error).__name__}:{error}",
            flush=True,
        )
        raise
    print(
        f"[NightlyIncremental] stage={name} status=PASS "
        f"duration_ms={(time.monotonic() - started) * 1000:.1f}",
        flush=True,
    )
    return result


def _published_release_directory(
    assembly: ReleaseAssembly,
    *,
    staging_directory: Path,
) -> Path:
    """Return only the immutable directory after the assembler's atomic publish."""
    published = assembly.release_directory.resolve()
    if published == staging_directory.resolve() or not published.is_dir():
        raise FileNotFoundError(
            "published incremental candidate is unavailable after atomic publish: "
            f"{published}"
        )
    return published


def _create_incremental_staging_directory(
    releases_root: Path,
    release_id: str,
) -> Path:
    """Create staging beside the final root, including a missing parent safely."""
    releases_root.parent.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(
            prefix=f".{release_id}.incremental-",
            dir=releases_root.parent,
        )
    )


def iso_week_anchor(reference_date: date) -> date:
    """Return the Monday that starts reference_date's ISO week."""
    return reference_date - timedelta(days=reference_date.weekday())


def service_dates(
    *,
    valid_from: date | None = None,
    valid_through: date | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[date]:
    if window_days != ANCHORED_WINDOW_DAYS:
        raise ValueError(
            f"anchored temporal window must contain exactly {ANCHORED_WINDOW_DAYS} days"
        )
    reference = valid_from or valid_through or date.today()
    start = iso_week_anchor(reference)
    end = start + timedelta(days=window_days - 1)
    if valid_through is not None and valid_through != end:
        raise ValueError(
            f"validThrough must equal anchored window end {end.isoformat()}"
        )
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _directory_size_bytes(path: Path) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    try:
        output = subprocess.check_output(
            ["du", "-skL", str(path)], stderr=subprocess.DEVNULL, text=True
        )
        return int(output.split()[0]) * 1024
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
        return 0


def _disk_telemetry(
    *,
    stage: str,
    stop_data: Path,
    releases_root: Path,
    normalized_cache_root: Path,
    static_artifact_root: Path,
) -> None:
    data_root = stop_data.parent.parent
    free_bytes = shutil.disk_usage(data_root).free
    generation_bytes = _directory_size_bytes(releases_root)
    artifact_bytes = (
        _directory_size_bytes(normalized_cache_root)
        + _directory_size_bytes(static_artifact_root)
    )
    temporary_bytes = sum(
        _directory_size_bytes(Path(path))
        for pattern in ("/tmp/haltewecker-*", "/private/tmp/haltewecker-*")
        for path in glob.glob(pattern)
    )
    print(
        "[NightlyIncremental] "
        f"disk stage={stage} free_bytes={free_bytes} "
        f"generation_bytes={generation_bytes} "
        f"artifact_bytes={artifact_bytes} "
        f"temp_bytes={temporary_bytes}",
        flush=True,
    )


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _validate_temporal_window(
    *,
    provider_id: str,
    temporal_manifest: Mapping[str, object],
    dates: list[date],
) -> None:
    dependencies = temporal_manifest.get("dependencies")
    if not isinstance(dependencies, Mapping):
        raise ValueError(f"provider={provider_id} temporal dependencies are missing")
    if dependencies.get("validFrom") != dates[0].isoformat():
        raise ValueError(f"provider={provider_id} temporal validFrom mismatch")
    if dependencies.get("validThrough") != dates[-1].isoformat():
        raise ValueError(f"provider={provider_id} temporal validThrough mismatch")


def _source_map(repository_root: Path) -> dict[str, dict[str, object]]:
    sources = load_external_gtfs_sources(
        repository_root / "config" / "external-gtfs-sources.json"
    )
    return {str(source["id"]): source for source in sources}


def _normalize_provider_ids(
    raw: str | Iterable[str] | None,
    *,
    source_name: str,
) -> tuple[str, ...]:
    if raw is None:
        raise ValueError(f"{source_name} must be configured")
    values = raw.split(",") if isinstance(raw, str) else raw
    normalized = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in values
            if str(value).strip()
        )
    )
    if not normalized:
        raise ValueError(f"{source_name} must not be empty")
    return normalized


def _environment_provider_ids(
    environ: Mapping[str, str],
    variable_name: str,
) -> tuple[str, ...]:
    return _normalize_provider_ids(
        environ.get(variable_name),
        source_name=variable_name,
    )


def provider_selection_plan(
    repository_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    provider_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    """Validate and describe the configured production incremental scope."""
    values = dict(os.environ if environ is None else environ)
    selected = _normalize_provider_ids(
        values.get(INCREMENTAL_PROVIDER_IDS_ENV)
        if provider_ids is None
        else provider_ids,
        source_name=INCREMENTAL_PROVIDER_IDS_ENV,
    )
    sources = _source_map(repository_root)
    unknown = sorted(set(selected) - set(sources))
    if unknown:
        raise ValueError(f"unknown incremental providers: {unknown}")

    build_cache_ids = set(
        _environment_provider_ids(values, BUILD_CACHE_PROVIDER_IDS_ENV)
    )
    departures_v3_ids = set(
        _environment_provider_ids(values, DEPARTURES_V3_PROVIDER_IDS_ENV)
    )
    departure_cache_ids = set(
        _environment_provider_ids(values, DEPARTURE_CACHE_PROVIDER_IDS_ENV)
    )
    capability_status: dict[str, dict[str, bool]] = {}
    unsupported: list[str] = []
    for provider_id in selected:
        source = sources[provider_id]
        status = {
            "normalized": provider_id in build_cache_ids,
            "structural": provider_id in build_cache_ids,
            "departuresV3": provider_id in departures_v3_ids,
            "stopSetDigest": provider_id in departure_cache_ids,
        }
        capability_status[provider_id] = status
        if not all(status.values()) or source.get("buildDepartures") is not True:
            unsupported.append(provider_id)
    if unsupported:
        raise ValueError(
            "providers lack required incremental capabilities: "
            f"{sorted(set(unsupported))}"
        )

    merge_groups: dict[str, dict[str, object]] = {}
    for provider_id, source in sources.items():
        merge_group = str(source.get("mergeGroup", "")).strip()
        if not merge_group:
            continue
        group = merge_groups.setdefault(
            merge_group,
            {"providers": [], "selected": [], "complete": False},
        )
        group["providers"].append(provider_id)
    for merge_group, group in merge_groups.items():
        members = tuple(sorted(str(value) for value in group["providers"]))
        selected_members = tuple(
            provider_id for provider_id in selected if provider_id in members
        )
        group["providers"] = list(members)
        group["selected"] = list(selected_members)
        group["complete"] = bool(selected_members) and set(selected_members) == set(members)
        if selected_members and not group["complete"]:
            raise ValueError(
                f"partial incremental merge-group selection: {merge_group} "
                f"selected={list(selected_members)} required={list(members)}"
            )
    return {
        "selectedProviders": list(selected),
        "normalizedCanonicalIDs": list(selected),
        "mergeGroups": {
            key: merge_groups[key]
            for key in sorted(merge_groups)
            if merge_groups[key]["selected"]
        },
        "capabilityStatus": capability_status,
        "artifactStrategies": {
            provider_id: provider_artifact_strategy(repository_root, provider_id)
            for provider_id in selected
        },
        "representativeReadinessProviders": list(REPRESENTATIVE_READINESS_PROVIDER_IDS),
        "excludedProviders": [
            {"providerID": provider_id, "reason": "not-allowlisted"}
            for provider_id in sorted(set(sources) - set(selected))
        ],
    }


def validate_incremental_artifact_strategies(
    repository_root: Path,
    provider_ids: Iterable[str],
) -> dict[str, str]:
    """Fail before provider work when no validated artifact strategy exists."""
    strategies = {
        provider_id: provider_artifact_strategy(repository_root, provider_id)
        for provider_id in provider_ids
    }
    unsupported = sorted(
        provider_id
        for provider_id, strategy in strategies.items()
        if strategy not in {NORMALIZED_REQUIRED, STRUCTURAL_SUFFICIENT}
    )
    if unsupported:
        details = ", ".join(
            f"{provider_id}={strategies[provider_id]}" for provider_id in unsupported
        )
        raise ValueError(
            "incremental artifact strategy preflight failed before provider work: "
            f"{details}"
        )
    return strategies


def _configured_stop_id_prefix(source: Mapping[str, object]) -> str:
    for key in ("staticStopIDPrefix", "namespace", "identifierPrefix"):
        value = str(source.get(key, "")).strip()
        if value:
            return value
    return ""


def _raw_entry(
    artifacts: Mapping[str, object],
    provider_id: str,
    *,
    raw_snapshot: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[Path, str]:
    if raw_snapshot is not None:
        entry = raw_snapshot.get(provider_id)
        if not isinstance(entry, Mapping):
            raise ValueError(f"provider={provider_id} is missing from raw snapshot")
        try:
            return validate_raw_snapshot_entry(provider_id, entry)
        except ValueError as error:
            raise ValueError(str(error)) from error
    external = artifacts.get("external")
    if not isinstance(external, Mapping):
        raise ValueError("GTFS artifact manifest has no external section")
    entry = external.get(provider_id)
    if not isinstance(entry, Mapping) or not entry.get("path"):
        raise ValueError(f"provider={provider_id} raw artifact is missing")
    path = Path(str(entry["path"])).resolve()
    if not path.is_file():
        raise ValueError(f"provider={provider_id} raw artifact is missing: {path}")
    digest, size = artifact_provenance(path)
    declared_digest = str(entry.get("sha256") or "")
    declared_size = entry.get("size")
    if declared_digest and declared_digest != digest:
        raise ValueError(f"provider={provider_id} raw artifact SHA mismatch")
    if isinstance(declared_size, int) and declared_size != size:
        raise ValueError(f"provider={provider_id} raw artifact size mismatch")
    return path, digest


def _external_build_cache_root(
    normalized_cache_root: Path,
    environ: Mapping[str, str],
) -> Path:
    configured = str(environ.get("HALTEWECKER_EXTERNAL_BUILD_CACHE_ROOT", "")).strip()
    if configured:
        return Path(configured)
    provider_artifacts_root = normalized_cache_root.parent
    if provider_artifacts_root.name == "provider-artifacts":
        return provider_artifacts_root.parent / "cache" / "gtfs" / "external-build"
    return provider_artifacts_root / "gtfs" / "external-build"


def _merge_group_members(
    provider_id: str,
    source: Mapping[str, object],
    sources: Mapping[str, Mapping[str, object]],
) -> tuple[str, ...]:
    return canonical_merge_group_members(source, sources)


def _archive_member_fingerprints(archive) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for filename in ("calendar.txt", "calendar_dates.txt"):
        if filename not in archive.namelist():
            continue
        digest = hashlib.sha256()
        with archive.open(filename) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        fingerprints[filename] = digest.hexdigest()
    return fingerprints


def _stop_data_stop_set_digest(stop_data_root: Path, city_ids: tuple[str, ...]) -> str:
    manifest_path = stop_data_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"stop-data manifest is unreadable: {manifest_path}") from error
    if not isinstance(manifest, Mapping):
        raise ValueError(f"stop-data manifest is invalid: {manifest_path}")
    entries = {
        str(item.get("id")): item
        for item in manifest.get("cities", ())
        if isinstance(item, Mapping) and item.get("id")
    }
    city_payloads: list[dict[str, object]] = []
    for city_id in city_ids:
        entry = entries.get(city_id)
        if not isinstance(entry, Mapping):
            raise ValueError(f"stop-data city package is missing: {city_id}")
        package_path = stop_data_root / str(entry.get("url", ""))
        try:
            payload = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(
                f"stop-data stop package is unreadable: {package_path}"
            ) from error
        if not isinstance(payload, list):
            raise ValueError(f"stop-data stop package is invalid: {package_path}")
        stop_ids = sorted(
            {
                str(item["id"])
                for item in payload
                if isinstance(item, Mapping) and item.get("id")
            }
        )
        if not stop_ids:
            raise ValueError(f"stop-data stop package is empty: {package_path}")
        city_payloads.append({"cityID": city_id, "stopIDs": stop_ids})
    canonical = json.dumps(
        city_payloads,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _probe_external_build_cache(
    *,
    repository_root: Path,
    provider_id: str,
    raw_sha: str,
    source: Mapping[str, object],
    cities: list[dict[str, object]],
    sources: Mapping[str, Mapping[str, object]],
    normalized_cache_root: Path,
    environ: Mapping[str, str],
):
    """Resolve and probe the same transformed cache identity used by runtime."""
    if not cache_enabled(environ):
        raise ValueError(
            f"external build cache is disabled for selected provider: {provider_id}"
        )
    if not cache_provider_allowed(provider_id, environ):
        raise ValueError(
            f"external build cache is not allowlisted for selected provider: {provider_id}"
        )
    city_ids = tuple(str(city["id"]) for city in cities)
    if not city_ids:
        raise ValueError(f"provider has no cities: {provider_id}")
    key = cache_key(
        repository_root=repository_root,
        provider_id=provider_id,
        raw_sha256=raw_sha,
        source=source,
        city_id=city_ids[0],
        city_ids=city_ids,
        merge_group_members=_merge_group_members(provider_id, source, sources),
    )
    cache = ExternalBuildCache(
        _external_build_cache_root(normalized_cache_root, environ),
        provider_id=provider_id,
        city_id=city_ids[0],
        city_ids=city_ids,
        include_trip_index=bool(source.get("buildTripIndex", True)),
    )
    return key, cache.probe(key), city_ids


def _load_structural_provider_context(
    *,
    archive,
    repository_root: Path,
    provider_id: str,
    raw_sha: str,
    source: Mapping[str, object],
    cities: list[dict[str, object]],
    sources: Mapping[str, Mapping[str, object]],
    stop_data_root: Path,
    normalized_cache_root: Path,
    environ: Mapping[str, str],
) -> StructuralProviderContext:
    if provider_id not in TRANSFORMED_CACHE_PROVIDER_IDS:
        raise ValueError(
            f"structural-sufficient provider is not transformed-cache eligible: {provider_id}"
        )
    if not cache_enabled(environ):
        raise ValueError(
            f"structural-sufficient provider requires external build cache: {provider_id}"
        )
    if provider_id not in CACHEABLE_PROVIDER_CITY_IDS and not transformed_cache_enabled(environ):
        raise ValueError(
            f"structural-sufficient transformed cache is disabled: {provider_id}"
        )
    if not cache_provider_allowed(provider_id, environ):
        raise ValueError(
            f"structural-sufficient provider is not cache-allowlisted: {provider_id}"
        )
    city_ids = tuple(str(city["id"]) for city in cities)
    if not city_ids:
        raise ValueError(f"structural-sufficient provider has no cities: {provider_id}")
    key = cache_key(
        repository_root=repository_root,
        provider_id=provider_id,
        raw_sha256=raw_sha,
        source=source,
        city_id=city_ids[0],
        city_ids=city_ids,
        merge_group_members=_merge_group_members(provider_id, source, sources),
    )
    cache = ExternalBuildCache(
        _external_build_cache_root(normalized_cache_root, environ),
        provider_id=provider_id,
        city_id=city_ids[0],
        city_ids=city_ids,
        include_trip_index=bool(source.get("buildTripIndex", True)),
    )
    lookup = cache.probe(key)
    print(
        f"[NightlyIncremental] provider={provider_id} stage=build-cache "
        f"strategy={STRUCTURAL_SUFFICIENT} status={lookup.status} "
        f"reason={lookup.reason} key={key.value[:12]} rawSHA={raw_sha[:12]}",
        flush=True,
    )
    if lookup.status not in {"HIT", "HIT_COMPATIBLE"}:
        raise ValueError(
            f"structural-sufficient cache is not reusable for {provider_id}: "
            f"{lookup.status} {lookup.reason}"
        )
    deterministic_provenance = {
        "cacheKey": key.value,
        "rawSHA256": raw_sha,
        "cacheBuilderFingerprint": key.builder_fingerprint,
        "cacheCityIDs": list(key.city_ids),
        "cacheProjectionFingerprint": key.projection_fingerprint,
    }
    return StructuralProviderContext(
        archive,
        provider_id=provider_id,
        structural_input_key=key.value,
        stop_set_digest=_stop_data_stop_set_digest(stop_data_root, city_ids),
        calendar_fingerprints=_archive_member_fingerprints(archive),
        provenance=deterministic_provenance,
    )


def _manifest_reference(use: object) -> dict[str, object]:
    manifest = use.manifest
    dependencies = manifest.get("dependencies")
    if not isinstance(dependencies, Mapping):
        dependencies = {}
    return {
        "path": str(use.database_path),
        "manifestPath": str(use.artifact_directory / "manifest.json"),
        "artifactKey": use.artifact_key,
        "sha256": manifest["sqlite"]["sha256"],
        "size": manifest["sqlite"]["size"],
        "schemaVersion": manifest.get(
            "structuralSchemaVersion", manifest.get("temporalSchemaVersion")
        ),
        "validFrom": dependencies.get("validFrom"),
        "validThrough": dependencies.get("validThrough"),
    }


def _provider_rows(
    provider_id: str,
    structural_database: Path,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str, str, str, str]]]:
    with sqlite3.connect(f"file:{structural_database}?mode=ro", uri=True) as connection:
        stop_rows = [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT provider_id, city_id, stop_id FROM provider_city_stops"
            )
        ]
        mode_rows = [
            tuple(str(value) for value in row)
            for row in connection.execute(
                """
                SELECT provider_id, city_id, mode, timezone,
                       stop_id_prefix, identifier_prefix
                FROM provider_city_modes
                """
            )
        ]
    if any(row[0] != provider_id for row in stop_rows):
        raise ValueError(f"provider={provider_id} structural ownership contains a foreign provider")
    if any(row[0] != provider_id for row in mode_rows):
        raise ValueError(f"provider={provider_id} structural modes contain a foreign provider")
    return stop_rows, mode_rows


def _first_stop(stop_rows: Iterable[tuple[str, str, str]], city_id: str) -> str:
    stops = sorted(row[2] for row in stop_rows if row[1] == city_id)
    if not stops:
        raise ValueError(f"no provider-local stop exists for city={city_id}")
    return stops[0]


def _active_service_dates(
    *,
    provider_id: str,
    temporal_database: Path,
    dates: Iterable[date],
) -> list[date]:
    window = sorted(set(dates))
    if not window:
        raise ValueError(f"provider={provider_id} has an empty temporal window")
    with sqlite3.connect(f"file:{temporal_database}?mode=ro", uri=True) as temporal:
        rows = temporal.execute(
            """
            SELECT DISTINCT service_date
            FROM active_services
            WHERE service_date BETWEEN ? AND ?
            ORDER BY service_date
            """,
            (window[0].strftime("%Y%m%d"), window[-1].strftime("%Y%m%d")),
        ).fetchall()
    return [datetime.strptime(str(row[0]), "%Y%m%d").date() for row in rows]


def _select_probe_date(
    *,
    provider_id: str,
    temporal_database: Path,
    dates: Iterable[date],
) -> date:
    window = sorted(set(dates))
    active_dates = _active_service_dates(
        provider_id=provider_id,
        temporal_database=temporal_database,
        dates=window,
    )
    if not active_dates:
        raise ValueError(
            f"provider={provider_id} has no active service within "
            f"{window[0].isoformat()}..{window[-1].isoformat()}"
        )
    return active_dates[0]


def _select_common_probe_date(
    *,
    provider_temporal_databases: Mapping[str, Path],
    dates: Iterable[date],
) -> date:
    window = sorted(set(dates))
    if not window:
        raise ValueError("merged readiness has an empty temporal window")
    active_by_provider = {
        provider_id: set(
            _active_service_dates(
                provider_id=provider_id,
                temporal_database=temporal_database,
                dates=window,
            )
        )
        for provider_id, temporal_database in provider_temporal_databases.items()
    }
    common_dates = (
        set.intersection(*active_by_provider.values())
        if active_by_provider
        else set()
    )
    if not common_dates:
        providers = ",".join(sorted(active_by_provider))
        raise ValueError(
            f"providers={providers} have no common active service within "
            f"{window[0].isoformat()}..{window[-1].isoformat()}"
        )
    return min(common_dates)


def _first_trip_case(
    *,
    provider_id: str,
    structural_database: Path,
    temporal_database: Path,
    city_id: str,
    service_date: date,
    static_root: Path,
) -> dict[str, object]:
    with sqlite3.connect(f"file:{structural_database}?mode=ro", uri=True) as structural:
        structural.execute("ATTACH DATABASE ? AS temporal", (str(temporal_database),))
        compact_date = service_date.strftime("%Y%m%d")
        active_service_count = structural.execute(
            "SELECT COUNT(DISTINCT service_id) FROM temporal.active_services "
            "WHERE service_date=?",
            (compact_date,),
        ).fetchone()[0]
        matching_service_count = structural.execute(
            """
            SELECT COUNT(DISTINCT active.service_id)
            FROM temporal.active_services AS active
            JOIN main.trips AS trip ON trip.service_id=active.service_id
            WHERE active.service_date=?
            """,
            (compact_date,),
        ).fetchone()[0]
        row = structural.execute(
            """
            SELECT trip.service_id, trip.trip_id
            FROM temporal.active_services AS active
            JOIN main.trips AS trip ON trip.service_id=active.service_id
            WHERE active.service_date=?
            ORDER BY trip.service_id, trip.trip_id
            LIMIT 1
            """,
            (compact_date,),
        ).fetchone()
    if row is None:
        raise ValueError(
            f"provider={provider_id} date={service_date.isoformat()} "
            f"active_service_count={active_service_count} "
            f"matching_structural_service_count={matching_service_count} "
            "has no readiness trip in the temporal/structural intersection"
        )
    selected_service_id, selected_trip_id = (str(row[0]), str(row[1]))
    print(
        "[NightlyIncremental] stage=readiness-trip status=PASS "
        f"provider={provider_id} service_date={service_date.isoformat()} "
        f"selected_service_id={selected_service_id} "
        f"selected_trip_id={selected_trip_id} "
        f"active_service_count={active_service_count} "
        f"matching_service_count={matching_service_count}",
        flush=True,
    )
    return {
        "providerID": provider_id,
        "cityID": city_id,
        "tripID": selected_trip_id,
        "serviceDate": service_date.isoformat(),
        "staticRoot": str(static_root),
    }


def _build_common_catalog(
    *,
    output: Path,
    release_id: str,
    provider_builds: list[ProviderBuild],
) -> None:
    providers: dict[str, dict[str, object]] = {}
    aliases: list[tuple[str, str]] = []
    city_stops: set[tuple[str, str]] = set()
    provider_city_stops: list[tuple[str, str, str]] = []
    provider_modes: list[dict[str, object]] = []

    for provider_order, item in enumerate(provider_builds):
        structural_reference = _manifest_reference(item.structural)
        temporal_reference = _manifest_reference(item.temporal)
        providers[item.provider_id] = {
            "cities": [str(city["id"]) for city in item.cities],
            "status": "active",
            "required": True,
            "providerOrder": provider_order,
            "mergeGroup": str(item.source.get("mergeGroup", "")),
            "releaseID": release_id,
            "structural": structural_reference,
            "temporal": temporal_reference,
        }
        stop_rows, mode_rows = _provider_rows(
            item.provider_id,
            item.structural.database_path,
        )
        provider_city_stops.extend(stop_rows)
        city_stops.update((city_id, stop_id) for _provider, city_id, stop_id in stop_rows)
        configured_stop_prefix = _configured_stop_id_prefix(item.source)
        for row in mode_rows:
            artifact_stop_prefix = row[4].strip()
            if (
                configured_stop_prefix
                and artifact_stop_prefix
                and configured_stop_prefix != artifact_stop_prefix
            ):
                raise ValueError(
                    f"provider={item.provider_id} stop ID prefix mismatch: "
                    f"config={configured_stop_prefix!r} artifact={artifact_stop_prefix!r}"
                )
            provider_modes.append(
                {
                    "providerID": row[0],
                    "cityID": row[1],
                    "mode": row[2],
                    "timezone": row[3],
                    "stopIDPrefix": configured_stop_prefix or artifact_stop_prefix,
                    "identifierPrefix": row[5],
                }
            )
        for city in item.cities:
            city_id = str(city["id"])
            for alias in city.get("aliases", ()) or ():
                aliases.append((str(alias), city_id))

    build_common_catalog(
        output,
        release_id=release_id,
        providers=providers,
        aliases=aliases,
        city_stops=sorted(city_stops),
        provider_city_stops=provider_city_stops,
        provider_modes=provider_modes,
    )


def full_chain_preflight(
    *,
    repository_root: Path,
    stop_data_root: Path,
    normalized_cache_root: Path,
    static_artifact_root: Path,
    dates: list[date],
    provider_ids: tuple[str, ...],
    raw_snapshot: Mapping[str, Mapping[str, object]],
    common_snapshot_fingerprint: str | None = None,
) -> dict[str, object]:
    """Probe the complete runtime artifact chain without building or mutating cache."""
    selection_plan = provider_selection_plan(
        repository_root,
        provider_ids=provider_ids,
    )
    strategies = validate_incremental_artifact_strategies(repository_root, provider_ids)
    sources = _source_map(repository_root)
    environment = dict(os.environ)
    environment["HALTEWECKER_NORMALIZED_PROVIDER_CACHE_ROOT"] = str(
        normalized_cache_root
    )
    environment["HALTEWECKER_STATIC_PROVIDER_ARTIFACT_ROOT"] = str(
        static_artifact_root
    )
    provider_reports: list[dict[str, object]] = []
    mandatory_misses: list[dict[str, object]] = []
    rollover_misses: list[dict[str, object]] = []

    for provider_id in provider_ids:
        source = dict(sources[provider_id])
        strategy = strategies[provider_id]
        report: dict[str, object] = {
            "providerID": provider_id,
            "strategy": strategy,
            "raw": {"status": "MISS"},
            "transformed": {"status": "NOT_PROBED"},
            "normalized": {"status": "NOT_APPLICABLE"},
            "structural": {"status": "NOT_PROBED"},
            "temporal": {"status": "NOT_PROBED"},
            "departurePartitions": [],
        }
        archive = None
        normalized_use = None
        structural_context = None
        try:
            raw_path, raw_sha = _raw_entry({}, provider_id, raw_snapshot=raw_snapshot)
            report["raw"] = {
                "status": "HIT",
                "sha256": raw_sha,
                "path": str(raw_path),
            }
            cities = load_external_cities(source, repository_root)
            build_key, build_lookup, city_ids = _probe_external_build_cache(
                repository_root=repository_root,
                provider_id=provider_id,
                raw_sha=raw_sha,
                source=source,
                cities=cities,
                sources=sources,
                normalized_cache_root=normalized_cache_root,
                environ=environment,
            )
            report["transformed"] = {
                "status": build_lookup.status,
                "reason": build_lookup.reason,
                "key": build_key.value,
                "builderFingerprint": build_key.builder_fingerprint,
                "directory": (
                    str(build_lookup.directory)
                    if build_lookup.directory is not None
                    else None
                ),
                "mergeGroupMembers": list(build_key.city_ids),
            }
            if build_lookup.status not in {"HIT", "HIT_COMPATIBLE"}:
                mandatory_misses.append(
                    {
                        "providerID": provider_id,
                        "stage": "transformed",
                        "status": build_lookup.status,
                        "reason": build_lookup.reason,
                    }
                )

            if strategy == NORMALIZED_REQUIRED:
                try:
                    archive = load_gtfs_archive(str(raw_path))
                    normalized_use = probe_existing_for_raw_sha(
                        repository_root=repository_root,
                        provider_id=provider_id,
                        raw_artifact_sha256=raw_sha,
                        gtfs_cache_root=normalized_cache_root.parent / "gtfs",
                        environ=environment,
                        archive=archive,
                    )
                    report["normalized"] = {
                        "status": normalized_use.status,
                        "resolution": normalized_use.resolution_status,
                        "reason": normalized_use.reason,
                        "key": normalized_use.semantic_key,
                        "directory": str(normalized_use.artifact_directory),
                    }
                except Exception as error:
                    report["normalized"] = {
                        "status": "MISS",
                        "reason": f"{type(error).__name__}: {error}",
                    }
                    mandatory_misses.append(
                        {
                            "providerID": provider_id,
                            "stage": "normalized",
                            "status": "MISS",
                            "reason": str(error),
                        }
                    )
            elif (
                build_lookup.status in {"HIT", "HIT_COMPATIBLE"}
                and strategy == STRUCTURAL_SUFFICIENT
            ):
                archive = load_gtfs_archive(str(raw_path))
                structural_context = StructuralProviderContext(
                    archive,
                    provider_id=provider_id,
                    structural_input_key=build_key.value,
                    stop_set_digest=_stop_data_stop_set_digest(
                        stop_data_root,
                        city_ids,
                    ),
                    calendar_fingerprints=_archive_member_fingerprints(archive),
                    provenance={
                        "cacheKey": build_key.value,
                        "rawSHA256": raw_sha,
                        "cacheBuilderFingerprint": build_key.builder_fingerprint,
                        "cacheCityIDs": list(build_key.city_ids),
                        "cacheProjectionFingerprint": build_key.projection_fingerprint,
                    },
                )

            if normalized_use is not None or structural_context is not None:
                probes = probe_static_provider_artifacts(
                    normalized_artifact=normalized_use,
                    structural_context=structural_context,
                    repository_root=repository_root,
                    provider_id=provider_id,
                    source=source,
                    cities=cities,
                    stop_data=stop_data_root,
                    dates=dates,
                    environ=environment,
                    common_snapshot_fingerprint=common_snapshot_fingerprint,
                )
                report["structural"] = {
                    "status": probes.structural.status,
                    "reason": probes.structural.reason,
                    "key": probes.structural.artifact_key,
                    "directory": str(probes.structural.artifact_directory),
                }
                report["temporal"] = {
                    "status": probes.temporal.status,
                    "reason": probes.temporal.reason,
                    "key": probes.temporal.artifact_key,
                    "directory": str(probes.temporal.artifact_directory),
                }
                for stage, probe in (
                    ("structural", probes.structural),
                    ("temporal", probes.temporal),
                ):
                    if probe.status != "HIT":
                        mandatory_misses.append(
                            {
                                "providerID": provider_id,
                                "stage": stage,
                                "status": probe.status,
                                "reason": probe.reason,
                                "key": probe.artifact_key,
                            }
                        )

            if build_lookup.directory is not None:
                departure_cache = DeparturePartitionCache(
                    Path(_external_build_cache_root(normalized_cache_root, environment)).parent
                    / "external-departure-partitions",
                    provider_id,
                )
                departure_dates = _departure_service_dates(
                    str(source["timezone"]),
                    int(source.get("departurePackageDays", 3)),
                )
                for city_id in city_ids:
                    stop_package = build_lookup.directory / "stops" / f"{city_id}.json"
                    stop_digest, stop_count = departure_stop_set_digest(stop_package)
                    for service_date in departure_dates:
                        partition = departure_partition_key(
                            repository_root=repository_root,
                            provider_id=provider_id,
                            city_id=city_id,
                            service_date=service_date,
                            raw_sha256=raw_sha,
                            structural_input_key=build_key.value,
                            stop_set_digest=stop_digest,
                            calendar_fingerprint=raw_sha,
                            source=source,
                        )
                        lookup = departure_cache.probe(partition)
                        row = {
                            "cityID": city_id,
                            "serviceDate": service_date,
                            "key": partition.value,
                            "status": lookup.status,
                            "reason": lookup.reason,
                            "stopCount": stop_count,
                        }
                        report["departurePartitions"].append(row)
                        if lookup.status == "MISS":
                            rollover_misses.append(
                                {
                                    "providerID": provider_id,
                                    "cityID": city_id,
                                    "serviceDate": service_date,
                                    "key": partition.value,
                                }
                            )
                        elif lookup.status != "HIT":
                            mandatory_misses.append(
                                {
                                    "providerID": provider_id,
                                    "stage": "departure-partition",
                                    "status": lookup.status,
                                    "reason": lookup.reason,
                                    "key": partition.value,
                                }
                            )
        except Exception as error:
            report["error"] = f"{type(error).__name__}: {error}"
            mandatory_misses.append(
                {
                    "providerID": provider_id,
                    "stage": "preflight",
                    "status": "ERROR",
                    "reason": str(error),
                }
            )
        finally:
            if archive is not None:
                archive.close()
        provider_reports.append(report)
        provider_failed = any(
            item["providerID"] == provider_id for item in mandatory_misses
        )
        print(
            "[NightlyIncremental] stage=full-chain-preflight "
            f"provider={provider_id} status={'NO-GO' if provider_failed else 'PASS'} "
            + json.dumps(report, ensure_ascii=False, sort_keys=True),
            flush=True,
        )

    return {
        "status": "PASS" if not mandatory_misses else "NO-GO",
        "selectionPlan": selection_plan,
        "providers": provider_reports,
        "mandatoryMisses": mandatory_misses,
        "rolloverPartitionMisses": rollover_misses,
        "mandatoryMissCount": len(mandatory_misses),
        "rolloverPartitionMissCount": len(rollover_misses),
        "dates": {
            "validFrom": dates[0].isoformat(),
            "validThrough": dates[-1].isoformat(),
        },
    }


def build_incremental_candidate(
    *,
    repository_root: Path,
    release_id: str,
    releases_root: Path,
    stop_data_root: Path,
    gtfs_artifacts_path: Path,
    normalized_cache_root: Path,
    static_artifact_root: Path,
    dates: list[date],
    provider_ids: tuple[str, ...] | None = None,
    raw_snapshot: Mapping[str, Mapping[str, object]] | None = None,
    common_snapshot_fingerprint: str | None = None,
    trusted_common_stop_data: Path | None = None,
) -> dict[str, object]:
    selection_plan = provider_selection_plan(
        repository_root,
        provider_ids=provider_ids,
    )
    provider_ids = tuple(selection_plan["selectedProviders"])
    validate_incremental_artifact_strategies(repository_root, provider_ids)
    stop_manifest = _read_json(stop_data_root / "manifest.json")
    if str(stop_manifest.get("releaseID")) != release_id:
        raise ValueError("stop-data releaseID does not match nightly release ID")
    artifacts = _read_json(gtfs_artifacts_path)
    sources = _source_map(repository_root)
    missing_sources = sorted(set(provider_ids) - set(sources))
    if missing_sources:
        raise ValueError(
            f"incremental providers are absent from source registry: {missing_sources}"
        )

    environment = dict(os.environ)
    environment["HALTEWECKER_NORMALIZED_PROVIDER_CACHE_ROOT"] = str(normalized_cache_root)
    environment["HALTEWECKER_STATIC_PROVIDER_ARTIFACT_ROOT"] = str(static_artifact_root)
    _disk_telemetry(
        stage="incremental-start",
        stop_data=stop_data_root,
        releases_root=releases_root,
        normalized_cache_root=normalized_cache_root,
        static_artifact_root=static_artifact_root,
    )
    provider_builds: list[ProviderBuild] = []
    for provider_id in provider_ids:
        source = sources[provider_id]
        strategy = str(selection_plan["artifactStrategies"][provider_id])
        raw_path, raw_sha = _stage(
            f"{provider_id}:raw-refresh",
            lambda provider_id=provider_id: _raw_entry(
                artifacts,
                provider_id,
                raw_snapshot=raw_snapshot,
            ),
        )
        source = dict(source)
        cities = load_external_cities(source, repository_root)
        archive = None
        normalized_context = None
        structural_context = None
        try:
            archive = load_gtfs_archive(str(raw_path))
            normalized_use = None
            if strategy == NORMALIZED_REQUIRED:
                normalized_started = time.monotonic()
                normalized_context, normalized_use = load_or_build_normalized(
                    archive=archive,
                    repository_root=repository_root,
                    provider_id=provider_id,
                    raw_artifact_sha256=raw_sha,
                    gtfs_cache_root=normalized_cache_root.parent / "gtfs",
                    environ=environment,
                )
                normalized_size = int(
                    normalized_use.manifest.get("normalizedSQLite", {}).get("size", 0)
                )
                print(
                    f"[NightlyIncremental] provider={provider_id} stage=normalized-provider "
                    f"strategy={strategy} status={normalized_use.status} "
                    f"resolution={normalized_use.resolution_status} "
                    f"duration_ms={(time.monotonic() - normalized_started) * 1000:.1f} "
                    f"bytes_written={0 if normalized_use.status in {'HIT', 'HIT_COMPATIBLE'} else normalized_size} "
                    f"artifact_key={normalized_use.semantic_key}",
                    flush=True,
                )
                _disk_telemetry(
                    stage=f"{provider_id}:normalized",
                    stop_data=stop_data_root,
                    releases_root=releases_root,
                    normalized_cache_root=normalized_cache_root,
                    static_artifact_root=static_artifact_root,
                )
            elif strategy == STRUCTURAL_SUFFICIENT:
                structural_context = _load_structural_provider_context(
                    archive=archive,
                    repository_root=repository_root,
                    provider_id=provider_id,
                    raw_sha=raw_sha,
                    source=source,
                    cities=cities,
                    sources=sources,
                    stop_data_root=stop_data_root,
                    normalized_cache_root=normalized_cache_root,
                    environ=environment,
                )
            else:
                raise ValueError(
                    f"unsupported incremental artifact strategy for {provider_id}: {strategy}"
                )
            structural_started = time.monotonic()
            artifacts_use = load_or_build_static_provider_artifacts(
                normalized_context=normalized_context,
                normalized_artifact=normalized_use,
                structural_context=structural_context,
                repository_root=repository_root,
                provider_id=provider_id,
                source=source,
                cities=cities,
                stop_data=stop_data_root,
                dates=dates,
                environ=environment,
                common_snapshot_fingerprint=common_snapshot_fingerprint,
            )
            structural = artifacts_use.structural
            temporal = artifacts_use.temporal
            print(
                f"[NightlyIncremental] provider={provider_id} stage=structural-provider "
                f"status={structural.status} duration_ms={structural.duration_seconds * 1000:.1f} "
                f"bytes_written={0 if structural.status == 'HIT' else structural.bytes_written} "
                f"artifact_key={structural.artifact_key}",
                flush=True,
            )
            print(
                f"[NightlyIncremental] provider={provider_id} stage=temporal-provider "
                f"status={temporal.status} duration_ms={temporal.duration_seconds * 1000:.1f} "
                f"bytes_written={0 if temporal.status == 'HIT' else temporal.bytes_written} "
                f"artifact_key={temporal.artifact_key} "
                f"validFrom={temporal.manifest['dependencies']['validFrom']} "
                f"validThrough={temporal.manifest['dependencies']['validThrough']}",
                flush=True,
            )
            _disk_telemetry(
                stage=f"{provider_id}:structural",
                stop_data=stop_data_root,
                releases_root=releases_root,
                normalized_cache_root=normalized_cache_root,
                static_artifact_root=static_artifact_root,
            )
            _disk_telemetry(
                stage=f"{provider_id}:temporal",
                stop_data=stop_data_root,
                releases_root=releases_root,
                normalized_cache_root=normalized_cache_root,
                static_artifact_root=static_artifact_root,
            )
            _validate_temporal_window(
                provider_id=provider_id,
                temporal_manifest=temporal.manifest,
                dates=dates,
            )
            provider_builds.append(
                ProviderBuild(
                    provider_id=provider_id,
                    source=source,
                    cities=cities,
                    normalized=normalized_use,
                    structural=structural,
                    temporal=temporal,
                    provider_release_entry={},
                    strategy=strategy,
                )
            )
        finally:
            _close_provider_resources(
                normalized_context,
                structural_context,
                archive if structural_context is None else None,
            )

    work_root = _create_incremental_staging_directory(releases_root, release_id)
    try:
        common_path = work_root / "common.sqlite"
        _stage(
            "common-catalog",
            lambda: _build_common_catalog(
                output=common_path,
                release_id=release_id,
                provider_builds=provider_builds,
            ),
        )
        _disk_telemetry(
            stage="common-catalog",
            stop_data=stop_data_root,
            releases_root=releases_root,
            normalized_cache_root=normalized_cache_root,
            static_artifact_root=static_artifact_root,
        )
        provider_inputs = {
            item.provider_id: {
                "cities": [str(city["id"]) for city in item.cities],
                "status": "active",
                "required": True,
                "providerOrder": index,
                "mergeGroup": str(item.source.get("mergeGroup", "")),
                "structural": _manifest_reference(item.structural),
                "temporal": _manifest_reference(item.temporal),
            }
            for index, item in enumerate(provider_builds)
        }
        publication_started = time.monotonic()
        assembly = _stage(
            "release-assembly",
            lambda: assemble_release(
                releases_root,
                release_id,
                common_database=common_path,
                stop_data_root=stop_data_root,
                providers=provider_inputs,
                trusted_common_stop_data=trusted_common_stop_data,
                common_snapshot_fingerprint=common_snapshot_fingerprint,
            ),
        )
        published_release_directory = _published_release_directory(
            assembly,
            staging_directory=work_root,
        )
        print(
            "[NightlyIncremental] stage=release-publication status=PASS "
            f"staging_path={work_root} "
            f"published_path={published_release_directory} "
            f"transition_ms={(time.monotonic() - publication_started) * 1000:.1f}",
            flush=True,
        )
        _disk_telemetry(
            stage="release-assembly",
            stop_data=stop_data_root,
            releases_root=releases_root,
            normalized_cache_root=normalized_cache_root,
            static_artifact_root=static_artifact_root,
        )
        _stage(
            "validation",
            lambda: validate_candidate_release(published_release_directory),
        )
        _disk_telemetry(
            stage="validation",
            stop_data=stop_data_root,
            releases_root=releases_root,
            normalized_cache_root=normalized_cache_root,
            static_artifact_root=static_artifact_root,
        )
        israel = next(item for item in provider_builds if item.provider_id == "israel-mot")
        israel_city = str(israel.cities[0]["id"])
        israel_probe_date = _select_probe_date(
            provider_id=israel.provider_id,
            temporal_database=israel.temporal.database_path,
            dates=dates,
        )
        israel_stop = _first_stop(
            _provider_rows(
                israel.provider_id,
                israel.structural.database_path,
            )[0],
            israel_city,
        )
        timezone_name = str(israel.source.get("timezone", "UTC"))
        toronto = next(item for item in provider_builds if item.provider_id == "ttc-surface")
        toronto_timezone_name = str(toronto.source.get("timezone", "UTC"))
        subway = next(item for item in provider_builds if item.provider_id == "ttc-subway")
        toronto_surface_probe_date = _select_probe_date(
            provider_id=toronto.provider_id,
            temporal_database=toronto.temporal.database_path,
            dates=dates,
        )
        toronto_subway_probe_date = _select_probe_date(
            provider_id=subway.provider_id,
            temporal_database=subway.temporal.database_path,
            dates=dates,
        )
        toronto_probe_date = _select_common_probe_date(
            provider_temporal_databases={
                "ttc-surface": toronto.temporal.database_path,
                "ttc-subway": subway.temporal.database_path,
            },
            dates=dates,
        )
        probe_dates = {
            "israel-mot": israel_probe_date.isoformat(),
            "ttc-surface": toronto_surface_probe_date.isoformat(),
            "ttc-subway": toronto_subway_probe_date.isoformat(),
            "toronto-common": toronto_probe_date.isoformat(),
        }
        print(
            "[NightlyIncremental] stage=readiness-probe-dates status=PASS "
            + " ".join(f"{key}={value}" for key, value in probe_dates.items()),
            flush=True,
        )
        try:
            from zoneinfo import ZoneInfo

            timezone = ZoneInfo(timezone_name)
            toronto_timezone = ZoneInfo(toronto_timezone_name)
        except Exception:
            timezone = None
            toronto_timezone = None
        from_datetime = datetime.combine(israel_probe_date, datetime.min.time()).replace(tzinfo=timezone)
        to_datetime = datetime.combine(israel_probe_date, datetime.max.time()).replace(tzinfo=timezone)
        toronto_from_datetime = datetime.combine(
            toronto_probe_date, datetime.min.time()
        ).replace(tzinfo=toronto_timezone)
        toronto_to_datetime = datetime.combine(
            toronto_probe_date, datetime.max.time()
        ).replace(tzinfo=toronto_timezone)
        trip_case = _first_trip_case(
            provider_id=israel.provider_id,
            structural_database=israel.structural.database_path,
            temporal_database=israel.temporal.database_path,
            city_id=israel_city,
            service_date=israel_probe_date,
            static_root=published_release_directory / "stop-data",
        )
        readiness = _stage(
            "readiness",
            lambda: readiness_probe(
                published_release_directory,
                provider_ids=provider_ids,
                israel_case={
                    "providerID": "israel-mot",
                    "cityID": israel_city,
                    "stopID": israel_stop,
                    "fromDate": from_datetime.isoformat(),
                    "toDate": to_datetime.isoformat(),
                    "limit": 30,
                },
                toronto_case={
                    "cityID": "toronto",
                    "stopID": "100",
                    "fromDate": toronto_from_datetime.isoformat(),
                    "toDate": toronto_to_datetime.isoformat(),
                    "limit": 30,
                },
                trip_case=trip_case,
            ),
        )
        _disk_telemetry(
            stage="readiness",
            stop_data=stop_data_root,
            releases_root=releases_root,
            normalized_cache_root=normalized_cache_root,
            static_artifact_root=static_artifact_root,
        )
        stop_metadata_path = stop_data_root.parent / "release-metadata.json"
        stop_metadata = _read_json(stop_metadata_path) if stop_metadata_path.is_file() else {}
        stop_manifest_path = stop_data_root / "manifest.json"
        stop_manifest_sha256 = hashlib.sha256(stop_manifest_path.read_bytes()).hexdigest()
        return {
            "releaseID": release_id,
            "releaseDirectory": str(published_release_directory),
            "stopData": {
                "releaseID": str(stop_manifest.get("releaseID")),
                "path": str(stop_data_root),
                "buildFingerprint": stop_metadata.get("buildFingerprint"),
                "manifestSha256": stop_manifest_sha256,
            },
            "selectionPlan": selection_plan,
            "providerIDs": list(provider_ids),
            "dates": {"validFrom": dates[0].isoformat(), "validThrough": dates[-1].isoformat()},
            "readinessProbeDates": probe_dates,
            "providers": {
                item.provider_id: {
                    "artifactStrategy": item.strategy,
                    "normalized": (
                        item.normalized.status
                        if item.normalized is not None
                        else "NOT_USED"
                    ),
                    "structural": item.structural.status,
                    "temporal": item.temporal.status,
                    "normalizedKey": (
                        item.normalized.semantic_key
                        if item.normalized is not None
                        else None
                    ),
                    "structuralKey": item.structural.artifact_key,
                    "temporalKey": item.temporal.artifact_key,
                }
                for item in provider_builds
            },
            "readiness": readiness,
        }
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--release-id")
    parser.add_argument("--releases-root", type=Path)
    parser.add_argument("--stop-data", type=Path)
    parser.add_argument("--gtfs-artifacts", type=Path)
    parser.add_argument("--normalized-cache-root", type=Path)
    parser.add_argument("--static-artifact-root", type=Path)
    parser.add_argument("--raw-snapshot-manifest", type=Path)
    parser.add_argument("--common-snapshot-fingerprint")
    parser.add_argument("--trusted-common-stop-data", type=Path)
    parser.add_argument("--valid-from", type=date.fromisoformat)
    parser.add_argument("--valid-through", type=date.fromisoformat)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--full-chain-preflight", action="store_true")
    parser.add_argument("--result-json", type=Path)
    parser.add_argument("--selection-plan", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    if args.repository_root is None:
        raise SystemExit("--repository-root is required")
    repository_root = args.repository_root.resolve()
    try:
        selection_plan = provider_selection_plan(repository_root)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1
    if args.selection_plan:
        print(json.dumps(selection_plan, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    raw_snapshot = None
    if args.raw_snapshot_manifest is not None:
        try:
            raw_snapshot = load_raw_snapshot_manifest(
                args.raw_snapshot_manifest,
                required_provider_ids=selection_plan["selectedProviders"],
            )
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return 1
        print(
            "[NightlyIncremental] stage=raw-snapshot status=PASS "
            f"manifest={args.raw_snapshot_manifest.resolve()} providers={len(raw_snapshot)}",
            flush=True,
        )
    if args.full_chain_preflight:
        required_arguments = {
            "--stop-data": args.stop_data,
            "--normalized-cache-root": args.normalized_cache_root,
            "--static-artifact-root": args.static_artifact_root,
        }
        missing_arguments = [
            name for name, value in required_arguments.items() if value is None
        ]
        if missing_arguments:
            raise SystemExit(
                "full-chain-preflight missing required arguments: "
                + ", ".join(missing_arguments)
            )
        if raw_snapshot is None:
            raise SystemExit(
                "full-chain-preflight requires --raw-snapshot-manifest"
            )
        dates = service_dates(
            valid_from=args.valid_from,
            valid_through=args.valid_through,
            window_days=args.window_days,
        )
        result = full_chain_preflight(
            repository_root=repository_root,
            stop_data_root=args.stop_data.resolve(),
            normalized_cache_root=args.normalized_cache_root.resolve(),
            static_artifact_root=args.static_artifact_root.resolve(),
            dates=dates,
            provider_ids=tuple(selection_plan["selectedProviders"]),
            raw_snapshot=raw_snapshot,
            common_snapshot_fingerprint=args.common_snapshot_fingerprint,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result["status"] == "PASS" else 1
    required_arguments = {
        "--release-id": args.release_id,
        "--releases-root": args.releases_root,
        "--stop-data": args.stop_data,
        "--gtfs-artifacts": args.gtfs_artifacts,
        "--normalized-cache-root": args.normalized_cache_root,
        "--static-artifact-root": args.static_artifact_root,
    }
    missing_arguments = [
        name for name, value in required_arguments.items() if value is None
    ]
    if missing_arguments:
        raise SystemExit("missing required arguments: " + ", ".join(missing_arguments))
    print(
        f"[NightlyIncremental] stage=nightly-start status=started release_id={args.release_id} "
        f"providers={','.join(selection_plan['selectedProviders'])}",
        flush=True,
    )
    print(
        "[NightlyIncremental] stage=selection-plan status=PASS "
        + json.dumps(selection_plan, ensure_ascii=False, sort_keys=True),
        flush=True,
    )
    dates = service_dates(
        valid_from=args.valid_from,
        valid_through=args.valid_through,
        window_days=args.window_days,
    )
    try:
        result = build_incremental_candidate(
            repository_root=repository_root,
            release_id=args.release_id,
            releases_root=args.releases_root.resolve(),
            stop_data_root=args.stop_data.resolve(),
            gtfs_artifacts_path=args.gtfs_artifacts.resolve(),
            normalized_cache_root=args.normalized_cache_root.resolve(),
            static_artifact_root=args.static_artifact_root.resolve(),
            dates=dates,
            provider_ids=tuple(selection_plan["selectedProviders"]),
            raw_snapshot=raw_snapshot,
            common_snapshot_fingerprint=args.common_snapshot_fingerprint,
            trusted_common_stop_data=(
                args.trusted_common_stop_data.resolve()
                if args.trusted_common_stop_data is not None
                else None
            ),
        )
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        print(
            f"[NightlyIncremental] stage=nightly-complete status=ERROR "
            f"duration_ms={(time.monotonic() - started) * 1000:.1f} "
            f"reason={type(error).__name__}:{error}",
            flush=True,
        )
        return 1
    if args.result_json is not None:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.result_json.with_name(f".{args.result_json.name}.tmp")
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, args.result_json)
        print(
            f"[NightlyIncremental] stage=result-metadata status=PASS path={args.result_json}",
            flush=True,
        )
    print(
        f"[NightlyIncremental] stage=nightly-complete status=PASS "
        f"duration_ms={(time.monotonic() - started) * 1000:.1f} "
        f"release_id={args.release_id} no_activate=true "
        f"release_directory={result['releaseDirectory']}",
        flush=True,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
