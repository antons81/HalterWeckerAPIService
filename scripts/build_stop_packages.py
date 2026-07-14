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
from datetime import date, datetime, time, timedelta
from pathlib import Path

EARTH_RADIUS_METERS = 6_371_000
CITY_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SUPPORTED_TRANSIT_RADAR_ADAPTERS = {
    "dbRegioBusNRW",
    "shgMobil",
    "stadtwerkeMuenster",
    "swu",
    "vbb"
}


def normalized(value: str) -> str:
    return value.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss").strip()


def distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))


def load_gtfs_archive(url: str) -> zipfile.ZipFile:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "HalteWeckerStopPipeline/1.0"}
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return zipfile.ZipFile(io.BytesIO(response.read()))


def load_table(archive: zipfile.ZipFile, filename: str) -> list[dict[str, str]]:
    if filename not in archive.namelist():
        return []
    with archive.open(filename) as file:
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

    if adapter == "swu":
        if city_id != "ulm" or region is not None:
            raise ValueError(f"Invalid SWU configuration for {city_id}")
        return

    if adapter == "shgMobil":
        if city_id != "schaumburg" or region is not None:
            raise ValueError(f"Invalid SHG Mobil configuration for {city_id}")
        return

    if adapter == "vbb":
        if city_id != "berlin" or not isinstance(region, dict):
            raise ValueError(f"Invalid VBB configuration for {city_id}")

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
            elif adapter == "swu":
                provider_id = "swu-ulm"
            elif adapter == "vbb":
                provider_id = "vbb-berlin"
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


def parse_gtfs_time(value: str) -> int:
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def build_vbb_network_index(archive: zipfile.ZipFile, output: Path) -> None:
    stops = {row["stop_id"]: row for row in load_table(archive, "stops.txt") if row.get("stop_id")}
    berlin_stop_ids = set()
    for stop_id, row in stops.items():
        try:
            latitude, longitude = float(row["stop_lat"]), float(row["stop_lon"])
        except (KeyError, ValueError):
            continue
        if 52.3383 <= latitude <= 52.6755 and 13.0884 <= longitude <= 13.7612:
            berlin_stop_ids.add(stop_id)

    stop_times = load_table(archive, "stop_times.txt")
    berlin_trip_ids = {row["trip_id"] for row in stop_times if row.get("stop_id") in berlin_stop_ids}
    times_by_trip: dict[str, list[dict[str, str]]] = {}
    used_stop_ids = set()
    for row in stop_times:
        trip_id = row.get("trip_id", "")
        if trip_id not in berlin_trip_ids:
            continue
        times_by_trip.setdefault(trip_id, []).append(row)
        used_stop_ids.add(row.get("stop_id", ""))

    routes = {row["route_id"]: row for row in load_table(archive, "routes.txt") if row.get("route_id")}
    trip_templates = []
    for row in load_table(archive, "trips.txt"):
        trip_id = row.get("trip_id", "")
        trip_times = times_by_trip.get(trip_id)
        if not trip_times:
            continue
        route = routes.get(row.get("route_id", ""), {})
        trip_times.sort(key=lambda item: int(item.get("stop_sequence", "0")))
        try:
            compact_times = [{
                "stopID": item["stop_id"],
                "arrivalSeconds": parse_gtfs_time(item["arrival_time"]),
                "departureSeconds": parse_gtfs_time(item["departure_time"])
            } for item in trip_times]
        except (KeyError, ValueError):
            continue
        service_id = row.get("service_id", "")
        trip_templates.append({
            "id": trip_id,
            "routeID": row.get("route_id", ""),
            "lineName": route.get("route_short_name") or route.get("route_long_name") or row.get("route_id", ""),
            "directionName": row.get("trip_headsign", ""),
            "serviceID": service_id,
            "stopTimes": compact_times
        })

    calendar_dates: dict[str, dict[str, list[str]]] = {}
    for row in load_table(archive, "calendar_dates.txt"):
        service_id = row.get("service_id", "")
        key = "addedDates" if row.get("exception_type") == "1" else "removedDates"
        calendar_dates.setdefault(service_id, {"addedDates": [], "removedDates": []})[key].append(row.get("date", ""))

    calendar_by_id = {row["service_id"]: row for row in load_table(archive, "calendar.txt") if row.get("service_id")}

    def service_is_active(service_id: str, service_date: date) -> bool:
        date_key = service_date.strftime("%Y%m%d")
        exceptions = calendar_dates.get(service_id, {"addedDates": [], "removedDates": []})
        if date_key in exceptions["removedDates"]:
            return False
        if date_key in exceptions["addedDates"]:
            return True
        row = calendar_by_id.get(service_id)
        if not row or not row.get("start_date", "") <= date_key <= row.get("end_date", ""):
            return False
        weekday = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[service_date.weekday()]
        return row.get(weekday) == "1"

    first_target_date = date.today()
    target_dates = {first_target_date + timedelta(days=offset) for offset in range(3)}
    packages: dict[str, list[dict[str, object]]] = {}
    for day_offset in range(-1, 3):
        service_date = first_target_date + timedelta(days=day_offset)
        service_midnight = datetime.combine(service_date, time.min)
        for template in trip_templates:
            if not service_is_active(str(template["serviceID"]), service_date):
                continue
            trip_stop_times = template["stopTimes"]
            if not trip_stop_times:
                continue
            start = service_midnight + timedelta(seconds=int(trip_stop_times[0]["arrivalSeconds"]))
            end = service_midnight + timedelta(seconds=int(trip_stop_times[-1]["departureSeconds"]))
            hour = start.replace(minute=0, second=0, microsecond=0)
            while hour <= end:
                if hour.date() in target_dates:
                    key = hour.strftime("%Y%m%d-%H")
                    trip = {key: value for key, value in template.items() if key != "serviceID"}
                    trip["serviceDate"] = service_date.strftime("%Y%m%d")
                    packages.setdefault(key, []).append(trip)
                hour += timedelta(hours=1)

    transit_output = output / "transit" / "vbb"
    transit_output.mkdir(parents=True, exist_ok=True)
    for key, trips in packages.items():
        package_stop_ids = {
            stop_time["stopID"] for trip in trips for stop_time in trip["stopTimes"]
        }
        compact_stops = []
        for stop_id in package_stop_ids:
            row = stops.get(str(stop_id))
            if not row:
                continue
            try:
                compact_stops.append({"id": stop_id, "latitude": float(row["stop_lat"]), "longitude": float(row["stop_lon"])})
            except (KeyError, ValueError):
                continue
        payload = {"version": date.today().isoformat(), "stops": compact_stops, "trips": trips}
        (transit_output / f"{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs-url", required=True)
    parser.add_argument(
        "--vbb-gtfs-url",
        default="https://unternehmen.vbb.de/fileadmin/user_upload/VBB/Dokumente/API-Datensaetze/gtfs-mastscharf/GTFS.zip"
    )
    parser.add_argument("--cities", default="config/cities.json")
    parser.add_argument("--output", default="docs/data")
    args = parser.parse_args()

    cities = load_cities(Path(args.cities))
    archive = load_gtfs_archive(args.gtfs_url)
    stops = canonicalize(load_table(archive, "stops.txt"))
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
    build_vbb_network_index(load_gtfs_archive(args.vbb_gtfs_url), output)
    print(f"Built {len(manifest)} city packages from {len(stops)} canonical stops.")


if __name__ == "__main__":
    main()
