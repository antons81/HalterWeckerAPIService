#!/usr/bin/env python3
"""Forecast new provider allocations from read-only cache probes.

Created by Anton on 2026-10-11.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import io
import sys
import zipfile
from pathlib import Path

MIB = 1024 ** 2
REUSABLE = frozenset({"HIT", "HIT_COMPATIBLE"})


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--stop-data", type=Path, required=True)
    parser.add_argument("--gtfs-artifacts", type=Path, required=True)
    parser.add_argument("--normalized-cache-root", type=Path, required=True)
    parser.add_argument("--static-artifact-root", type=Path, required=True)
    parser.add_argument("--active-release", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.repository))
    from scripts.run_incremental_provider_pipeline import (
        full_chain_preflight, provider_selection_plan, service_dates,
    )
    # Probe records stay diagnostic; stdout has exactly one numeric result.
    with contextlib.redirect_stdout(io.StringIO()):
        selection = provider_selection_plan(args.repository)
        plan = full_chain_preflight(
            repository_root=args.repository, stop_data_root=args.stop_data,
            normalized_cache_root=args.normalized_cache_root,
            static_artifact_root=args.static_artifact_root,
            dates=service_dates(valid_from=None, valid_through=None, window_days=21),
            provider_ids=tuple(selection["selectedProviders"]), raw_snapshot=None,
            gtfs_artifacts=json.loads(args.gtfs_artifacts.read_text()),
        )
    forecast = forecast_provider_allocations(
        plan, args.active_release, normalized_cache_root=args.normalized_cache_root,
    )
    print("[Nightly] provider-disk-forecast " + json.dumps(forecast), file=sys.stderr, flush=True)
    print(forecast["additionalKiB"])


if __name__ == "__main__":
    main()
