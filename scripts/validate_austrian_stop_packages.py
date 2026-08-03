#!/usr/bin/env python3
"""Validate every Austrian package against the shared source registry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from austrian_sources import load_austrian_sources


EXPECTED_CITIES = {"wien", "graz", "linz", "salzburg", "innsbruck", "klagenfurt", "st-poelten", "bregenz"}


def validate(stop_data: Path, registry_path: Path) -> dict[str, int]:
    registry = load_austrian_sources(registry_path)
    cities = {city for source in registry for city in source["cities"]}
    if cities != EXPECTED_CITIES:
        raise ValueError(f"Registry city coverage mismatch: {sorted(cities)}")
    counts: dict[str, int] = {}
    for city in sorted(cities):
        path = stop_data / "stops" / f"{city}.json"
        if not path.is_file():
            raise ValueError(f"Missing Austrian stop package: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"Austrian stop package is empty or invalid: {city}")
        ids: set[str] = set()
        for stop in payload:
            if not isinstance(stop, dict) or not all(key in stop for key in ("id", "name", "latitude", "longitude")):
                raise ValueError(f"Invalid stop row in Austrian package: {city}")
            stop_id = str(stop["id"])
            if stop_id in ids:
                raise ValueError(f"Duplicate stop ID {stop_id} in Austrian package {city}")
            ids.add(stop_id)
            if not (-90 <= float(stop["latitude"]) <= 90 and -180 <= float(stop["longitude"]) <= 180):
                raise ValueError(f"Invalid coordinates for {stop_id} in {city}")
            for alias in stop.get("sourceStopIDs", []):
                if not isinstance(alias, str) or not alias:
                    raise ValueError(f"Invalid sourceStopIDs entry for {stop_id}")
        counts[city] = len(ids)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop-data", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("config/austrian-sources.json"))
    args = parser.parse_args()
    print(json.dumps({"cities": validate(args.stop_data, args.registry)}, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
