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


STATIC_PROBE = '''
import json, os, sqlite3, sys
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
if os.environ.get("HALTEWECKER_STATIC_DEPARTURES_PROVIDER_IDS", "").split(",") != list(providers):
    raise RuntimeError("runtime provider contract mismatch")
if get("/static-departures/health").get("database", {}).get("releaseID") != expected:
    raise RuntimeError("runtime release mismatch")
for city in ("duisburg", "dusseldorf"):
    payload = get("/static-stop-data/stops/" + city + ".json")
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("empty stop package: " + city)
get("/static-stop-data/transit-radar-cities.json")
with sqlite3.connect("file:/data/current-release/common.sqlite?mode=ro", uri=True) as connection:
    placeholders = ",".join("?" for _ in providers)
    row = connection.execute("SELECT city_id, stop_id FROM provider_city_stops "
                             "WHERE provider_id IN (" + placeholders + ") LIMIT 1", providers).fetchone()
if row is None:
    raise RuntimeError("no runtime stop for departures readiness")
payload = get("/static-departures/board?" + urlencode(dict(cityID=row[0], stopID=row[1], limit=3)))
if not isinstance(payload.get("departures"), list):
    raise RuntimeError("invalid departures response")
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


def reload_route(container: str, image: str, identifier: str) -> None:
    require_identity(container, image, identifier)
    docker("restart", container)
    require_identity(container, image, identifier)


def stop_data_root(release_root: str) -> Path:
    root = Path(release_root).resolve(strict=True)
    entry = json.loads((root / "release.json").read_text())["stopData"]
    relative = entry.get("path", "stop-data") if isinstance(entry, dict) else entry
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
    parser.add_argument("consumer", choices=("route", "haltewecker", "restore-route", "reload-route", "stop-data-root"))
    parser.add_argument("--container", default="")
    parser.add_argument("--image", default="")
    parser.add_argument("--container-id", default="")
    parser.add_argument("--release-id", default="")
    parser.add_argument("--providers", default="")
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
    elif args.consumer == "restore-route":
        restore_route(args.container, args.image, args.container_id, reload=args.reload)
    elif args.consumer == "reload-route":
        reload_route(args.container, args.image, args.container_id)
    else:
        docker("exec", args.container, "python3", "-c", STATIC_PROBE, args.release_id, args.providers)


if __name__ == "__main__":
    main()
