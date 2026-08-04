#!/usr/bin/env python3
"""Build per-stop Swiss GTFS Static indexes consumed by the Worker."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from build_stop_packages import load_gtfs_archive


def rows(archive: zipfile.ZipFile, name: str):
    with archive.open(name) as raw:
        yield from csv.DictReader((line.decode("utf-8-sig") for line in raw))


def distance_meters(a_lat, a_lon, b_lat, b_lon):
    radius = 6_371_000
    d_lat = math.radians(b_lat - a_lat)
    d_lon = math.radians(b_lon - a_lon)
    value = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(d_lon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


def filename(stop_id: str) -> str:
    return hashlib.sha256(stop_id.encode("utf-8")).hexdigest() + ".json"


def service_active(service: dict | None, date: str) -> bool:
    if not service:
        return False
    if date in service["exceptions"]:
        return service["exceptions"][date] == 1
    if not service["startDate"] <= date <= service["endDate"]:
        return False
    return bool(service["weekdays"][datetime.strptime(date, "%Y%m%d").weekday()])


def stop_time_targets(all_stops: dict[str, dict[str, str]], selected: dict[str, dict[str, str]]):
    """Map each source stop_id to the selected Swiss output stops that consume it."""
    members_by_parent: dict[str, list[str]] = defaultdict(list)
    for stop_id, stop in all_stops.items():
        parent_station = (stop.get("parent_station") or "").strip()
        if parent_station:
            members_by_parent[parent_station].append(stop_id)

    targets: dict[str, list[str]] = defaultdict(list)
    for output_stop_id, stop in selected.items():
        parent_station = (stop.get("parent_station") or "").strip()
        platform_code = (stop.get("platform_code") or "").strip()
        source_stop_ids = (
            members_by_parent.get(parent_station, [output_stop_id])
            if parent_station and not platform_code
            else [output_stop_id]
        )
        for source_stop_id in source_stop_ids:
            targets[source_stop_id].append(output_stop_id)
    return targets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs-url", required=True)
    parser.add_argument("--cities", default="config/swiss-cities.json")
    parser.add_argument("--output", default="docs/data/swiss-static")
    args = parser.parse_args()

    output = Path(args.output)
    shutil.rmtree(output, ignore_errors=True)
    (output / "stops").mkdir(parents=True)
    cities = json.loads(Path(args.cities).read_text())
    with load_gtfs_archive(args.gtfs_url) as archive:
            feed_info = next(rows(archive, "feed_info.txt"), {}) if "feed_info.txt" in archive.namelist() else {}
            all_stops = {row["stop_id"]: row for row in rows(archive, "stops.txt") if row.get("stop_id")}
            selected = {}
            for stop_id, stop in all_stops.items():
                try:
                    latitude, longitude = float(stop["stop_lat"]), float(stop["stop_lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                if any(distance_meters(latitude, longitude, city["latitude"], city["longitude"]) <= city["radiusMeters"] for city in cities):
                    selected[stop_id] = stop
            routes = {row["route_id"]: row for row in rows(archive, "routes.txt")}
            trips = {row["trip_id"]: row for row in rows(archive, "trips.txt")}
            calendar = {row["service_id"]: {"startDate": row["start_date"], "endDate": row["end_date"], "weekdays": [int(row[day]) for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")], "exceptions": {}} for row in rows(archive, "calendar.txt")}
            for row in rows(archive, "calendar_dates.txt"):
                calendar.setdefault(row["service_id"], {"startDate": "00000000", "endDate": "99999999", "weekdays": [0] * 7, "exceptions": {}})["exceptions"][row["date"]] = int(row["exception_type"])
            departure_dates = [
                (datetime.now(ZoneInfo("Europe/Zurich")).date() + timedelta(days=offset)).strftime("%Y%m%d")
                for offset in (-1, 0, 1)
            ]
            departures = defaultdict(list)
            targets = stop_time_targets(all_stops, selected)
            relevant_trips = set()
            for row in rows(archive, "stop_times.txt"):
                stop_id = row.get("stop_id")
                if stop_id not in targets or not row.get("departure_time"):
                    continue
                trip = trips.get(row["trip_id"])
                if not trip:
                    continue
                route = routes.get(trip.get("route_id"), {})
                relevant_trips.add(row["trip_id"])
                service = calendar.get(trip.get("service_id"))
                for service_date in departure_dates:
                    if service_active(service, service_date):
                        item = {"tripId": row["trip_id"], "routeId": trip.get("route_id", ""), "line": route.get("route_short_name") or route.get("route_long_name") or trip.get("route_id", ""), "destination": trip.get("trip_headsign") or "", "departureTime": row["departure_time"], "serviceDate": service_date, "transportType": transport_type(route.get("route_type"))}
                        for output_stop_id in targets[stop_id]:
                            departures[output_stop_id].append(item.copy())
            terminals = {}
            for row in rows(archive, "stop_times.txt"):
                if row.get("trip_id") in relevant_trips:
                    terminals[row["trip_id"]] = row.get("stop_id", "")
            generated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            manifest = {"generatedAt": generated, "stopCount": len(selected), "stops": {}}
            for stop_id, stop in selected.items():
                items = departures.get(stop_id, [])
                for item in items:
                    if not item["destination"]:
                        item["destination"] = all_stops.get(terminals.get(item["tripId"]), {}).get("stop_name") or "Unbekanntes Ziel"
                payload = {"staticFeed": {"version": feed_info.get("feed_version") or feed_info.get("feed_start_date"), "fetchedAt": generated}, "timezone": "Europe/Zurich", "stop": {"id": stop_id, "name": stop.get("stop_name", ""), "platform": stop.get("platform_code") or None}, "departures": items}
                name = filename(stop_id)
                (output / "stops" / name).write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                manifest["stops"][stop_id] = name
            (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))

            # Also generate city-level compact index for iOS client (like Netherlands pattern)
            city_output = output.parent / "departures"
            city_output.mkdir(parents=True, exist_ok=True)
            for city in cities:
                city_id = city["id"]
                city_lat = city["latitude"]
                city_lon = city["longitude"]
                city_radius = city["radiusMeters"]
                city_stops: dict[str, list[dict]] = {}
                for stop_id, stop in selected.items():
                    try:
                        lat, lon = float(stop["stop_lat"]), float(stop["stop_lon"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if distance_meters(lat, lon, city_lat, city_lon) <= city_radius:
                        items = departures.get(stop_id, [])
                        if items:
                            stop_key = stop_id.split(":")[-1] if ":" in stop_id else stop_id
                            city_stops[stop_key] = [
                                {"t": d["tripId"], "r": d["routeId"], "h": d["destination"] or d["line"],
                                 "d": trips.get(d["tripId"], {}).get("direction_id", "0"),
                                 "p": d["departureTime"]}
                                for d in items
                            ]
                payload = {"generatedAt": generated, "stops": city_stops}
                (city_output / f"{city_id}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                )


def transport_type(route_type: str | None):
    try:
        value = int(route_type or "")
    except ValueError:
        return None

    if value == 0 or 900 <= value < 1_000:
        return "tram"
    if value == 1 or 400 <= value < 500:
        return "subway"
    if value == 2 or 100 <= value < 200:
        return "train"
    if value == 3 or 700 <= value < 800:
        return "bus"
    if value == 4 or 1_000 <= value < 1_100:
        return "ferry"
    return None


if __name__ == "__main__":
    main()
