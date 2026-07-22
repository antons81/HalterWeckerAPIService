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
            relevant_trips = set()
            for row in rows(archive, "stop_times.txt"):
                stop_id = row.get("stop_id")
                if stop_id not in selected or not row.get("departure_time"):
                    continue
                trip = trips.get(row["trip_id"])
                if not trip:
                    continue
                route = routes.get(trip.get("route_id"), {})
                relevant_trips.add(row["trip_id"])
                service = calendar.get(trip.get("service_id"))
                for service_date in departure_dates:
                    if service_active(service, service_date):
                        departures[stop_id].append({"tripId": row["trip_id"], "routeId": trip.get("route_id", ""), "line": route.get("route_short_name") or route.get("route_long_name") or trip.get("route_id", ""), "destination": trip.get("trip_headsign") or "", "departureTime": row["departure_time"], "serviceDate": service_date, "transportType": transport_type(route.get("route_type"))})
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


def transport_type(route_type: str | None):
    return {"0": "tram", "1": "subway", "2": "train", "3": "bus", "4": "ferry"}.get(route_type or "")


if __name__ == "__main__":
    main()
