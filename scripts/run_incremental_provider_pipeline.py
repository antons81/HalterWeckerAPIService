#!/usr/bin/env python3
"""Build and validate the pilot provider release without activating it."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping

try:
    from .artifact_provenance import artifact_provenance
    from .build_stop_packages import load_gtfs_archive
    from .common_catalog import build_common_catalog
    from .external_gtfs import load_external_cities, load_external_gtfs_sources
    from .normalized_provider_artifact import load_or_build as load_or_build_normalized
    from .provider_release_assembler import (
        ReleaseAssembly,
        assemble_release,
        readiness_probe,
        validate_candidate_release,
    )
    from .static_provider_artifact import load_or_build_static_provider_artifacts
except ImportError:
    from artifact_provenance import artifact_provenance
    from build_stop_packages import load_gtfs_archive
    from common_catalog import build_common_catalog
    from external_gtfs import load_external_cities, load_external_gtfs_sources
    from normalized_provider_artifact import load_or_build as load_or_build_normalized
    from provider_release_assembler import (
        ReleaseAssembly,
        assemble_release,
        readiness_probe,
        validate_candidate_release,
    )
    from static_provider_artifact import load_or_build_static_provider_artifacts


PILOT_PROVIDER_IDS = ("israel-mot", "ttc-surface", "ttc-subway")
DEFAULT_WINDOW_DAYS = 15


@dataclass(frozen=True)
class ProviderBuild:
    provider_id: str
    source: dict[str, object]
    cities: list[dict[str, object]]
    normalized: object
    structural: object
    temporal: object
    provider_release_entry: dict[str, object]


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


def service_dates(
    *,
    valid_from: date | None = None,
    valid_through: date | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[date]:
    if window_days < 2:
        raise ValueError("temporal window must cover at least today and tomorrow")
    start = valid_from or date.today()
    end = valid_through or (start + timedelta(days=window_days - 1))
    if end <= start:
        raise ValueError("temporal window must include a date after validFrom")
    if valid_through is None and (end - start).days + 1 != window_days:
        raise ValueError("temporal window has an invalid length")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


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


def _raw_entry(
    artifacts: Mapping[str, object],
    provider_id: str,
) -> tuple[Path, str]:
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


def _first_trip_case(
    *,
    structural_database: Path,
    temporal_database: Path,
    city_id: str,
    service_date: date,
    static_root: Path,
) -> dict[str, object]:
    with sqlite3.connect(f"file:{temporal_database}?mode=ro", uri=True) as temporal:
        service_ids = [
            str(row[0])
            for row in temporal.execute(
                "SELECT service_id FROM active_services WHERE service_date=? ORDER BY service_id LIMIT 20",
                (service_date.isoformat(),),
            )
        ]
    if not service_ids:
        raise ValueError(
            f"provider=israel-mot has no active service on {service_date.isoformat()}"
        )
    placeholders = ",".join("?" for _ in service_ids)
    with sqlite3.connect(f"file:{structural_database}?mode=ro", uri=True) as structural:
        row = structural.execute(
            f"SELECT trip_id FROM trips WHERE service_id IN ({placeholders}) ORDER BY trip_id LIMIT 1",
            service_ids,
        ).fetchone()
    if row is None:
        raise ValueError("no trip is available for the selected Israel service date")
    return {
        "providerID": "israel-mot",
        "cityID": city_id,
        "tripID": str(row[0]),
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
        provider_modes.extend(
            {
                "providerID": row[0],
                "cityID": row[1],
                "mode": row[2],
                "timezone": row[3],
                "stopIDPrefix": row[4],
                "identifierPrefix": row[5],
            }
            for row in mode_rows
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
    provider_ids: tuple[str, ...] = PILOT_PROVIDER_IDS,
) -> dict[str, object]:
    if tuple(provider_ids) != PILOT_PROVIDER_IDS:
        raise ValueError(
            "Phase 6 pilot scope is fixed to israel-mot, ttc-surface, ttc-subway"
        )
    stop_manifest = _read_json(stop_data_root / "manifest.json")
    if str(stop_manifest.get("releaseID")) != release_id:
        raise ValueError("stop-data releaseID does not match nightly release ID")
    artifacts = _read_json(gtfs_artifacts_path)
    sources = _source_map(repository_root)
    missing_sources = sorted(set(provider_ids) - set(sources))
    if missing_sources:
        raise ValueError(f"pilot providers are absent from source registry: {missing_sources}")

    environment = dict(os.environ)
    environment["HALTEWECKER_NORMALIZED_PROVIDER_CACHE_ROOT"] = str(normalized_cache_root)
    environment["HALTEWECKER_STATIC_PROVIDER_ARTIFACT_ROOT"] = str(static_artifact_root)
    provider_builds: list[ProviderBuild] = []
    for provider_id in provider_ids:
        source = sources[provider_id]
        raw_path, raw_sha = _stage(
            f"{provider_id}:raw-refresh",
            lambda provider_id=provider_id: _raw_entry(artifacts, provider_id),
        )
        source = dict(source)
        cities = load_external_cities(source, repository_root)
        archive = load_gtfs_archive(str(raw_path))
        normalized_context = None
        try:
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
                f"status={normalized_use.status} duration_ms={(time.monotonic() - normalized_started) * 1000:.1f} "
                f"bytes_written={0 if normalized_use.status == 'HIT' else normalized_size} "
                f"artifact_key={normalized_use.semantic_key}",
                flush=True,
            )
            structural_started = time.monotonic()
            artifacts_use = load_or_build_static_provider_artifacts(
                normalized_context=normalized_context,
                normalized_artifact=normalized_use,
                repository_root=repository_root,
                provider_id=provider_id,
                source=source,
                cities=cities,
                stop_data=stop_data_root,
                dates=dates,
                environ=environment,
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
                )
            )
        finally:
            normalized_context.close()
            archive.close()

    work_root = Path(
        tempfile.mkdtemp(prefix=f".{release_id}.incremental-", dir=releases_root.parent)
    )
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
        _stage(
            "validation",
            lambda: validate_candidate_release(published_release_directory),
        )
        israel = next(item for item in provider_builds if item.provider_id == "israel-mot")
        israel_city = str(israel.cities[0]["id"])
        israel_stop = _first_stop(
            _provider_rows(israel.structural.database_path)[0],
            israel_city,
        )
        timezone_name = str(israel.source.get("timezone", "UTC"))
        toronto = next(item for item in provider_builds if item.provider_id == "ttc-surface")
        toronto_timezone_name = str(toronto.source.get("timezone", "UTC"))
        try:
            from zoneinfo import ZoneInfo

            timezone = ZoneInfo(timezone_name)
            toronto_timezone = ZoneInfo(toronto_timezone_name)
        except Exception:
            timezone = None
            toronto_timezone = None
        from_datetime = datetime.combine(dates[0], datetime.min.time()).replace(tzinfo=timezone)
        to_datetime = datetime.combine(dates[0], datetime.max.time()).replace(tzinfo=timezone)
        toronto_from_datetime = datetime.combine(
            dates[0], datetime.min.time()
        ).replace(tzinfo=toronto_timezone)
        toronto_to_datetime = datetime.combine(
            dates[0], datetime.max.time()
        ).replace(tzinfo=toronto_timezone)
        trip_case = _first_trip_case(
            structural_database=israel.structural.database_path,
            temporal_database=israel.temporal.database_path,
            city_id=israel_city,
            service_date=dates[0],
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
        return {
            "releaseID": release_id,
            "releaseDirectory": str(published_release_directory),
            "providerIDs": list(provider_ids),
            "dates": {"validFrom": dates[0].isoformat(), "validThrough": dates[-1].isoformat()},
            "providers": {
                item.provider_id: {
                    "normalized": item.normalized.status,
                    "structural": item.structural.status,
                    "temporal": item.temporal.status,
                    "normalizedKey": item.normalized.semantic_key,
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
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--releases-root", type=Path, required=True)
    parser.add_argument("--stop-data", type=Path, required=True)
    parser.add_argument("--gtfs-artifacts", type=Path, required=True)
    parser.add_argument("--normalized-cache-root", type=Path, required=True)
    parser.add_argument("--static-artifact-root", type=Path, required=True)
    parser.add_argument("--valid-from", type=date.fromisoformat)
    parser.add_argument("--valid-through", type=date.fromisoformat)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    print(
        f"[NightlyIncremental] stage=nightly-start status=started release_id={args.release_id} "
        f"providers={','.join(PILOT_PROVIDER_IDS)}",
        flush=True,
    )
    dates = service_dates(
        valid_from=args.valid_from,
        valid_through=args.valid_through,
        window_days=args.window_days,
    )
    try:
        result = build_incremental_candidate(
            repository_root=args.repository_root.resolve(),
            release_id=args.release_id,
            releases_root=args.releases_root.resolve(),
            stop_data_root=args.stop_data.resolve(),
            gtfs_artifacts_path=args.gtfs_artifacts.resolve(),
            normalized_cache_root=args.normalized_cache_root.resolve(),
            static_artifact_root=args.static_artifact_root.resolve(),
            dates=dates,
        )
    except Exception as error:
        print(
            f"[NightlyIncremental] stage=nightly-complete status=ERROR "
            f"duration_ms={(time.monotonic() - started) * 1000:.1f} "
            f"reason={type(error).__name__}:{error}",
            flush=True,
        )
        return 1
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
