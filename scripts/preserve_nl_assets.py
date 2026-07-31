#!/usr/bin/env python3
"""Keep the last valid Dutch assets when the optional NL GTFS upstream is unavailable."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from build_stop_packages import load_cities, nl_city_ids, normalized


def copy_if_present(source: Path, destination: Path) -> None:
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def preserve_nl_assets(current: Path, output: Path, cities_path: Path) -> None:
    nl_ids = nl_city_ids(load_cities(cities_path))
    previous_manifest = json.loads((current / "manifest.json").read_text(encoding="utf-8"))
    next_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    previous_by_id = {str(city["id"]): city for city in previous_manifest["cities"]}
    next_ids = {str(city["id"]) for city in next_manifest["cities"]}

    for city_id in sorted(nl_ids):
        city = previous_by_id.get(city_id)
        if city is None:
            raise ValueError(f"Cannot preserve Dutch city absent from current manifest: {city_id}")
        city["country"] = "NL"
        if city_id not in next_ids:
            next_manifest["cities"].append(city)
        copy_if_present(current / "stops" / f"{city_id}.json", output / "stops" / f"{city_id}.json")
        copy_if_present(current / "routes" / f"{city_id}.json", output / "routes" / f"{city_id}.json")
        copy_if_present(current / "departures" / f"{city_id}.json", output / "departures" / f"{city_id}.json")
        copy_if_present(current / "transit" / "city-lines" / f"{city_id}.json", output / "transit" / "city-lines" / f"{city_id}.json")

    next_manifest["cities"].sort(key=lambda city: (normalized(str(city["name"])), str(city["id"])))
    (output / "manifest.json").write_text(
        json.dumps(next_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "cities.json").write_text(
        json.dumps(next_manifest["cities"], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cities", default="config/cities.json")
    args = parser.parse_args()
    preserve_nl_assets(Path(args.current), Path(args.output), Path(args.cities))


if __name__ == "__main__":
    main()
