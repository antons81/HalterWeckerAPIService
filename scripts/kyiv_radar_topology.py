"""Build compact cumulative-progress topology from static GTFS for Kyiv Radar."""

from __future__ import annotations

import csv
import json
import math
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

KYIV_SURFACE_ROUTE_TYPES = frozenset({"0", "3", "11"})
EARTH_RADIUS_METERS = 6_371_000.0


def build_radar_topology(
    archive: zipfile.ZipFile,
    cities: list[dict[str, object]],
    output: Path,
    namespace: str = "",
) -> dict[str, int]:
    """Write one topology artifact per configured city and return route counts."""
    routes = {
        str(row.get("route_id", "")).strip(): row
        for row in _rows(archive, "routes.txt")
        if str(row.get("route_id", "")).strip()
        and str(row.get("route_type", "")).strip() in KYIV_SURFACE_ROUTE_TYPES
    }
    if not routes:
        raise ValueError("Kyiv GTFS has no supported surface routes for Radar topology")

    trips: dict[str, dict[str, str]] = {}
    for row in _rows(archive, "trips.txt"):
        trip_id = str(row.get("trip_id", "")).strip()
        route_id = str(row.get("route_id", "")).strip()
        shape_id = str(row.get("shape_id", "")).strip()
        if not trip_id or route_id not in routes or not shape_id:
            continue
        trips[trip_id] = {
            "route_id": route_id,
            "direction_id": str(row.get("direction_id", "0") or "0").strip() or "0",
            "shape_id": shape_id,
            "headsign": str(row.get("trip_headsign", "") or "").strip(),
        }

    stop_names = {
        str(row.get("stop_id", "")).strip(): str(row.get("stop_name", "") or "").strip()
        for row in _rows(archive, "stops.txt")
        if str(row.get("stop_id", "")).strip()
    }
    terminal_by_trip: dict[str, tuple[int, str]] = {}
    for row in _rows(archive, "stop_times.txt"):
        trip_id = str(row.get("trip_id", "")).strip()
        if trip_id not in trips:
            continue
        stop_id = str(row.get("stop_id", "")).strip()
        if not stop_id:
            continue
        try:
            sequence = int(str(row.get("stop_sequence", "0") or "0"))
        except ValueError:
            sequence = 0
        previous = terminal_by_trip.get(trip_id)
        if previous is None or sequence >= previous[0]:
            terminal_by_trip[trip_id] = (sequence, stop_id)

    shape_groups: dict[str, list[tuple[float, float, int, float | None]]] = defaultdict(list)
    for row in _rows(archive, "shapes.txt"):
        shape_id = str(row.get("shape_id", "")).strip()
        if not shape_id:
            continue
        try:
            latitude = float(row.get("shape_pt_lat", ""))
            longitude = float(row.get("shape_pt_lon", ""))
            sequence = int(str(row.get("shape_pt_sequence", "0") or "0"))
        except (TypeError, ValueError):
            continue
        raw_distance = str(row.get("shape_dist_traveled", "") or "").strip()
        try:
            shape_distance = float(raw_distance) if raw_distance else None
        except ValueError:
            shape_distance = None
        shape_groups[shape_id].append((latitude, longitude, sequence, shape_distance))

    shape_payloads: dict[str, dict[str, object]] = {}
    for shape_id, rows in shape_groups.items():
        rows.sort(key=lambda row: row[2])
        points: list[list[float]] = []
        cumulative = 0.0
        previous_latitude: float | None = None
        previous_longitude: float | None = None
        for latitude, longitude, _sequence, _distance in rows:
            if previous_latitude is not None and previous_longitude is not None:
                cumulative += _distance_meters(
                    previous_latitude,
                    previous_longitude,
                    latitude,
                    longitude,
                )
            points.append([latitude, longitude, round(cumulative, 3)])
            previous_latitude = latitude
            previous_longitude = longitude
        if len(points) < 2:
            continue
        shape_payloads[shape_id] = {
            "shapeID": shape_id,
            "lengthMeters": round(cumulative, 3),
            "points": points,
        }

    grouped: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for trip_id, trip in trips.items():
        shape_id = trip["shape_id"]
        if shape_id not in shape_payloads:
            continue
        destination = trip["headsign"]
        if not destination:
            terminal = terminal_by_trip.get(trip_id)
            destination = stop_names.get(terminal[1], "") if terminal else ""
        grouped[(trip["route_id"], trip["direction_id"], destination)].add(shape_id)

    for city in cities:
        city_id = str(city.get("id", "")).strip()
        if not city_id:
            continue
        city_routes: dict[str, dict[str, object]] = {}
        for (route_id, direction_id, destination), shape_ids in sorted(grouped.items()):
            published_route_id = f"{namespace}{route_id}" if namespace else route_id
            direction_rows = city_routes.setdefault(
                published_route_id,
                {"directions": []},
            )["directions"]
            if not isinstance(direction_rows, list):
                continue
            direction_rows.append({
                "directionID": direction_id,
                "destination": destination or None,
                "shapes": [shape_payloads[shape_id] for shape_id in sorted(shape_ids)],
            })
        artifact = {
            "schemaVersion": 1,
            "cityID": city_id,
            "routes": dict(sorted(city_routes.items())),
        }
        path = output / "radar" / f"{city_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(artifact, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    return {
        str(city.get("id")): len({route_id for route_id, _direction, _destination in grouped})
        for city in cities
        if city.get("id")
    }


def _rows(archive: zipfile.ZipFile, name: str) -> Iterable[dict[str, str]]:
    with archive.open(name) as source:
        text = (line.decode("utf-8-sig") for line in source)
        yield from csv.DictReader(text)


def _distance_meters(
    first_latitude: float,
    first_longitude: float,
    second_latitude: float,
    second_longitude: float,
) -> float:
    first_latitude_radians = math.radians(first_latitude)
    second_latitude_radians = math.radians(second_latitude)
    delta_latitude = second_latitude_radians - first_latitude_radians
    delta_longitude = math.radians(second_longitude - first_longitude)
    mean_latitude = (first_latitude_radians + second_latitude_radians) / 2.0
    x = delta_longitude * math.cos(mean_latitude)
    y = delta_latitude
    return EARTH_RADIUS_METERS * math.hypot(x, y)
