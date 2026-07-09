#!/usr/bin/env python3
"""Create static HalteWecker stop packages from a GTFS ZIP feed."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import urllib.request
import zipfile
from pathlib import Path

EARTH_RADIUS_METERS = 6_371_000


def normalized(value: str) -> str:
    return value.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss").strip()


def distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))


def load_stops(url: str) -> list[dict[str, str]]:
    with urllib.request.urlopen(url, timeout=180) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    with archive.open("stops.txt") as file:
        return list(csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig")))


def canonicalize(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    by_id = {row["stop_id"]: row for row in rows if row.get("stop_id")}
    unique: dict[tuple[str, int, int], dict[str, object]] = {}
    for row in by_id.values():
        source = by_id.get(row.get("parent_station", "")) or row
        try:
            latitude, longitude = float(source["stop_lat"]), float(source["stop_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        name = source.get("stop_name", "").strip()
        if not name:
            continue
        key = normalized(name), round(latitude * 10_000), round(longitude * 10_000)
        unique.setdefault(key, {
            "id": source["stop_id"], "name": name, "latitude": latitude,
            "longitude": longitude, "searchName": normalized(name)
        })
    return list(unique.values())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs-url", required=True)
    parser.add_argument("--cities", default="config/cities.json")
    parser.add_argument("--output", default="docs/data")
    args = parser.parse_args()

    cities = json.loads(Path(args.cities).read_text(encoding="utf-8"))
    stops = canonicalize(load_stops(args.gtfs_url))
    output = Path(args.output)
    packages = output / "stops"
    packages.mkdir(parents=True, exist_ok=True)
    manifest = []
    for city in cities:
        city_stops = [stop for stop in stops if distance_meters(float(stop["latitude"]), float(stop["longitude"]), city["latitude"], city["longitude"]) <= city["radiusMeters"]]
        city_stops.sort(key=lambda stop: (str(stop["searchName"]), str(stop["id"])))
        filename = f'{city["id"]}.json'
        (packages / filename).write_text(json.dumps(city_stops, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        manifest.append({"id": city["id"], "name": city["name"], "stopCount": len(city_stops), "url": f"stops/{filename}"})
    (output / "cities.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps({"version": __import__("datetime").date.today().isoformat(), "cities": manifest}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Built {len(manifest)} city packages from {len(stops)} canonical stops.")


if __name__ == "__main__":
    main()
