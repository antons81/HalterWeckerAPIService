"""Read-only consumer gates and exact-container recovery for release activation."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


ROUTE_PROBE = '''
import json
from urllib.parse import quote, urlencode
from urllib.request import urlopen
root = "http://127.0.0.1:8091/routerecall/v1"
for city, line in (("berlin", "100"), ("wuppertal", "635")):
    with urlopen(root + "/lines/search?" + urlencode(dict(city=city, line=line)), timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("Germany search did not return HTTP 200")
        payload = json.load(response)
    selected = next(item for item in payload["lines"]
                    if item.get("providerID") == "germany" and item.get("patterns"))
    pattern = selected["patterns"][0]["id"]
    path = "/lines/" + quote(selected["id"], safe="") + "/patterns/" + quote(pattern, safe="")
    with urlopen(root + path + "?" + urlencode(dict(city=city)), timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("Germany pattern did not return HTTP 200")
        if not json.load(response)["pattern"].get("stops"):
            raise RuntimeError("Germany pattern has no stops")
'''


LEGACY_SMOKE_EXPECTATION = '''
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from static_departures_api import Database
from gtfs_board_window import service_day_epoch

def smoke_expectation(database_path, city, stop):
    database = Database(database_path)
    try:
        _, timezone_name, _, _ = database.city_departure_mode(city)
        zone = ZoneInfo(timezone_name)
        connection = database._connection()
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        start = datetime.combine(datetime.now(zone).date(), datetime.min.time(), zone)
        end = datetime.fromisoformat(metadata["validThrough"]).replace(tzinfo=zone) + timedelta(days=1)
        if end <= start:
            raise RuntimeError("fallback schedule window expired")
        stop_ids = database._external_stop_time_ids(city, stop)
        if not stop_ids:
            return start, end, timezone_name, False
        connection.create_function("readiness_service_epoch", 1,
                                   lambda day: service_day_epoch(str(day), timezone_name),
                                   deterministic=True)
        placeholders = ",".join("?" for _ in stop_ids)
        # Test source existence independently of board ordering and its row limit.
        row = connection.execute(
            "SELECT 1 FROM stop_times st JOIN trips t ON t.trip_id=st.trip_id "
            "JOIN active_services a ON a.service_id=t.service_id "
            "WHERE st.raw_stop_id IN (" + placeholders + ") "
            "AND readiness_service_epoch(a.service_date)+st.departure_seconds>=? "
            "AND readiness_service_epoch(a.service_date)+st.departure_seconds<? LIMIT 1",
            (*stop_ids, start.timestamp(), end.timestamp()),
        ).fetchone()
        return start, end, timezone_name, row is not None
    finally:
        if database.connection is not None:
            database.connection.close()
'''


STATIC_PROBE = LEGACY_SMOKE_EXPECTATION + '''
import json, os, sys
from urllib.parse import urlencode
from urllib.request import urlopen
root = "http://127.0.0.1:8080"
def get(path):
    with urlopen(root + path, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("HalteWecker endpoint did not return HTTP 200")
        return json.load(response)
expected, raw_providers = sys.argv[1:]
providers = tuple(raw_providers.split(","))
providers = tuple(value for value in providers if value)
configured_providers = tuple(
    value for value in os.environ.get("HALTEWECKER_STATIC_DEPARTURES_PROVIDER_IDS", "").split(",") if value
)
if configured_providers != providers:
    raise RuntimeError("runtime provider contract mismatch")
metadata = get("/static-departures/health").get("database", {})
if metadata.get("releaseID") != expected:
    raise RuntimeError("runtime release mismatch")
runtime_mode = os.environ.get("HALTEWECKER_EXPECTED_RUNTIME_MODE", "legacy")
if runtime_mode == "legacy" and os.environ.get("HALTEWECKER_STATIC_DEPARTURES_SHADOW_RUNTIME", "0").strip().lower() in {"1", "true", "yes", "on"}:
    raise RuntimeError("legacy runtime unexpectedly enables provider shadow queries")
if runtime_mode == "legacy" and os.environ.get("DEPARTURES_DATABASE", "") != "/data/current-release/departures.sqlite":
    raise RuntimeError("legacy runtime is not reading the active shared departures SQLite")
if runtime_mode == "hybrid":
    if metadata.get("runtimeMode") != "hybrid" or metadata.get("fallbackReleaseID") != expected:
        raise RuntimeError("rollback runtime has no validated full fallback database")
elif metadata.get("runtimeMode") in {"provider", "hybrid"} or metadata.get("fallbackReleaseID"):
    raise RuntimeError("runtime is not using the shared departures database")
elif metadata.get("fallbackProfile") == "hybrid-only":
    raise RuntimeError("runtime departures database omits provider schedules")
for city in ("duisburg", "dusseldorf"):
    payload = get("/static-stop-data/stops/" + city + ".json")
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("empty stop package: " + city)
get("/static-stop-data/transit-radar-cities.json")
for city, stop in (
    ("wien", "at:49:1876:0:1"),
    ("wien", "at:49:1320:5"),
    ("helsinki", "fi-hsl:1040401"),
):
    start, end, zone, scheduled = smoke_expectation(
        os.environ.get("DEPARTURES_DATABASE", "/data/departures-current.sqlite"), city, stop)
    payload = get("/static-departures/board?" + urlencode(dict(
        cityID=city, stopID=stop, limit=3, **{"from": start.isoformat(), "to": end.isoformat()})))
    departures = payload.get("departures")
    if not isinstance(departures, list) or (scheduled and not departures):
        raise RuntimeError("empty departures response: " + city + "/" + stop)
    from static_departures_api import departure_datetime
    epochs = [departure_datetime(row, zone).timestamp() for row in departures]
    if epochs != sorted(epochs) or any(not start.timestamp() <= value < end.timestamp() for value in epochs):
        raise RuntimeError("invalid departure times: " + city + "/" + stop)
'''


def docker(*arguments: str) -> str:
    return subprocess.check_output(["docker", *arguments], text=True)


def container_state(container: str) -> dict:
    return json.loads(docker("inspect", container))[0]


def require_identity(container: str, expected_image: str, expected_id: str) -> None:
    state = container_state(container)
    if state["Image"] != expected_image or state["Id"] != expected_id:
        raise RuntimeError("RouteRecall container/image changed during release activation")


def check_route(container: str, image: str, identifier: str) -> None:
    require_identity(container, image, identifier)
    docker("exec", container, "python3", "-c", ROUTE_PROBE)
    require_identity(container, image, identifier)


def check_shared_route(container: str, image: str, identifier: str) -> None:
    state = container_state(container)
    if state["Image"] != image or state["Id"] != identifier:
        raise RuntimeError("RouteRecall container/image changed during shared SQLite activation")
    environment = dict(
        value.split("=", 1)
        for value in state.get("Config", {}).get("Env", [])
        if "=" in value
    )
    if environment.get("ROUTERECALL_PROVIDER_RUNTIME") != "0":
        raise RuntimeError("RouteRecall still has provider-shard runtime enabled")
    if environment.get("ROUTERECALL_DATABASE") != "/data/haltewecker/current-release/departures.sqlite":
        raise RuntimeError("RouteRecall is not configured for the shared departures SQLite")
    if environment.get("ROUTERECALL_RELEASE_POINTER") != "/data/haltewecker/current-release":
        raise RuntimeError("RouteRecall release pointer is not the shared release pointer")
    docker("exec", container, "python3", "-c", ROUTE_PROBE)
    require_identity(container, image, identifier)


def reload_route(container: str, image: str, identifier: str) -> None:
    require_identity(container, image, identifier)
    docker("restart", container)
    require_identity(container, image, identifier)


def stop_data_root(release_root: str) -> Path:
    root = Path(release_root).resolve(strict=True)
    release_manifest = root / "release.json"
    if release_manifest.is_file():
        payload = json.loads(release_manifest.read_text(encoding="utf-8"))
        entry = payload["stopData"]
        relative = entry.get("path", "stop-data") if isinstance(entry, dict) else entry
    else:
        metadata_path = root / "release-metadata.json"
        if not metadata_path.is_file():
            raise RuntimeError(f"release metadata is missing: {root}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("releaseID") != root.name:
            raise RuntimeError(f"release metadata ID does not match directory: {root}")
        relative = "stop-data"
    return (root / str(relative or "stop-data")).resolve(strict=True)


def restore_route(container: str, image: str, identifier: str, *, reload: bool = False) -> None:
    """Restore only the captured container; never build, pull or substitute an image."""
    previous = container_state(identifier)
    if previous["Image"] != image:
        raise RuntimeError("original RouteRecall container is unavailable; operator recovery required")
    try:
        current = container_state(container)
    except subprocess.CalledProcessError:
        # A replacement may have removed the name, but not the captured container.
        docker("rename", identifier, container)
        docker("start", container)
        require_identity(container, image, identifier)
        return
    if current["Id"] == identifier:
        if reload:
            reload_route(container, image, identifier)
        elif not current.get("State", {}).get("Running", True):
            docker("start", container)
        require_identity(container, image, identifier)
        return
    failed_name = container + "-failed-activation-" + current["Id"][:12]
    docker("stop", "--time", "10", container)
    docker("rename", container, failed_name)
    docker("rename", identifier, container)
    docker("start", container)
    require_identity(container, image, identifier)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("consumer", choices=("route", "shared-route", "haltewecker", "restore-route", "reload-route", "stop-data-root"))
    parser.add_argument("--container", default="")
    parser.add_argument("--image", default="")
    parser.add_argument("--container-id", default="")
    parser.add_argument("--release-id", default="")
    parser.add_argument("--providers", default="")
    parser.add_argument("--runtime-mode", choices=("legacy", "hybrid"), default="legacy")
    parser.add_argument("--release-root", default="")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    if args.consumer == "stop-data-root":
        if not args.release_root:
            parser.error("--release-root is required")
        print(stop_data_root(args.release_root))
        return
    if not args.container:
        parser.error("--container is required")
    if args.consumer == "route":
        check_route(args.container, args.image, args.container_id)
    elif args.consumer == "shared-route":
        check_shared_route(args.container, args.image, args.container_id)
    elif args.consumer == "restore-route":
        restore_route(args.container, args.image, args.container_id, reload=args.reload)
    elif args.consumer == "reload-route":
        reload_route(args.container, args.image, args.container_id)
    else:
        docker("exec", "-e", f"HALTEWECKER_EXPECTED_RUNTIME_MODE={args.runtime_mode}",
               args.container, "python3", "-c", STATIC_PROBE, args.release_id, args.providers)


if __name__ == "__main__":
    main()
