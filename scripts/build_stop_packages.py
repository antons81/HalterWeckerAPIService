#!/usr/bin/env python3
"""Create static HalteWecker stop packages from a GTFS ZIP feed."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import unicodedata
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable

EARTH_RADIUS_METERS = 6_371_000
CITY_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BKG_MUNICIPALITIES_URL = (
    "https://sgx.geodatenzentrum.de/wfs_vg250"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=vg250:vg250_gem&srsName=EPSG:4326"
    "&outputFormat=application/json"
)
BKG_PAGE_SIZE = 2_000
SPATIAL_GRID_SIZE_DEGREES = 0.25
SUPPORTED_TRANSIT_RADAR_ADAPTERS = {
    "dbRegioBusNRW",
    "ivantoMQTT",
    "ruhrbahn",
    "shgMobil",
    "stadtwerkeMuenster",
    "swu",
    "vbb",
    "vrrEFA"
}


def normalized(value: str) -> str:
    return value.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss").strip()


def identifier_component(value: str) -> str:
    folded = unicodedata.normalize("NFKD", normalized(value))
    ascii_value = folded.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-") or "gemeinde"


def distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))


def load_gtfs_archive(url: str) -> zipfile.ZipFile:
    parsed_url = urlsplit(url)
    if parsed_url.scheme in ("", "file"):
        path = Path(parsed_url.path if parsed_url.scheme == "file" else url)
        return zipfile.ZipFile(path)

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "HalteWeckerStopPipeline/1.0"}
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return zipfile.ZipFile(io.BytesIO(response.read()))


def paged_url(url: str, start_index: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({"count": str(BKG_PAGE_SIZE), "startIndex": str(start_index)})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def load_municipality_features(source: str) -> list[dict[str, object]]:
    parsed_source = urlsplit(source)
    if parsed_source.scheme in ("", "file"):
        path = Path(parsed_source.path if parsed_source.scheme == "file" else source)
        payload = json.loads(path.read_text(encoding="utf-8"))
        features = payload.get("features", [])
        if not isinstance(features, list):
            raise ValueError("Municipality GeoJSON must contain a features array.")
        return features

    features: list[dict[str, object]] = []
    start_index = 0
    expected_count: int | None = None
    while expected_count is None or start_index < expected_count:
        request = urllib.request.Request(
            paged_url(source, start_index),
            headers={"User-Agent": "HalteWeckerStopPipeline/1.0"}
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.load(response)

        page = payload.get("features", [])
        if not isinstance(page, list):
            raise ValueError("Municipality WFS response must contain a features array.")
        if expected_count is None:
            expected_count = int(payload.get("numberMatched", len(page)))
        features.extend(page)
        if not page:
            break
        start_index += len(page)

    if expected_count is not None and len(features) != expected_count:
        raise ValueError(
            f"Municipality WFS returned {len(features)} of {expected_count} features."
        )
    return features


def valid_bbox(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        minimum_longitude, minimum_latitude, maximum_longitude, maximum_latitude = (
            float(component) for component in value
        )
    except (TypeError, ValueError):
        return None
    if minimum_longitude > maximum_longitude or minimum_latitude > maximum_latitude:
        return None
    return minimum_longitude, minimum_latitude, maximum_longitude, maximum_latitude


def load_municipalities(source: str) -> list[dict[str, object]]:
    municipalities = []
    seen_codes: set[str] = set()
    for feature in load_municipality_features(source):
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        geometry = feature.get("geometry")
        bbox = valid_bbox(feature.get("bbox"))
        if not isinstance(properties, dict) or not isinstance(geometry, dict) or bbox is None:
            continue

        code = str(properties.get("ags", "")).strip()
        name = str(properties.get("gen", "")).strip()
        state = str(properties.get("lkz", "")).strip()
        if not code or not name or code in seen_codes:
            continue
        if geometry.get("type") not in ("Polygon", "MultiPolygon"):
            continue

        municipalities.append({
            "code": code,
            "name": name,
            "state": state,
            "bbox": bbox,
            "geometry": geometry
        })
        seen_codes.add(code)

    if not municipalities:
        raise ValueError("No valid German municipalities were loaded from BKG.")
    return municipalities


def grid_coordinate(value: float) -> int:
    return math.floor(value / SPATIAL_GRID_SIZE_DEGREES)


def municipality_spatial_index(
    municipalities: list[dict[str, object]]
) -> dict[tuple[int, int], list[int]]:
    index: dict[tuple[int, int], list[int]] = {}
    for municipality_index, municipality in enumerate(municipalities):
        minimum_longitude, minimum_latitude, maximum_longitude, maximum_latitude = municipality["bbox"]
        for longitude_cell in range(
            grid_coordinate(float(minimum_longitude)),
            grid_coordinate(float(maximum_longitude)) + 1
        ):
            for latitude_cell in range(
                grid_coordinate(float(minimum_latitude)),
                grid_coordinate(float(maximum_latitude)) + 1
            ):
                index.setdefault((longitude_cell, latitude_cell), []).append(municipality_index)
    return index


def point_is_on_segment(
    longitude: float,
    latitude: float,
    start: list[float],
    end: list[float]
) -> bool:
    start_longitude, start_latitude = float(start[0]), float(start[1])
    end_longitude, end_latitude = float(end[0]), float(end[1])
    cross_product = (
        (latitude - start_latitude) * (end_longitude - start_longitude)
        - (longitude - start_longitude) * (end_latitude - start_latitude)
    )
    if abs(cross_product) > 1e-10:
        return False
    return (
        min(start_longitude, end_longitude) - 1e-10
        <= longitude
        <= max(start_longitude, end_longitude) + 1e-10
        and min(start_latitude, end_latitude) - 1e-10
        <= latitude
        <= max(start_latitude, end_latitude) + 1e-10
    )


def point_is_in_ring(longitude: float, latitude: float, ring: object) -> bool:
    if not isinstance(ring, list) or len(ring) < 4:
        return False

    inside = False
    previous = ring[-1]
    for current in ring:
        if not isinstance(previous, list) or not isinstance(current, list):
            previous = current
            continue
        if len(previous) < 2 or len(current) < 2:
            previous = current
            continue
        if point_is_on_segment(longitude, latitude, previous, current):
            return True

        previous_longitude, previous_latitude = float(previous[0]), float(previous[1])
        current_longitude, current_latitude = float(current[0]), float(current[1])
        crosses_latitude = (current_latitude > latitude) != (previous_latitude > latitude)
        if crosses_latitude:
            intersection = (
                (previous_longitude - current_longitude)
                * (latitude - current_latitude)
                / (previous_latitude - current_latitude)
                + current_longitude
            )
            if longitude < intersection:
                inside = not inside
        previous = current
    return inside


def point_is_in_polygon(longitude: float, latitude: float, polygon: object) -> bool:
    if not isinstance(polygon, list) or not polygon:
        return False
    if not point_is_in_ring(longitude, latitude, polygon[0]):
        return False
    return not any(point_is_in_ring(longitude, latitude, hole) for hole in polygon[1:])


def point_is_in_geometry(longitude: float, latitude: float, geometry: object) -> bool:
    if not isinstance(geometry, dict):
        return False
    coordinates = geometry.get("coordinates")
    if geometry.get("type") == "Polygon":
        return point_is_in_polygon(longitude, latitude, coordinates)
    if geometry.get("type") == "MultiPolygon" and isinstance(coordinates, list):
        return any(point_is_in_polygon(longitude, latitude, polygon) for polygon in coordinates)
    return False


def municipality_for_coordinate(
    latitude: float,
    longitude: float,
    municipalities: list[dict[str, object]],
    spatial_index: dict[tuple[int, int], list[int]]
) -> dict[str, object] | None:
    candidates = spatial_index.get(
        (grid_coordinate(longitude), grid_coordinate(latitude)),
        []
    )
    matches = []
    for candidate_index in candidates:
        municipality = municipalities[candidate_index]
        minimum_longitude, minimum_latitude, maximum_longitude, maximum_latitude = municipality["bbox"]
        if not (
            float(minimum_longitude) <= longitude <= float(maximum_longitude)
            and float(minimum_latitude) <= latitude <= float(maximum_latitude)
        ):
            continue
        if point_is_in_geometry(longitude, latitude, municipality["geometry"]):
            matches.append(municipality)

    if not matches:
        return None
    return min(
        matches,
        key=lambda municipality: (
            float(municipality["bbox"][2]) - float(municipality["bbox"][0])
        ) * (
            float(municipality["bbox"][3]) - float(municipality["bbox"][1])
        )
    )


def load_table(archive: zipfile.ZipFile, filename: str) -> list[dict[str, str]]:
    if filename not in archive.namelist():
        return []
    with archive.open(filename) as file:
        return list(csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig")))


def iter_table(
    archive: zipfile.ZipFile,
    filename: str
) -> Iterable[dict[str, str]]:
    if filename not in archive.namelist():
        return
    with archive.open(filename) as file:
        yield from csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig"))


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


def canonical_stop_id_by_stop_id(
    rows: list[dict[str, str]]
) -> dict[str, str]:
    by_id = {row["stop_id"]: row for row in rows if row.get("stop_id")}
    return {
        stop_id: (by_id.get(row.get("parent_station", "")) or row)["stop_id"]
        for stop_id, row in by_id.items()
    }


def line_names(route: dict[str, str]) -> list[str]:
    names = []
    for key in ("route_short_name", "route_long_name"):
        value = route.get(key, "").strip()
        if value and value not in names:
            names.append(value)
    return names


def build_lines_by_stop_id(
    stop_rows: list[dict[str, str]],
    stop_times: Iterable[dict[str, str]],
    trips: list[dict[str, str]],
    routes: list[dict[str, str]],
    included_stop_ids: set[str] | None = None
) -> dict[str, dict[str, dict[str, object]]]:
    canonical_ids = canonical_stop_id_by_stop_id(stop_rows)
    route_by_id = {
        row["route_id"]: row for row in routes if row.get("route_id")
    }
    route_id_by_trip_id = {
        row["trip_id"]: row.get("route_id", "")
        for row in trips if row.get("trip_id")
    }
    lines_by_stop_id: dict[str, dict[str, dict[str, object]]] = {}

    for stop_time in stop_times:
        canonical_stop_id = canonical_ids.get(stop_time.get("stop_id", ""))
        if (
            included_stop_ids is not None
            and canonical_stop_id not in included_stop_ids
        ):
            continue
        route_id = route_id_by_trip_id.get(stop_time.get("trip_id", ""), "")
        route = route_by_id.get(route_id)
        if canonical_stop_id is None or route is None:
            continue

        names = line_names(route)
        if not names:
            continue

        route_type_value = route.get("route_type", "").strip()
        line: dict[str, object] = {
            "routeID": route_id,
            "agencyID": route.get("agency_id", "").strip() or None,
            "names": names
        }
        if route_type_value.isdigit():
            line["routeType"] = int(route_type_value)
        lines_by_stop_id.setdefault(canonical_stop_id, {})[route_id] = line

    return lines_by_stop_id


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

    if adapter == "vrrEFA":
        efa_path = configuration.get("efaPath")
        if (
            region is not None
            or not isinstance(efa_path, str)
            or not CITY_ID_PATTERN.fullmatch(efa_path)
        ):
            raise ValueError(f"Invalid VRR EFA configuration for {city_id}")
        return

    if adapter == "vbb":
        if not isinstance(region, dict):
            raise ValueError(f"Invalid VBB configuration for {city_id}")

    if adapter == "ivantoMQTT":
        agency = configuration.get("agency")
        if agency not in {"evag", "heag", "vku", "wvg"}:
            raise ValueError(f"Invalid Ivanto MQTT agency for {city_id}")

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
            elif adapter == "ivantoMQTT":
                agency = str(provider_configuration["agency"])
                provider_id = (
                    f"ruhrbahn-{city_id}"
                    if agency == "evag"
                    else f"ivanto-{agency}-{city_id}"
                )
            elif adapter == "ruhrbahn":
                provider_id = f"ruhrbahn-{city_id}"
            elif adapter == "shgMobil":
                provider_id = "shg-mobil-schaumburg"
            elif adapter == "stadtwerkeMuenster":
                provider_id = "stadtwerke-muenster"
            elif adapter == "swu":
                provider_id = "swu-ulm"
            elif adapter == "vbb":
                provider_id = f"vbb-{city_id}"
            elif adapter == "vrrEFA":
                provider_id = f"vrr-efa-{city_id}"
            else:
                raise ValueError(f"Unsupported transit radar adapter for {city_id}")

            is_departure_provider = adapter == "vrrEFA"
            provider = {
                "providerID": provider_id,
                "adapter": adapter,
                "isEnabled": provider_configuration.get("isEnabled", True),
                "isExperimental": True,
                "features": (
                    ["realtimeDepartures", "firstDepartures", "stopLookup", "realtimeDelay"]
                    if is_departure_provider
                    else ["liveVehicles", "realtimeDelay"]
                ),
                "statusMessage": (
                    f'Live-Abfahrten für {city["name"]}'
                    if is_departure_provider
                    else f'Live-Radar für {city["name"]}'
                )
            }
            region = provider_configuration.get("region")
            if isinstance(region, dict):
                provider["region"] = region
            agency = provider_configuration.get("agency")
            if isinstance(agency, str):
                provider["agency"] = agency
            efa_path = provider_configuration.get("efaPath")
            if isinstance(efa_path, str):
                provider["efaPath"] = efa_path
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


def build_vbb_city_network_index(
    stops: dict[str, dict[str, str]],
    stop_times: list[dict[str, str]],
    routes: dict[str, dict[str, str]],
    trips: list[dict[str, str]],
    calendar_date_rows: list[dict[str, str]],
    calendar_rows: list[dict[str, str]],
    output: Path,
    city_id: str,
    region: dict[str, object]
) -> None:
    region_stop_ids = set()
    for stop_id, row in stops.items():
        try:
            latitude, longitude = float(row["stop_lat"]), float(row["stop_lon"])
        except (KeyError, ValueError):
            continue
        if (
            float(region["minimumLatitude"]) <= latitude <= float(region["maximumLatitude"])
            and float(region["minimumLongitude"]) <= longitude <= float(region["maximumLongitude"])
        ):
            region_stop_ids.add(stop_id)

    region_trip_ids = {
        row["trip_id"] for row in stop_times
        if row.get("stop_id") in region_stop_ids
    }
    times_by_trip: dict[str, list[dict[str, str]]] = {}
    for row in stop_times:
        trip_id = row.get("trip_id", "")
        if trip_id not in region_trip_ids:
            continue
        times_by_trip.setdefault(trip_id, []).append(row)

    trip_templates = []
    for row in trips:
        trip_id = row.get("trip_id", "")
        trip_times = times_by_trip.get(trip_id)
        if not trip_times:
            continue
        route = routes.get(row.get("route_id", ""), {})
        route_type_value = route.get("route_type", "").strip()
        route_type = int(route_type_value) if route_type_value.isdigit() else None
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
            "routeType": route_type,
            "directionName": row.get("trip_headsign", ""),
            "serviceID": service_id,
            "stopTimes": compact_times
        })

    calendar_dates: dict[str, dict[str, list[str]]] = {}
    for row in calendar_date_rows:
        service_id = row.get("service_id", "")
        key = "addedDates" if row.get("exception_type") == "1" else "removedDates"
        calendar_dates.setdefault(service_id, {"addedDates": [], "removedDates": []})[key].append(row.get("date", ""))

    calendar_by_id = {
        row["service_id"]: row for row in calendar_rows if row.get("service_id")
    }

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

    transit_outputs = [output / "transit" / "vbb" / city_id]
    if city_id == "berlin":
        transit_outputs.append(output / "transit" / "vbb")
    for transit_output in transit_outputs:
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
        encoded_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        for transit_output in transit_outputs:
            (transit_output / f"{key}.json").write_text(
                encoded_payload,
                encoding="utf-8"
            )


def build_vbb_network_indexes(
    archive: zipfile.ZipFile,
    output: Path,
    cities: list[dict[str, object]]
) -> None:
    stops = {
        row["stop_id"]: row
        for row in load_table(archive, "stops.txt")
        if row.get("stop_id")
    }
    stop_times = load_table(archive, "stop_times.txt")
    routes = {
        row["route_id"]: row
        for row in load_table(archive, "routes.txt")
        if row.get("route_id")
    }
    trips = load_table(archive, "trips.txt")
    calendar_date_rows = load_table(archive, "calendar_dates.txt")
    calendar_rows = load_table(archive, "calendar.txt")

    for city in cities:
        transit_radar = city.get("transitRadar")
        if transit_radar is None:
            continue
        for configuration in transit_radar_configurations(transit_radar):
            if configuration.get("adapter") != "vbb":
                continue
            build_vbb_city_network_index(
                stops=stops,
                stop_times=stop_times,
                routes=routes,
                trips=trips,
                calendar_date_rows=calendar_date_rows,
                calendar_rows=calendar_rows,
                output=output,
                city_id=str(city["id"]),
                region=configuration["region"]
            )


def configured_municipality_codes(
    cities: list[dict[str, object]],
    municipalities: list[dict[str, object]],
    spatial_index: dict[tuple[int, int], list[int]]
) -> dict[str, str]:
    result = {}
    for city in cities:
        municipality = municipality_for_coordinate(
            float(city["latitude"]),
            float(city["longitude"]),
            municipalities,
            spatial_index
        )
        if municipality is None:
            continue

        configured_names = {
            normalized(str(name))
            for name in [city["name"], *city.get("aliases", [])]
        }
        if normalized(str(municipality["name"])) in configured_names:
            result[str(municipality["code"])] = str(city["id"])
    return result


def municipality_stop_packages(
    stops: list[dict[str, object]],
    municipalities: list[dict[str, object]],
    spatial_index: dict[tuple[int, int], list[int]]
) -> tuple[dict[str, list[dict[str, object]]], int]:
    packages: dict[str, list[dict[str, object]]] = {}
    skipped_stop_count = 0
    for stop in stops:
        municipality = municipality_for_coordinate(
            float(stop["latitude"]),
            float(stop["longitude"]),
            municipalities,
            spatial_index
        )
        if municipality is None:
            skipped_stop_count += 1
            continue
        packages.setdefault(str(municipality["code"]), []).append(stop)
    return packages, skipped_stop_count


def write_stop_package(
    packages_directory: Path,
    city_id: str,
    stops: list[dict[str, object]]
) -> str:
    filename = f"{city_id}.json"
    stops.sort(key=lambda stop: (str(stop["searchName"]), str(stop["id"])))
    (packages_directory / filename).write_text(
        json.dumps(stops, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8"
    )
    return filename


def build_stop_packages(
    stops: list[dict[str, object]],
    cities: list[dict[str, object]],
    municipalities: list[dict[str, object]],
    output: Path
) -> tuple[list[dict[str, object]], int, dict[str, list[dict[str, object]]]]:
    packages_directory = output / "stops"
    packages_directory.mkdir(parents=True, exist_ok=True)
    spatial_index = municipality_spatial_index(municipalities)
    packages_by_code, skipped_stop_count = municipality_stop_packages(
        stops,
        municipalities,
        spatial_index
    )
    configured_codes = configured_municipality_codes(
        cities,
        municipalities,
        spatial_index
    )
    configured_code_by_city_id = {
        city_id: code for code, city_id in configured_codes.items()
    }
    municipalities_by_code = {
        str(municipality["code"]): municipality for municipality in municipalities
    }
    duplicate_name_counts: dict[str, int] = {}
    for code in packages_by_code:
        municipality = municipalities_by_code[code]
        key = normalized(str(municipality["name"]))
        duplicate_name_counts[key] = duplicate_name_counts.get(key, 0) + 1

    manifest = []
    package_stops_by_city_id: dict[str, list[dict[str, object]]] = {}
    for code, municipality_stops in packages_by_code.items():
        if code in configured_codes:
            continue
        municipality = municipalities_by_code[code]
        base_name = str(municipality["name"])
        has_duplicate_name = duplicate_name_counts[normalized(base_name)] > 1
        display_name = (
            f'{base_name} ({municipality["state"]})'
            if has_duplicate_name and municipality["state"]
            else base_name
        )
        aliases = [base_name] if display_name != base_name else []
        city_id = f"{identifier_component(base_name)}-{code.lower()}"
        filename = write_stop_package(packages_directory, city_id, municipality_stops)
        package_stops_by_city_id[city_id] = municipality_stops
        manifest.append({
            "id": city_id,
            "name": display_name,
            "aliases": aliases,
            "stopCount": len(municipality_stops),
            "url": f"stops/{filename}"
        })

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
        if not city_stops:
            raise ValueError(f'No stops found for configured city {city["id"]}.')

        city_id = str(city["id"])
        filename = write_stop_package(packages_directory, city_id, city_stops)
        municipality_code = configured_code_by_city_id.get(city_id)
        package_stops_by_city_id[city_id] = (
            packages_by_code.get(municipality_code, city_stops)
            if municipality_code is not None
            else city_stops
        )
        manifest.append({
            "id": city_id,
            "name": city["name"],
            "aliases": city.get("aliases", []),
            "stopCount": len(city_stops),
            "url": f"stops/{filename}"
        })

    manifest.sort(key=lambda city: (normalized(str(city["name"])), str(city["id"])))
    return manifest, skipped_stop_count, package_stops_by_city_id


def radar_coverage_names(cities: list[dict[str, object]]) -> set[str]:
    result = set()
    for city in cities:
        if city.get("transitRadar") is None:
            continue
        result.update(
            normalized(str(name))
            for name in [city["name"], *city.get("aliases", [])]
        )
    return result


def radar_city_ids(
    manifest: list[dict[str, object]],
    cities: list[dict[str, object]]
) -> set[str]:
    coverage_names = radar_coverage_names(cities)
    result = set()
    for city in manifest:
        names = {normalized(str(city["name"]))}
        names.update(normalized(str(alias)) for alias in city.get("aliases", []))
        if not names.isdisjoint(coverage_names):
            result.add(str(city["id"]))
    return result


def build_city_line_catalogs(
    output: Path,
    manifest: list[dict[str, object]],
    package_stops_by_city_id: dict[str, list[dict[str, object]]],
    lines_by_stop_id: dict[str, dict[str, dict[str, object]]],
    cities: list[dict[str, object]]
) -> None:
    included_city_ids = radar_city_ids(manifest, cities)
    directory = output / "transit" / "city-lines"
    directory.mkdir(parents=True, exist_ok=True)

    for city in manifest:
        if str(city["id"]) not in included_city_ids:
            continue

        lines: dict[str, dict[str, object]] = {}
        for stop in package_stops_by_city_id.get(str(city["id"]), []):
            lines.update(lines_by_stop_id.get(str(stop["id"]), {}))

        sorted_lines = sorted(
            lines.values(),
            key=lambda line: (
                normalized(str(line["names"][0])),
                str(line["routeID"])
            )
        )
        payload = {
            "version": date.today().isoformat(),
            "cityID": city["id"],
            "lines": sorted_lines
        }
        (directory / f'{city["id"]}.json').write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs-url", required=True)
    parser.add_argument(
        "--vbb-gtfs-url",
        default="https://unternehmen.vbb.de/fileadmin/user_upload/VBB/Dokumente/API-Datensaetze/gtfs-mastscharf/GTFS.zip"
    )
    parser.add_argument("--cities", default="config/cities.json")
    parser.add_argument("--municipalities-url", default=BKG_MUNICIPALITIES_URL)
    parser.add_argument("--output", default="docs/data")
    args = parser.parse_args()

    cities = load_cities(Path(args.cities))
    archive = load_gtfs_archive(args.gtfs_url)
    stop_rows = load_table(archive, "stops.txt")
    stops = canonicalize(stop_rows)
    municipalities = load_municipalities(args.municipalities_url)
    output = Path(args.output)
    manifest, skipped_stop_count, package_stops_by_city_id = build_stop_packages(
        stops,
        cities,
        municipalities,
        output
    )
    included_city_ids = radar_city_ids(manifest, cities)
    included_stop_ids = {
        str(stop["id"])
        for city_id in included_city_ids
        for stop in package_stops_by_city_id.get(city_id, [])
    }
    lines_by_stop_id = build_lines_by_stop_id(
        stop_rows=stop_rows,
        stop_times=iter_table(archive, "stop_times.txt"),
        trips=load_table(archive, "trips.txt"),
        routes=load_table(archive, "routes.txt"),
        included_stop_ids=included_stop_ids
    )
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
    (output / "attributions.json").write_text(
        json.dumps(
            [{
                "name": "Bundesamt für Kartographie und Geodäsie (BKG)",
                "license": "Datenlizenz Deutschland – Namensnennung – Version 2.0",
                "url": "https://gdz.bkg.bund.de/index.php/default/open-data/wfs-verwaltungsgebiete-1-250-000-stand-01-01-wfs-vg250.html"
            }],
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )
    build_city_line_catalogs(
        output=output,
        manifest=manifest,
        package_stops_by_city_id=package_stops_by_city_id,
        lines_by_stop_id=lines_by_stop_id,
        cities=cities
    )
    build_vbb_network_indexes(load_gtfs_archive(args.vbb_gtfs_url), output, cities)
    print(
        f"Built {len(manifest)} city packages from {len(stops)} canonical stops; "
        f"skipped {skipped_stop_count} stops outside German municipalities."
    )


if __name__ == "__main__":
    main()
