"""Build compact cumulative-progress topology from static GTFS for Kyiv Radar."""

from __future__ import annotations

import hashlib
import json
import math
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

try:
    from .gtfs_csv import normalized_dict_reader
except ImportError:
    from gtfs_csv import normalized_dict_reader

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
    stops_by_trip: dict[str, list[tuple[int, str]]] = defaultdict(list)
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
        stops_by_trip[trip_id].append((sequence, stop_id))
    for trip_stops in stops_by_trip.values():
        trip_stops.sort()

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

    grouped: dict[tuple[str, str, str, str, str, tuple[str, ...]], set[str]] = defaultdict(set)
    for trip_id, trip in trips.items():
        shape_id = trip["shape_id"]
        if shape_id not in shape_payloads:
            continue
        trip_stops = stops_by_trip.get(trip_id, [])
        if not trip_stops:
            continue
        ordered_stop_ids = tuple(stop_id for _sequence, stop_id in trip_stops)
        ordered_stop_sequences = tuple(sequence for sequence, _stop_id in trip_stops)
        terminal_stop_id = ordered_stop_ids[-1]
        destination = trip["headsign"]
        if not destination:
            destination = stop_names.get(terminal_stop_id, "")
        grouped[(trip["route_id"], trip["direction_id"], shape_id, terminal_stop_id, destination, ordered_stop_ids, ordered_stop_sequences)].add(trip_id)

    for city in cities:
        city_id = str(city.get("id", "")).strip()
        if not city_id:
            continue
        city_routes: dict[str, dict[str, object]] = {}
        for (route_id, direction_id, shape_id, terminal_stop_id, destination, stop_ids, stop_sequences), trip_ids in sorted(grouped.items()):
            published_route_id = f"{namespace}{route_id}" if namespace else route_id
            direction_rows = city_routes.setdefault(
                published_route_id,
                {"directions": []},
            )["directions"]
            if not isinstance(direction_rows, list):
                continue
            signature = "|".join(f"{sequence}:{stop_id}" for sequence, stop_id in zip(stop_sequences, stop_ids))
            variant_id = hashlib.sha1(
                f"{route_id}|{direction_id}|{shape_id}|{terminal_stop_id}|{signature}".encode("utf-8")
            ).hexdigest()[:16]
            direction_rows.append({
                "variantID": variant_id,
                "routeID": published_route_id,
                "directionID": direction_id,
                "shapeID": shape_id,
                "terminalStopID": terminal_stop_id,
                "destination": destination or None,
                "tripIDs": sorted(trip_ids),
                "stopIDs": list(stop_ids),
                "stopSequences": list(stop_sequences),
                "shapes": [shape_payloads[shape_id]],
            })
        artifact = {
            "schemaVersion": 2,
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
        str(city.get("id")): len({route_id for route_id, _direction, _shape, _terminal, _destination, _stops, _sequences in grouped})
        for city in cities
        if city.get("id")
    }


def _rows(archive: zipfile.ZipFile, name: str) -> Iterable[dict[str, str]]:
    with archive.open(name) as source:
        text = (line.decode("utf-8-sig") for line in source)
        yield from normalized_dict_reader(text)


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
