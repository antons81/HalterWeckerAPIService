"""Read-only cache-key and manifest probe for incremental providers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from .external_build_cache import (
        DeparturePartitionCache,
        ExternalBuildCache,
        CacheKeyUnavailable,
        cache_key,
        cache_provider_allowed,
        departure_partition_key,
        departure_stop_set_digest,
    )
    from .external_gtfs import load_external_cities, load_external_gtfs_sources
except ImportError:
    from external_build_cache import (
        DeparturePartitionCache,
        ExternalBuildCache,
        CacheKeyUnavailable,
        cache_key,
        cache_provider_allowed,
        departure_partition_key,
        departure_stop_set_digest,
    )
    from external_gtfs import load_external_cities, load_external_gtfs_sources


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe transformed and departure caches without building or cleanup."
    )
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path("/srv/haltewecker/cache/gtfs"),
    )
    parser.add_argument(
        "--sources",
        type=Path,
        default=Path("config/external-gtfs-sources.json"),
    )
    parser.add_argument("--provider", action="append", required=True)
    parser.add_argument(
        "--service-date",
        action="append",
        help="Provider-local YYYYMMDD date; defaults to local D-1, D, D+1.",
    )
    return parser.parse_args()


def _raw_sha256(cache_root: Path, provider_id: str) -> str:
    state_path = cache_root / provider_id / "state.json"
    artifact_path = cache_root / provider_id / "current.zip"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise CacheKeyUnavailable(
            f"raw cache state is unreadable for {provider_id}"
        ) from error
    if (
        not isinstance(state, dict)
        or state.get("validated") is not True
        or not isinstance(state.get("sha256"), str)
        or len(state["sha256"]) != 64
        or not artifact_path.is_file()
    ):
        raise CacheKeyUnavailable(f"validated raw cache is unavailable for {provider_id}")
    return state["sha256"]


def _service_dates(source: dict[str, object], requested: list[str] | None) -> list[str]:
    if requested:
        return requested
    timezone_name = str(source.get("timezone", "UTC"))
    local_date = datetime.now(ZoneInfo(timezone_name)).date()
    return [
        (local_date + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in (-1, 0, 1)
    ]


def probe_provider(
    *,
    repository_root: Path,
    cache_root: Path,
    source: dict[str, object],
    service_dates: list[str],
    incremental_providers: str,
) -> dict[str, object]:
    provider_id = str(source["id"])
    cities = load_external_cities(source, repository_root)
    city_ids = tuple(str(city["id"]) for city in cities)
    city_id = city_ids[0]
    enabled = cache_provider_allowed(
        provider_id,
        {
            "HALTEWECKER_EXTERNAL_BUILD_CACHE_PROVIDERS": incremental_providers,
        },
    )
    report: dict[str, object] = {
        "provider": provider_id,
        "incrementalSelected": provider_id in {
            value.strip() for value in incremental_providers.split(",") if value.strip()
        },
        "buildCacheEnabled": enabled,
        "cityIDs": list(city_ids),
        "rawSHA": None,
        "buildKey": None,
        "buildStatus": "DISABLED" if not enabled else "INVALID",
        "buildReason": "provider-not-allowlisted" if not enabled else None,
        "builderFingerprint": None,
        "stopSetDigest": None,
        "departurePartitions": [],
    }
    if not enabled:
        return report

    raw_sha = _raw_sha256(cache_root, provider_id)
    report["rawSHA"] = raw_sha
    key = cache_key(
        repository_root=repository_root,
        provider_id=provider_id,
        raw_sha256=raw_sha,
        source=source,
        city_id=city_id,
        city_ids=city_ids,
    )
    report["buildKey"] = key.value
    report["builderFingerprint"] = key.builder_fingerprint
    build_cache = ExternalBuildCache(
        cache_root / "external-build",
        provider_id=provider_id,
        city_id=city_id,
        city_ids=city_ids,
        include_trip_index=bool(source.get("buildTripIndex", True)),
    )
    build_lookup = build_cache.probe(key)
    report["buildStatus"] = build_lookup.status
    report["buildReason"] = build_lookup.reason

    if build_lookup.status != "HIT" or build_lookup.directory is None:
        return report

    stop_digests: dict[str, str] = {}
    for current_city_id in city_ids:
        stop_package = build_lookup.directory / "stops" / f"{current_city_id}.json"
        digest, _count = departure_stop_set_digest(stop_package)
        stop_digests[current_city_id] = digest
    report["stopSetDigest"] = stop_digests

    departure_cache = DeparturePartitionCache(
        cache_root / "external-departure-partitions", provider_id
    )
    for current_city_id in city_ids:
        stop_digest = stop_digests[current_city_id]
        for service_date in service_dates:
            partition_key = departure_partition_key(
                repository_root=repository_root,
                provider_id=provider_id,
                city_id=current_city_id,
                service_date=service_date,
                raw_sha256=raw_sha,
                structural_input_key=key.value,
                stop_set_digest=stop_digest,
                calendar_fingerprint=raw_sha,
                source=source,
            )
            lookup = departure_cache.probe(partition_key)
            report["departurePartitions"].append(
                {
                    "cityID": current_city_id,
                    "serviceDate": service_date,
                    "key": partition_key.value,
                    "status": lookup.status,
                    "reason": lookup.reason,
                }
            )
    return report


def main() -> int:
    arguments = _arguments()
    repository_root = arguments.repository_root.resolve()
    sources_path = (
        arguments.sources
        if arguments.sources.is_absolute()
        else repository_root / arguments.sources
    )
    sources = {
        str(source["id"]): source
        for source in load_external_gtfs_sources(sources_path)
    }
    incremental_providers = os.environ.get(
        "HALTEWECKER_INCREMENTAL_PROVIDER_IDS", ""
    )
    if not incremental_providers.strip():
        raise SystemExit("HALTEWECKER_INCREMENTAL_PROVIDER_IDS is required")
    reports: list[dict[str, object]] = []
    for provider_id in arguments.provider:
        source = sources.get(provider_id)
        if source is None:
            raise SystemExit(f"unknown provider: {provider_id}")
        dates = _service_dates(source, arguments.service_date)
        reports.append(
            probe_provider(
                repository_root=repository_root,
                cache_root=arguments.cache_root,
                source=source,
                service_dates=dates,
                incremental_providers=incremental_providers,
            )
        )
    print(json.dumps(reports, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
