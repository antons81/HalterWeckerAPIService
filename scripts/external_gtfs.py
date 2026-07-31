#!/usr/bin/env python3
"""Generic external GTFS source registry and builders (Sweden-first)."""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from build_stop_packages import (
    build_lines_by_stop_id_noncanonical,
    distance_meters,
    iter_table,
    load_cities,
    load_table,
    normalized,
    write_stop_package,
)


# Auth is resolved at runtime from environment variables. Secrets never appear
# in config JSON. Future sources can register here without touching GTFS parsers.
EXTERNAL_SOURCE_AUTH: dict[str, dict[str, object]] = {
    "sweden": {
        "api_key_env": "SAMTRAFIKEN_STATIC_API_KEY",
        "query_parameter": "key",
        "headers": {
            "Accept-Encoding": "gzip",
            "User-Agent": "HalteWeckerStopPipeline/1.0",
        },
    },
}


def load_external_gtfs_sources(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("External GTFS sources must be a JSON array.")
    sources: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Each external GTFS source must be an object.")
        sources.append(item)
    return sources


def validate_external_gtfs_source(
    source: dict[str, object],
    repository_root: Path,
    *,
    known_source_ids: set[str] | None = None,
    known_prefixes: set[str] | None = None,
) -> None:
    source_id = source.get("id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("External GTFS source id must be a non-empty string.")
    if known_source_ids is not None and source_id in known_source_ids:
        raise ValueError(f"Duplicate external GTFS source id: {source_id}")

    cities_path = source.get("cities")
    if not isinstance(cities_path, str) or not cities_path.strip():
        raise ValueError(f"External GTFS source {source_id} is missing cities path.")
    resolved_cities = (repository_root / cities_path).resolve()
    if not resolved_cities.is_file():
        raise ValueError(
            f"External GTFS source {source_id} city file does not exist: {cities_path}"
        )

    timezone_name = source.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError(f"External GTFS source {source_id} is missing timezone.")
    try:
        ZoneInfo(timezone_name)
    except Exception as error:
        raise ValueError(
            f"External GTFS source {source_id} has invalid timezone {timezone_name!r}."
        ) from error

    prefix = source.get("identifierPrefix")
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError(
            f"External GTFS source {source_id} requires a non-empty identifierPrefix."
        )
    if known_prefixes is not None and prefix in known_prefixes:
        raise ValueError(f"Duplicate external GTFS identifierPrefix: {prefix}")

    stop_id_mode = source.get("stopIDMode", "exact")
    if stop_id_mode != "exact":
        raise ValueError(
            f"External GTFS source {source_id} stopIDMode {stop_id_mode!r} "
            "is not supported (only 'exact')."
        )

    for flag in ("buildStops", "buildRoutes", "buildDepartures"):
        value = source.get(flag, True)
        if not isinstance(value, bool):
            raise ValueError(f"External GTFS source {source_id} has invalid {flag}.")


def load_external_cities(
    source: dict[str, object],
    repository_root: Path,
) -> list[dict[str, object]]:
    cities_rel = str(source["cities"])
    cities = load_cities(repository_root / cities_rel)
    source_id = str(source["id"])
    if not cities:
        raise ValueError(f"External GTFS source {source_id} has no cities.")
    for city in cities:
        city_id = str(city["id"])
        package_mode = city.get("packageMode", "german")
        if package_mode != "external":
            raise ValueError(
                f"External city {city_id} must set packageMode to 'external'."
            )
        provider = city.get("externalGTFSProvider")
        if provider is not None and provider != source_id:
            raise ValueError(
                f"External city {city_id} externalGTFSProvider {provider!r} "
                f"does not match source {source_id!r}."
            )
        city["externalGTFSProvider"] = source_id
    return cities


def external_city_ids(
    sources: list[dict[str, object]],
    repository_root: Path,
) -> set[str]:
    ids: set[str] = set()
    for source in sources:
        for city in load_external_cities(source, repository_root):
            ids.add(str(city["id"]))
    return ids


def parse_external_gtfs_url_args(values: list[str] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(
                f"Invalid --external-gtfs-url value {value!r}; expected providerID=URL."
            )
        provider_id, url = value.split("=", 1)
        provider_id = provider_id.strip()
        url = url.strip()
        if not provider_id or not url:
            raise ValueError(
                f"Invalid --external-gtfs-url value {value!r}; expected providerID=URL."
            )
        if provider_id in mapping:
            raise ValueError(f"Duplicate --external-gtfs-url provider id: {provider_id}")
        mapping[provider_id] = url
    return mapping


def authenticated_external_request(
    source_id: str,
    url: str,
    environ: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Return (url, headers) for an external source without logging secrets."""
    env = environ if environ is not None else os.environ
    auth = EXTERNAL_SOURCE_AUTH.get(source_id, {})
    headers = {
        str(key): str(value)
        for key, value in dict(auth.get("headers") or {}).items()
    }
    if "User-Agent" not in headers:
        headers["User-Agent"] = "HalteWeckerStopPipeline/1.0"

    parts = urlsplit(url)
    is_remote = parts.scheme in {"http", "https"}
    api_key_env = auth.get("api_key_env")
    if is_remote and isinstance(api_key_env, str) and api_key_env:
        api_key = env.get(api_key_env, "").strip()
        if not api_key:
            raise ValueError(
                f"Missing required environment variable {api_key_env} "
                f"for external GTFS source {source_id}."
            )
        query_parameter = str(auth.get("query_parameter") or "key")
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query[query_parameter] = api_key
        url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
    return url, headers


def _parse_location_type(value: object) -> int:
    """GTFS location_type with safe fallback: empty/unknown values mean 0."""
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        parsed = int(raw)
    except ValueError:
        return 0
    if parsed not in (0, 1, 2, 3, 4):
        return 0
    return parsed


def _feed_stop_rows(
    archive: zipfile.ZipFile,
) -> dict[str, dict[str, str]]:
    return {
        str(row["stop_id"]): row
        for row in iter_table(archive, "stops.txt")
        if row.get("stop_id")
    }


def _stop_resolution_map(
    feed_stops: dict[str, dict[str, str]],
    public_stop_ids: set[str],
) -> dict[str, str | None]:
    """Map every feed stop ID to the public package stop that represents it.

    A public stop represents itself. A child platform rolls up to its nearest
    ancestor (via parent_station) that is present in the public package. Stops
    that do not resolve to any public package stop map to None.
    """
    parent_of = {
        stop_id: str(row.get("parent_station", "") or "").strip()
        for stop_id, row in feed_stops.items()
    }
    memo: dict[str, str | None] = {}

    def resolve(stop_id: str) -> str | None:
        if stop_id in memo:
            return memo[stop_id]
        seen: set[str] = set()
        chain: list[str] = []
        current = stop_id
        result: str | None = None
        while True:
            if current in public_stop_ids:
                result = current
                break
            if current in seen:
                break
            seen.add(current)
            chain.append(current)
            parent = parent_of.get(current, "")
            if not parent:
                break
            current = parent
        for node in chain:
            memo[node] = result
        return result

    return resolve


def build_external_stop_packages(
    archive: zipfile.ZipFile,
    cities: list[dict[str, object]],
    output: Path,
    stop_id_mode: str = "exact",
) -> tuple[list[dict[str, object]], dict[str, list[dict[str, object]]]]:
    if stop_id_mode != "exact":
        raise ValueError(f"Unsupported stopIDMode: {stop_id_mode!r}")
    if not cities:
        return [], {}

    raw_by_city_id: dict[str, list[dict[str, object]]] = {
        str(city["id"]): [] for city in cities
    }
    for row in iter_table(archive, "stops.txt"):
        stop_id = str(row.get("stop_id", "")).strip()
        name = str(row.get("stop_name", "") or "").strip()
        if not stop_id or not name:
            continue
        try:
            latitude = float(row["stop_lat"])
            longitude = float(row["stop_lon"])
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            continue
        record: dict[str, object] = {
            "id": stop_id,
            "name": name,
            "latitude": latitude,
            "longitude": longitude,
            "searchName": normalized(name),
            "location_type": _parse_location_type(row.get("location_type")),
            "parent_station": str(row.get("parent_station", "") or "").strip(),
            "platform_code": str(row.get("platform_code", "") or "").strip(),
        }
        for city in cities:
            if distance_meters(
                latitude,
                longitude,
                float(city["latitude"]),
                float(city["longitude"]),
            ) <= float(city["radiusMeters"]):
                raw_by_city_id[str(city["id"])].append(record)

    packages_directory = output / "stops"
    packages_directory.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    package_stops: dict[str, list[dict[str, object]]] = {}
    for city in cities:
        city_id = str(city["id"])
        raw_stops = raw_by_city_id[city_id]
        if not raw_stops:
            raise ValueError(f"No stops found for configured external city {city_id}.")
        parent_ids = {
            str(stop["id"])
            for stop in raw_stops
            if stop["location_type"] == 1
        }
        public_stops: list[dict[str, object]] = []
        for stop in raw_stops:
            location_type = int(stop["location_type"])
            parent_station = str(stop["parent_station"])
            if location_type in (2, 3, 4):
                # entrances, generic nodes, and boarding areas are not public stops
                continue
            if location_type == 1:
                # parent stations are always public stops
                public_stops.append(stop)
                continue
            # location_type == 0: keep only orphans whose parent is unavailable
            if parent_station and parent_station in parent_ids:
                continue
            public_stops.append(stop)
        if not public_stops:
            raise ValueError(
                f"No public stops found for configured external city {city_id}."
            )
        public_entries = [
            {
                "id": stop["id"],
                "name": stop["name"],
                "latitude": stop["latitude"],
                "longitude": stop["longitude"],
                "searchName": stop["searchName"],
            }
            for stop in public_stops
        ]
        filename = write_stop_package(packages_directory, city_id, public_entries)
        package_stops[city_id] = public_entries
        manifest.append({
            "id": city["id"],
            "name": city["name"],
            "aliases": city.get("aliases", []),
            "stopCount": len(public_entries),
            "url": f"stops/{filename}",
        })
    return manifest, package_stops


def build_external_route_index(
    archive: zipfile.ZipFile,
    cities: list[dict[str, object]],
    output: Path,
) -> None:
    if not cities:
        return

    routes = {
        str(row["route_id"]): row
        for row in load_table(archive, "routes.txt")
        if row.get("route_id")
    }
    if not routes:
        return

    trip_routes: dict[str, str] = {}
    trip_headsigns: dict[str, tuple[str, str]] = {}
    for trip in iter_table(archive, "trips.txt"):
        route_id = str(trip.get("route_id", ""))
        trip_id = str(trip.get("trip_id", ""))
        if route_id in routes and trip_id:
            trip_routes[trip_id] = route_id
            headsign = str(trip.get("trip_headsign", "") or "")
            direction_id = str(trip.get("direction_id", "0"))
            if headsign:
                trip_headsigns[trip_id] = (direction_id, headsign)

    stop_route_ids: dict[str, set[str]] = {}
    feed_stops = _feed_stop_rows(archive)
    packages_directory = output / "stops"
    routes_directory = output / "routes"
    routes_directory.mkdir(parents=True, exist_ok=True)

    public_stop_ids: set[str] = set()
    city_stop_ids: dict[str, set[str]] = {}
    for city in cities:
        city_id = str(city["id"])
        stop_path = packages_directory / f"{city_id}.json"
        if not stop_path.exists():
            city_stop_ids[city_id] = set()
            continue
        ids = {str(stop["id"]) for stop in json.loads(stop_path.read_text(encoding="utf-8"))}
        city_stop_ids[city_id] = ids
        public_stop_ids.update(ids)

    resolve = _stop_resolution_map(feed_stops, public_stop_ids)
    for stop_time in iter_table(archive, "stop_times.txt"):
        stop_id = str(stop_time.get("stop_id", ""))
        trip_id = str(stop_time.get("trip_id", ""))
        route_id = trip_routes.get(trip_id)
        if stop_id and route_id:
            public_stop_id = resolve(stop_id)
            if public_stop_id is not None:
                stop_route_ids.setdefault(public_stop_id, set()).add(route_id)

    for city in cities:
        city_id = str(city["id"])
        city_routes: dict[str, dict[str, object]] = {}
        for stop_id in city_stop_ids[city_id]:
            for route_id in stop_route_ids.get(stop_id, set()):
                if route_id not in city_routes:
                    route = routes[route_id]
                    city_routes[route_id] = {
                        "short_name": str(route.get("route_short_name", "")),
                        "long_name": str(route.get("route_long_name", "")),
                        "type": str(route.get("route_type", "3")),
                        "agency": str(route.get("agency_id", "")),
                        "headsigns": {},
                    }
        for trip_id, (direction_id, headsign) in trip_headsigns.items():
            route_id = trip_routes.get(trip_id)
            if route_id and route_id in city_routes:
                headsigns = city_routes[route_id].setdefault("headsigns", {})
                if isinstance(headsigns, dict) and direction_id not in headsigns:
                    headsigns[direction_id] = headsign
        ordered = {
            route_id: city_routes[route_id]
            for route_id in sorted(city_routes)
        }
        (routes_directory / f"{city_id}.json").write_text(
            json.dumps(ordered, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )


def _service_calendar(archive: zipfile.ZipFile) -> dict[str, dict[str, object]]:
    calendar: dict[str, dict[str, object]] = {}
    names = set(archive.namelist())
    if "calendar.txt" in names:
        for row in iter_table(archive, "calendar.txt"):
            service_id = row.get("service_id", "").strip()
            if not service_id:
                continue
            calendar[service_id] = {
                "startDate": row.get("start_date", "00000000"),
                "endDate": row.get("end_date", "99999999"),
                "weekdays": [
                    int(row.get(day, "0") or "0")
                    for day in (
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                        "sunday",
                    )
                ],
                "exceptions": {},
            }
    if "calendar_dates.txt" in names:
        for row in iter_table(archive, "calendar_dates.txt"):
            service_id = row.get("service_id", "").strip()
            service_date = row.get("date", "").strip()
            if not service_id or not service_date:
                continue
            entry = calendar.setdefault(
                service_id,
                {
                    "startDate": "00000000",
                    "endDate": "99999999",
                    "weekdays": [0] * 7,
                    "exceptions": {},
                },
            )
            try:
                exception_type = int(row.get("exception_type", "0") or "0")
            except ValueError:
                continue
            exceptions = entry["exceptions"]
            if isinstance(exceptions, dict):
                exceptions[service_date] = exception_type
    return calendar


def _service_active(service: dict[str, object] | None, service_date: str) -> bool:
    if not service:
        return False
    exceptions = service.get("exceptions")
    if isinstance(exceptions, dict) and service_date in exceptions:
        return exceptions[service_date] == 1
    start_date = str(service.get("startDate", "00000000"))
    end_date = str(service.get("endDate", "99999999"))
    if not start_date <= service_date <= end_date:
        return False
    weekdays = service.get("weekdays")
    if not isinstance(weekdays, list) or len(weekdays) != 7:
        return False
    weekday = datetime.strptime(service_date, "%Y%m%d").weekday()
    return bool(weekdays[weekday])


def _line_label(route: dict[str, str], route_id: str) -> str:
    return (
        route.get("route_short_name", "").strip()
        or route.get("route_long_name", "").strip()
        or route_id
    )


def build_external_departure_index(
    archive: zipfile.ZipFile,
    cities: list[dict[str, object]],
    output: Path,
    timezone_name: str,
) -> None:
    if not cities:
        return

    zone = ZoneInfo(timezone_name)
    today = datetime.now(zone).date()
    service_dates = [
        (today + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in (-1, 0, 1)
    ]
    calendar = _service_calendar(archive)
    active_by_service: dict[str, list[str]] = {}
    for service_id, service in calendar.items():
        active_dates = [
            service_date
            for service_date in service_dates
            if _service_active(service, service_date)
        ]
        if active_dates:
            active_by_service[service_id] = active_dates

    routes = {
        str(row["route_id"]): row
        for row in load_table(archive, "routes.txt")
        if row.get("route_id")
    }
    stop_names = {
        str(row["stop_id"]): row.get("stop_name", "").strip()
        for row in load_table(archive, "stops.txt")
        if row.get("stop_id")
    }

    trip_meta: dict[str, dict[str, str]] = {}
    for trip in iter_table(archive, "trips.txt"):
        trip_id = str(trip.get("trip_id", "")).strip()
        service_id = str(trip.get("service_id", "")).strip()
        if not trip_id or service_id not in active_by_service:
            continue
        route_id = str(trip.get("route_id", "")).strip()
        trip_meta[trip_id] = {
            "route_id": route_id,
            "headsign": str(trip.get("trip_headsign", "") or "").strip(),
            "direction_id": str(trip.get("direction_id", "0") or "0"),
            "service_id": service_id,
        }

    packages_directory = output / "stops"
    city_stop_ids: dict[str, set[str]] = {}
    public_stop_ids: set[str] = set()
    for city in cities:
        city_id = str(city["id"])
        stop_path = packages_directory / f"{city_id}.json"
        if not stop_path.exists():
            city_stop_ids[city_id] = set()
            continue
        stops = json.loads(stop_path.read_text(encoding="utf-8"))
        ids = {str(stop["id"]) for stop in stops}
        city_stop_ids[city_id] = ids
        public_stop_ids.update(ids)

    feed_stops = _feed_stop_rows(archive)
    resolve = _stop_resolution_map(feed_stops, public_stop_ids)

    # Stream stop_times once: collect departures for package stops and terminal stops.
    stop_departures: dict[str, list[tuple[str, str, str, str]]] = {}
    terminal_by_trip: dict[str, tuple[int, str]] = {}
    for stop_time in iter_table(archive, "stop_times.txt"):
        trip_id = str(stop_time.get("trip_id", "")).strip()
        if trip_id not in trip_meta:
            continue
        stop_id = str(stop_time.get("stop_id", "")).strip()
        departure_time = stop_time.get("departure_time", "").strip()
        if stop_id and departure_time:
            public_stop_id = resolve(stop_id)
            if public_stop_id is not None:
                platform_code = ""
                if public_stop_id != stop_id:
                    platform_code = str(
                        feed_stops.get(stop_id, {}).get("platform_code", "") or ""
                    ).strip()
                stop_departures.setdefault(public_stop_id, []).append(
                    (departure_time, trip_id, stop_id, platform_code)
                )
        try:
            sequence = int(stop_time.get("stop_sequence", "0") or "0")
        except ValueError:
            sequence = 0
        if stop_id:
            previous = terminal_by_trip.get(trip_id)
            if previous is None or sequence >= previous[0]:
                terminal_by_trip[trip_id] = (sequence, stop_id)

    # Realtime mapping: parent public stop -> child platform stop IDs.
    platforms_by_parent: dict[str, set[str]] = {}
    for stop_row in feed_stops.values():
        if _parse_location_type(stop_row.get("location_type")) != 0:
            continue
        child_id = str(stop_row["stop_id"])
        parent_id = str(stop_row.get("parent_station", "") or "").strip()
        if not parent_id:
            continue
        target = resolve(child_id)
        if target is not None and target != child_id:
            platforms_by_parent.setdefault(target, set()).add(child_id)

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    departures_directory = output / "departures"
    departures_directory.mkdir(parents=True, exist_ok=True)

    for city in cities:
        city_id = str(city["id"])
        departures_by_stop: dict[str, list[dict[str, str]]] = {}
        for stop_id in sorted(city_stop_ids.get(city_id, set())):
            items: list[dict[str, str]] = []
            seen: set[tuple[str, str, str, str, str, str, str]] = set()
            for departure_time, trip_id, orig_stop_id, platform_code in sorted(
                stop_departures.get(stop_id, []),
                key=lambda value: (value[0], value[1]),
            ):
                meta = trip_meta[trip_id]
                route_id = meta["route_id"]
                route = routes.get(route_id, {})
                destination = meta["headsign"]
                if not destination:
                    terminal_stop_id = terminal_by_trip.get(trip_id, (0, ""))[1]
                    destination = stop_names.get(terminal_stop_id, "") or _line_label(
                        route, route_id
                    )
                direction_id = meta["direction_id"]
                key = (
                    trip_id,
                    route_id,
                    destination,
                    direction_id,
                    departure_time,
                    orig_stop_id,
                )
                if key in seen:
                    continue
                # Emit one compact row per active service day that shares this trip.
                for _service_date in active_by_service.get(meta["service_id"], []):
                    day_key = key + (_service_date,)
                    if day_key in seen:
                        continue
                    seen.add(day_key)
                    item: dict[str, str] = {
                        "t": trip_id,
                        "r": route_id,
                        "h": destination,
                        "d": direction_id,
                        "p": departure_time,
                    }
                    if orig_stop_id != stop_id:
                        item["s"] = orig_stop_id
                        if platform_code:
                            item["platform"] = platform_code
                    items.append(item)
                    break  # one compact row per trip/time is enough for the client index
            if items:
                items.sort(key=lambda item: (item["p"], item["t"], item["r"]))
                departures_by_stop[stop_id] = items

        platforms = {
            parent: sorted(child_ids)
            for parent, child_ids in platforms_by_parent.items()
            if parent in city_stop_ids.get(city_id, set())
        }
        payload = {
            "generatedAt": generated_at,
            "timezone": timezone_name,
            "stops": {
                stop_id: departures_by_stop[stop_id]
                for stop_id in sorted(departures_by_stop)
            },
            "platforms": platforms,
        }
        (departures_directory / f"{city_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )


def build_external_lines(
    archive: zipfile.ZipFile,
    package_stops_by_city_id: dict[str, list[dict[str, object]]],
) -> dict[str, dict[str, dict[str, object]]]:
    included_stop_ids = {
        str(stop["id"])
        for stops in package_stops_by_city_id.values()
        for stop in stops
    }
    if not included_stop_ids:
        return {}
    stop_rows = list(iter_table(archive, "stops.txt"))
    feed_stops = _feed_stop_rows(archive)
    resolve = _stop_resolution_map(feed_stops, included_stop_ids)

    def resolved_stop_times():
        for stop_time in iter_table(archive, "stop_times.txt"):
            public_stop_id = resolve(str(stop_time.get("stop_id", "")))
            if public_stop_id is None:
                continue
            yield {**stop_time, "stop_id": public_stop_id}

    return build_lines_by_stop_id_noncanonical(
        stop_rows=stop_rows,
        stop_times=resolved_stop_times(),
        trips=load_table(archive, "trips.txt"),
        routes=load_table(archive, "routes.txt"),
        included_stop_ids=included_stop_ids,
    )


def process_external_gtfs_sources(
    *,
    repository_root: Path,
    sources_path: Path,
    url_by_provider: dict[str, str],
    output: Path,
    load_gtfs_archive,
    environ: dict[str, str] | None = None,
    occupied_city_ids: set[str] | None = None,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, list[dict[str, object]]],
    dict[str, dict[str, dict[str, object]]],
]:
    """
    Validate and build all configured external GTFS sources.

    Returns:
      manifest_entries, external_cities, package_stops_by_city_id, lines_by_stop_id
    """
    sources = load_external_gtfs_sources(sources_path)
    if not sources:
        return [], [], {}, {}

    known_source_ids: set[str] = set()
    known_prefixes: set[str] = set()
    occupied = set(occupied_city_ids or ())
    manifest_entries: list[dict[str, object]] = []
    external_cities: list[dict[str, object]] = []
    package_stops_by_city_id: dict[str, list[dict[str, object]]] = {}
    lines_by_stop_id: dict[str, dict[str, dict[str, object]]] = {}

    for source in sources:
        validate_external_gtfs_source(
            source,
            repository_root,
            known_source_ids=known_source_ids,
            known_prefixes=known_prefixes,
        )
        source_id = str(source["id"])
        known_source_ids.add(source_id)
        known_prefixes.add(str(source["identifierPrefix"]))

        url = url_by_provider.get(source_id, "").strip()
        if not url:
            # Source is configured but not activated for this run. Production
            # pipeline must pass --external-gtfs-url for each active provider.
            print(
                f"[ExternalGTFS] skipping source {source_id}: "
                f"no --external-gtfs-url {source_id}=URL provided"
            )
            continue

        cities = load_external_cities(source, repository_root)
        for city in cities:
            city_id = str(city["id"])
            if city_id in occupied:
                raise ValueError(
                    f"Duplicate city id across feeds: {city_id} "
                    f"(external source {source_id})."
                )
            occupied.add(city_id)

        request_url, headers = authenticated_external_request(
            source_id, url, environ=environ
        )
        archive = load_gtfs_archive(request_url, headers=headers)
        try:
            package_stops: dict[str, list[dict[str, object]]] = {}
            if source.get("buildStops", True):
                entries, package_stops = build_external_stop_packages(
                    archive,
                    cities,
                    output,
                    stop_id_mode=str(source.get("stopIDMode", "exact")),
                )
                for entry in entries:
                    entry_with_source = dict(entry)
                    entry_with_source["_source"] = (
                        f"External GTFS source {source_id} "
                        f"({source.get('cities')})"
                    )
                    manifest_entries.append(entry_with_source)
                package_stops_by_city_id.update(package_stops)

            if source.get("buildRoutes", True):
                build_external_route_index(archive, cities, output)

            if source.get("buildDepartures", True):
                build_external_departure_index(
                    archive,
                    cities,
                    output,
                    timezone_name=str(source["timezone"]),
                )

            if package_stops:
                lines_by_stop_id.update(
                    build_external_lines(archive, package_stops)
                )
        finally:
            archive.close()

        external_cities.extend(cities)

    return manifest_entries, external_cities, package_stops_by_city_id, lines_by_stop_id
