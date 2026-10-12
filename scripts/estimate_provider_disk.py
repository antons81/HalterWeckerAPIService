#!/usr/bin/env python3
"""Forecast new provider allocations from read-only cache probes.

Created by Anton on 2026-10-11.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import io
import os
import sys
import zipfile
from pathlib import Path

MIB = 1024 ** 2
REUSABLE = frozenset({"HIT", "HIT_COMPATIBLE"})
ALLOCATION_BLOCK_BYTES = 4096
MISS_GROWTH_NUMERATOR = 5
MISS_GROWTH_DENOMINATOR = 4


def forecast_provider_allocations(
    plan: dict, active_release: Path | None, *, normalized_cache_root: Path | None = None,
) -> dict:
    """Charge cache misses only; existing releases are already included by df.

    Estimates are allocation envelopes, not a promise about future source size.
    A SQLite rebuild reserves twice the expanded GTFS size, with the previous
    artifact size as a lower bound. The independent live reserve guard remains
    required for source growth and concurrent filesystem writers.
    """
    active = {}
    if active_release is not None:
        active = json.loads((active_release / "release.json").read_text())["providers"]
    allocations = []
    for provider in plan["providers"]:
        provider_id = provider["providerID"]
        raw = provider["raw"]
        if raw.get("status") != "HIT" or provider.get("error"):
            raise ValueError(f"provider={provider_id} cannot forecast: {provider.get('error') or 'unavailable raw input'}")
        transformed = provider.get("transformed", {}).get("status", "MISS")
        if transformed not in REUSABLE | {"MISS", "NOT_APPLICABLE"}:
            raise ValueError(f"provider={provider_id} invalid transformed cache status={transformed}")
        with zipfile.ZipFile(raw["path"]) as archive:
            expanded = sum(item.file_size for item in archive.infolist())
            calendar_bytes = sum(item.file_size for item in archive.infolist()
                                 if Path(item.filename).name in {"calendar.txt", "calendar_dates.txt"})
        if expanded <= 0:
            raise ValueError(f"provider={provider_id} raw input is empty")
        layers = {}
        previous = active.get(provider_id, {})
        for layer in ("normalized", "structural", "temporal"):
            status = provider[layer]["status"]
            if status in REUSABLE or status == "NOT_APPLICABLE":
                layers[layer] = 0
                continue
            if status not in {"MISS", "NOT_PROBED"}:
                raise ValueError(f"provider={provider_id} layer={layer} invalid cache status={status}")
            old_size = 0
            if layer in previous:
                old_path = (active_release / previous[layer]["path"]).resolve(strict=True)
                old_size = old_path.stat().st_size
            if layer == "normalized" and normalized_cache_root is not None and previous.get("structural"):
                reference = previous["structural"]
                metadata = json.loads((active_release / reference["manifestPath"]).read_text())
                key = metadata.get("normalizedArtifactSemanticKey")
                if key:
                    normalized = normalized_cache_root / provider_id / str(key) / "normalized.sqlite"
                    if normalized.is_file():
                        old_size = normalized.stat().st_size
            if layer == "temporal":
                # Calendar service expansion is bounded by the requested window.
                from datetime import date
                dates = plan["dates"]
                days = (date.fromisoformat(dates["validThrough"])
                        - date.fromisoformat(dates["validFrom"])).days + 1
                if days < 1:
                    raise ValueError("invalid temporal forecast window")
                layers[layer] = max(MIB, (old_size * 5 + 3) // 4, calendar_bytes * days * 2)
            else:
                layers[layer] = max(MIB, (old_size * 5 + 3) // 4, expanded * 2)
        allocations.append({"providerID": provider_id, "rawExpandedBytes": expanded,
                            "cacheStatuses": {key: provider[key]["status"] for key in layers},
                            "additionalBytes": sum(layers.values()), "layers": layers})
    total = sum(item["additionalBytes"] for item in allocations)
    return {"additionalBytes": total, "additionalKiB": (total + 1023) // 1024,
            "providers": allocations}


def _round_up(value: int, block_size: int = ALLOCATION_BLOCK_BYTES) -> int:
    return ((value + block_size - 1) // block_size) * block_size


def _largest_valid_build_cache_generation(provider_root: Path) -> int:
    """Return the largest validated generation's logical allocation envelope."""
    if not provider_root.is_dir() or provider_root.is_symlink():
        return 0
    largest = 0
    for generation in provider_root.iterdir():
        if not generation.is_dir() or generation.is_symlink():
            continue
        manifest_path = generation / "manifest.json"
        if manifest_path.is_symlink():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            continue
        outputs = manifest.get("cachedOutputs") if isinstance(manifest, dict) else None
        if (
            not isinstance(outputs, list)
            or manifest.get("complete") is not True
            or manifest.get("status") != "complete"
            or manifest.get("providerID") != provider_root.name
        ):
            continue
        try:
            generation_root = generation.resolve(strict=True)
            total = _round_up(manifest_path.stat().st_size)
        except OSError:
            continue
        valid = True
        for output in outputs:
            if not isinstance(output, dict):
                valid = False
                break
            relative = output.get("path")
            declared_size = output.get("size")
            if (
                not isinstance(relative, str)
                or not relative
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or not isinstance(declared_size, int)
                or declared_size < 0
            ):
                valid = False
                break
            artifact = generation / relative
            try:
                if artifact.is_symlink() or not artifact.is_file():
                    valid = False
                    break
                resolved = artifact.resolve(strict=True)
                if not resolved.is_relative_to(generation_root):
                    valid = False
                    break
                info = artifact.stat()
            except OSError:
                valid = False
                break
            if info.st_size != declared_size:
                valid = False
                break
            total += _round_up(info.st_size)
        if valid:
            largest = max(largest, total)
    return largest


def forecast_full_sqlite_build_cache_misses(
    *,
    repository_root: Path,
    stop_data_root: Path,
    gtfs_artifacts: dict,
    normalized_cache_root: Path,
    runtime_provider_ids: str | None,
    environ: dict[str, str] | None = None,
) -> dict:
    """Forecast only the 13 build-time transform caches used by full-SQLite mode.

    The unified database pipeline deliberately publishes no provider runtime
    shards or departure partitions. For a transformed-cache miss, charge a
    fresh copy based on the largest validated previous generation. A cold
    provider is conservatively bounded by four times its expanded GTFS input.
    """
    from scripts.external_build_cache import (
        cache_enabled,
        cache_provider_allowed,
        transformed_cache_enabled,
    )
    from scripts.run_incremental_provider_pipeline import (
        _external_build_cache_root,
        _probe_external_build_cache,
        _provider_cities,
        _raw_entry,
        _source_map,
        provider_selection_plan,
        resolve_runtime_provider_ids,
    )

    values = dict(os.environ if environ is None else environ)
    selection = provider_selection_plan(
        repository_root,
        environ=values,
        require_departure_cache_capabilities=False,
    )
    selected = tuple(str(value) for value in selection["selectedProviders"])
    runtime_ids = resolve_runtime_provider_ids(
        repository_root,
        selected,
        runtime_provider_ids=runtime_provider_ids,
        environ=values,
    )
    if runtime_ids:
        raise ValueError(
            "full-SQLite disk forecast requires an empty runtime provider list"
        )
    if not cache_enabled(values) or not transformed_cache_enabled(values):
        raise ValueError("full-SQLite transformed build cache is disabled")

    sources = _source_map(repository_root)
    cache_root = _external_build_cache_root(normalized_cache_root, values)
    provider_reports = []
    total_bytes = 0
    for provider_id in selected:
        if not cache_provider_allowed(provider_id, values):
            raise ValueError(
                f"full-SQLite build cache provider is not allowlisted: {provider_id}"
            )
        source = sources[provider_id]
        raw_path, raw_sha = _raw_entry(
            gtfs_artifacts,
            provider_id,
            artifact_group=str(source.get("artifactGroup", "external")),
        )
        cities = _provider_cities(source, repository_root, stop_data_root)
        key, lookup, city_ids = _probe_external_build_cache(
            repository_root=repository_root,
            provider_id=provider_id,
            raw_sha=raw_sha,
            source=source,
            cities=cities,
            sources=sources,
            normalized_cache_root=normalized_cache_root,
            environ=values,
        )
        status = lookup.status
        if status in REUSABLE:
            additional_bytes = 0
            baseline_bytes = 0
        elif status in {"MISS", "INVALID"}:
            provider_cache_root = cache_root / provider_id
            baseline_bytes = _largest_valid_build_cache_generation(
                provider_cache_root
            )
            if baseline_bytes == 0:
                with zipfile.ZipFile(raw_path) as archive:
                    expanded_bytes = sum(
                        item.file_size for item in archive.infolist()
                    )
                if expanded_bytes <= 0:
                    raise ValueError(
                        f"provider={provider_id} raw GTFS input is empty"
                    )
                baseline_bytes = expanded_bytes * 4
            additional_bytes = _round_up(
                (
                    baseline_bytes * MISS_GROWTH_NUMERATOR
                    + MISS_GROWTH_DENOMINATOR
                    - 1
                )
                // MISS_GROWTH_DENOMINATOR
            )
        else:
            raise ValueError(
                f"provider={provider_id} transformed cache status cannot be forecast: {status}"
            )
        total_bytes += additional_bytes
        provider_reports.append(
            {
                "providerID": provider_id,
                "status": status,
                "cacheKey": key.value,
                "baselineBytes": baseline_bytes,
                "additionalBytes": additional_bytes,
                "reason": lookup.reason,
            }
        )
    return {
        "mode": "full-sqlite-build-cache-only",
        "runtimeProviders": [],
        "additionalBytes": total_bytes,
        "additionalKiB": (total_bytes + 1023) // 1024,
        "providers": provider_reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--stop-data", type=Path, required=True)
    parser.add_argument("--gtfs-artifacts", type=Path, required=True)
    parser.add_argument("--normalized-cache-root", type=Path, required=True)
    parser.add_argument("--static-artifact-root", type=Path, required=True)
    parser.add_argument("--active-release", type=Path)
    parser.add_argument("--runtime-provider-ids")
    parser.add_argument("--full-sqlite", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.repository))
    artifacts = json.loads(args.gtfs_artifacts.read_text())
    if args.full_sqlite:
        forecast = forecast_full_sqlite_build_cache_misses(
            repository_root=args.repository,
            stop_data_root=args.stop_data,
            gtfs_artifacts=artifacts,
            normalized_cache_root=args.normalized_cache_root,
            runtime_provider_ids=args.runtime_provider_ids,
        )
    else:
        from scripts.run_incremental_provider_pipeline import (
            full_chain_preflight, provider_selection_plan,
            resolve_runtime_provider_ids, service_dates,
        )
        # Probe records stay diagnostic; stdout has exactly one numeric result.
        with contextlib.redirect_stdout(io.StringIO()):
            selection = provider_selection_plan(args.repository)
            runtime_provider_ids = resolve_runtime_provider_ids(
                args.repository,
                selection["selectedProviders"],
                runtime_provider_ids=args.runtime_provider_ids,
            )
            plan = full_chain_preflight(
                repository_root=args.repository, stop_data_root=args.stop_data,
                normalized_cache_root=args.normalized_cache_root,
                static_artifact_root=args.static_artifact_root,
                dates=service_dates(valid_from=None, valid_through=None, window_days=21),
                provider_ids=tuple(selection["selectedProviders"]), raw_snapshot=None,
                runtime_provider_ids=runtime_provider_ids,
                gtfs_artifacts=artifacts,
            )
        forecast = forecast_provider_allocations(
            plan, args.active_release, normalized_cache_root=args.normalized_cache_root,
        )
    print("[Nightly] provider-disk-forecast " + json.dumps(forecast), file=sys.stderr, flush=True)
    print(forecast["additionalKiB"])


if __name__ == "__main__":
    main()
