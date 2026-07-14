#!/usr/bin/env python3
"""Create static HalteWecker stop packages from a GTFS ZIP feed."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

EARTH_RADIUS_METERS = 6_371_000
CITY_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SUPPORTED_TRANSIT_RADAR_ADAPTERS = {"dbRegioBusNRW", "shgMobil", "stadtwerkeMuenster"}


def normalized(value: str) -> str:
    return value.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss").strip()


def distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))


def load_stops(url: str) -> list[dict[str, str]]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "HalteWeckerStopPipeline/1.0"}
    )
    with urllib.request.urlopen(request, timeout=180) as response:
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


def load_cities(path: Path) -> list[dict[str, object]]:
    cities = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cities, list) or not cities:
        raise ValueError("City configuration must contain a non-empty JSON array.")

    seen_ids: set[str] = set()
    for city in cities:
        city_id = city.get("id")
        name = city.get("name")
        aliases = city.get("aliases", [])
        latitude = city.get("latitude")
        longitude = city.get("longitude")
        radius = city.get("radiusMeters")
        transit_radar = city.get("transitRadar")

        if not isinstance(city_id, str) or not CITY_ID_PATTERN.fullmatch(city_id):
            raise ValueError(f"Invalid city id: {city_id!r}")
        if city_id in seen_ids:
            raise ValueError(f"Duplicate city id: {city_id}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Invalid city name for {city_id}")
        if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
            raise ValueError(f"Invalid aliases for {city_id}")
        if not isinstance(latitude, (int, float)) or not -90 <= latitude <= 90:
            raise ValueError(f"Invalid latitude for {city_id}")
        if not isinstance(longitude, (int, float)) or not -180 <= longitude <= 180:
            raise ValueError(f"Invalid longitude for {city_id}")
        if not isinstance(radius, (int, float)) or radius <= 0:
            raise ValueError(f"Invalid radius for {city_id}")
        if transit_radar is not None:
            validate_transit_radar(city_id, latitude, longitude, transit_radar)

        seen_ids.add(city_id)

    return cities


def validate_transit_radar(
    city_id: str,
    latitude: float,
    longitude: float,
    configuration: object
) -> None:
    configurations = transit_radar_configurations(configuration)
    for provider_configuration in configurations:
        validate_transit_radar_provider(
            city_id,
            latitude,
            longitude,
            provider_configuration
        )


def transit_radar_configurations(configuration: object) -> list[dict[str, object]]:
    if isinstance(configuration, dict):
        return [configuration]
    if isinstance(configuration, list) and configuration and all(
        isinstance(item, dict) for item in configuration
    ):
        return configuration
    raise ValueError("Invalid transit radar configuration")


def validate_transit_radar_provider(
    city_id: str,
    latitude: float,
    longitude: float,
    configuration: dict[str, object]
) -> None:
    if not isinstance(configuration, dict):
        raise ValueError(f"Invalid transit radar configuration for {city_id}")

    adapter = configuration.get("adapter")
    region = configuration.get("region")
    is_enabled = configuration.get("isEnabled", True)
    if adapter not in SUPPORTED_TRANSIT_RADAR_ADAPTERS:
        raise ValueError(f"Invalid transit radar adapter for {city_id}")
    if not isinstance(is_enabled, bool):
        raise ValueError(f"Invalid transit radar availability for {city_id}")

    if adapter == "stadtwerkeMuenster":
        if city_id != "munster" or region is not None:
            raise ValueError(f"Invalid Stadtwerke Münster configuration for {city_id}")
        return

    if adapter == "shgMobil":
        if city_id != "schaumburg" or region is not None:
            raise ValueError(f"Invalid SHG Mobil configuration for {city_id}")
        return

    if not isinstance(region, dict):
        raise ValueError(f"Invalid transit radar region for {city_id}")

    required_bounds = (
        "minimumLongitude",
        "minimumLatitude",
        "maximumLongitude",
        "maximumLatitude"
    )
    if not all(isinstance(region.get(key), (int, float)) for key in required_bounds):
        raise ValueError(f"Invalid transit radar region for {city_id}")
    if not region["minimumLatitude"] < region["maximumLatitude"]:
        raise ValueError(f"Invalid transit radar latitude bounds for {city_id}")
    if not region["minimumLongitude"] < region["maximumLongitude"]:
        raise ValueError(f"Invalid transit radar longitude bounds for {city_id}")
    if region["maximumLatitude"] - region["minimumLatitude"] > 1:
        raise ValueError(f"Transit radar latitude span is too large for {city_id}")
    if region["maximumLongitude"] - region["minimumLongitude"] > 1:
        raise ValueError(f"Transit radar longitude span is too large for {city_id}")
    if not region["minimumLatitude"] <= latitude <= region["maximumLatitude"]:
        raise ValueError(f"Transit radar region misses the center of {city_id}")
    if not region["minimumLongitude"] <= longitude <= region["maximumLongitude"]:
        raise ValueError(f"Transit radar region misses the center of {city_id}")


def transit_radar_manifest(cities: list[dict[str, object]]) -> dict[str, object]:
    radar_cities = []
    for city in cities:
        configuration = city.get("transitRadar")
        if configuration is None:
            continue

        city_id = str(city["id"])
        providers = []
        for provider_configuration in transit_radar_configurations(configuration):
            adapter = str(provider_configuration["adapter"])
            if adapter == "dbRegioBusNRW":
                provider_id = f"db-regio-bus-nrw-{city_id}"
            elif adapter == "shgMobil":
                provider_id = "shg-mobil-schaumburg"
            elif adapter == "stadtwerkeMuenster":
                provider_id = "stadtwerke-muenster"
            else:
                raise ValueError(f"Unsupported transit radar adapter for {city_id}")

            provider = {
                "providerID": provider_id,
                "adapter": adapter,
                "isEnabled": provider_configuration.get("isEnabled", True),
                "isExperimental": True,
                "features": ["liveVehicles", "realtimeDelay"],
                "statusMessage": f'Live-Radar für {city["name"]}'
            }
            region = provider_configuration.get("region")
            if isinstance(region, dict):
                provider["region"] = region
            providers.append(provider)

        radar_cities.append({
            "cityID": f"{city_id}-de",
            "appCityID": city_id,
            "name": city["name"],
            "center": {
                "latitude": city["latitude"],
                "longitude": city["longitude"]
            },
            "providers": providers
        })

    return {"schemaVersion": 1, "cities": radar_cities}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs-url", required=True)
    parser.add_argument("--cities", default="config/cities.json")
    parser.add_argument("--output", default="docs/data")
    args = parser.parse_args()

    cities = load_cities(Path(args.cities))
    stops = canonicalize(load_stops(args.gtfs_url))
    output = Path(args.output)
    packages = output / "stops"
    packages.mkdir(parents=True, exist_ok=True)
    manifest = []
    for city in cities:
        city_stops = [
            stop for stop in stops
            if distance_meters(
                float(stop["latitude"]),
                float(stop["longitude"]),
                float(city["latitude"]),
                float(city["longitude"])
            ) <= float(city["radiusMeters"])
        ]
        city_stops.sort(key=lambda stop: (str(stop["searchName"]), str(stop["id"])))
        if not city_stops:
            raise ValueError(f'No stops found for configured city {city["id"]}.')

        filename = f'{city["id"]}.json'
        (packages / filename).write_text(json.dumps(city_stops, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        manifest.append({
            "id": city["id"],
            "name": city["name"],
            "aliases": city.get("aliases", []),
            "stopCount": len(city_stops),
            "url": f"stops/{filename}"
        })
    (output / "cities.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "manifest.json").write_text(
        json.dumps(
            {"version": date.today().isoformat(), "cities": manifest},
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )
    (output / "transit-radar-cities.json").write_text(
        json.dumps(transit_radar_manifest(cities), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"Built {len(manifest)} city packages from {len(stops)} canonical stops.")


if __name__ == "__main__":
    main()
