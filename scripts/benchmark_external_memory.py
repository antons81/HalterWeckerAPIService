#!/usr/bin/env python3
"""Reproducible external-feed memory benchmark.

Run this under `/usr/bin/time -v` on the deployment host. The benchmark uses
the same external source registry and process path as stop-data, but writes to
a caller-provided staging directory and never activates production data.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from build_stop_packages import load_gtfs_archive
from external_gtfs import parse_external_gtfs_url_args, process_external_gtfs_sources


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", required=True, help="External source registry JSON")
    parser.add_argument("--source", action="append", required=True, dest="source_ids")
    parser.add_argument("--external-gtfs-url", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    url_by_provider = parse_external_gtfs_url_args(args.external_gtfs_url)
    manifest, cities, packages, lines = process_external_gtfs_sources(
        repository_root=Path(__file__).resolve().parents[1],
        sources_path=Path(args.sources),
        url_by_provider=url_by_provider,
        output=args.output,
        load_gtfs_archive=load_gtfs_archive,
        selected_source_ids=set(args.source_ids),
    )
    print(
        f"benchmark sources={','.join(args.source_ids)} "
        f"cities={len(cities)} manifest={len(manifest)} "
        f"packages={len(packages)} lines={len(lines)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
