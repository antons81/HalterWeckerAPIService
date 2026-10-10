#!/usr/bin/env python3
"""Read-only consumer preflight before switching the shared release pointer."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.release_activation_requirements import validate_provider_contract, validate_shared_release
from scripts.static_departures_coverage import validate_city_coverage, require_preserved_schedules


STATIC_CITY_PROBE = '''
import json, sys
from urllib.parse import urlencode
from urllib.request import urlopen
from static_departures_api import departure_datetime, parse_iso_boundary
with open(sys.argv[1]) as source:
    plan = json.load(source)
root = "http://127.0.0.1:8080"
samples = {}
def board(city, stop, start, end):
    check = next(row for row in plan["checks"] if row["cityID"] == city)
    zone = check.get("timezone", "Europe/Berlin")
    lower, upper = parse_iso_boundary(start, zone).timestamp(), parse_iso_boundary(end, zone).timestamp()
    query = urlencode(dict(cityID=city, stopID=stop, limit=3, **{"from": start, "to": end}))
    with urlopen(root + "/static-departures/board?" + query, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("candidate city is unavailable: " + city)
        payload = json.load(response)
    if not isinstance(payload.get("departures"), list):
        raise RuntimeError("invalid candidate board: " + city)
    epochs = [departure_datetime(row, zone).timestamp() for row in payload["departures"]]
    if any(not lower <= value <= upper for value in epochs):
        raise RuntimeError("departure outside candidate board interval: " + city)
    if epochs != sorted(epochs):
        raise RuntimeError("candidate board is not chronological: " + city)
    return payload["departures"]
for check in plan["checks"]:
    city, stop = check["cityID"], check["stopID"]
    rows = board(city, stop, check["from"], check["to"])
    if check["hasSchedule"] and not rows:
        raise RuntimeError("candidate lost scheduled departures: " + city + "/" + stop)
    if city in {"wien", "helsinki", "bochum", "berlin", "israel", "toronto", "chicago", "oslo", "stockholm", "boston", "montreal"}:
        samples[city] = {"stopID": stop, "departures": len(rows), "backend": check["backend"]}
for city, stop in (("wien", "at:49:1876:0:1"), ("wien", "at:49:1320:5"), ("helsinki", "fi-hsl:1040401")):
    check = next(row for row in plan["checks"] if row["cityID"] == city)
    if not board(city, stop, check["from"], check["to"]):
        raise RuntimeError("candidate smoke board is empty: " + city + "/" + stop)
for check in plan["packageChecks"]:
    with urlopen(root + "/static-stop-data/departures/" + check["cityID"] + ".json", timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload.get("stops"), dict) or len(payload["stops"]) != check["stopCount"] or not any(isinstance(rows, list) and rows for rows in payload["stops"].values()):
        raise RuntimeError("candidate package city is unavailable: " + check["cityID"])
print(json.dumps({"status": "PASS", "releaseID": plan["releaseID"], "checkedCities": plan["supportedCities"], "apiCities": plan["apiCities"], "packageCities": plan["packageCities"], "samples": samples}))
'''


def provider_ids(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def docker_json(*arguments: str):
    return json.loads(subprocess.check_output(["docker", *arguments], text=True))


def image_supported_providers(image: str, providers: tuple[str, ...]) -> tuple[str, ...]:
    code = (
        "import json,sys; from pathlib import Path; import static_departures_runtime as r; "
        "root=Path(r.__file__).resolve().parents[1]; "
        "print(json.dumps([p for p in json.loads(sys.argv[1]) "
        "if r.provider_capability(root,p,r.SHARD_RUNTIME) "
        "and r.provider_capability(root,p,r.HYBRID_RUNTIME)]))"
    )
    return tuple(docker_json("run", "--rm", "--read-only", "--network", "none",
                             "--entrypoint", "python3", image, "-c", code, json.dumps(providers)))


def preflight(release: Path, build: tuple[str, ...], runtime: tuple[str, ...],
              static_container: str, route_container: str, *, reference: Path | None = None,
              static_image: str | None = None) -> dict[str, object]:
    release = release.resolve(strict=True)
    manifest = json.loads((release / "release.json").read_text())
    if set(manifest["providers"]) != set(build):
        raise ValueError("candidate build membership does not match configuration")
    static_image = static_image or docker_json("inspect", static_container)[0]["Image"]
    route_image = docker_json("inspect", route_container)[0]["Image"]
    supported = image_supported_providers(static_image, runtime)
    validate_provider_contract(build, runtime, supported)
    requirements = validate_shared_release(release)
    coverage = validate_city_coverage(release, runtime, Path(__file__).resolve().parents[1], reference=reference)
    missing_required = {"wien", "helsinki", "bochum", "berlin"} - {row["cityID"] for row in coverage["checks"]}
    if missing_required:
        raise ValueError("required production cities are missing: " + ", ".join(sorted(missing_required)))
    data_root = release.parents[2] if release.parent.name == "incremental" else release.parents[1]
    pointer = data_root / "current-release"
    initial_target = pointer.resolve(strict=True)
    geometry_root = Path("/srv/routerecall/data/osm")
    initial_geometry = (geometry_root / "current/de/geometry.sqlite").resolve(strict=True)
    pointer_workspace = tempfile.TemporaryDirectory(prefix="consumer-preflight-", dir=data_root / "staging")
    candidate_pointer = Path(pointer_workspace.name) / "current-release"
    candidate_pointer.symlink_to(os.path.relpath(release, candidate_pointer.parent))
    coverage_path = candidate_pointer.parent / "city-coverage.json"
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    token = uuid.uuid4().hex[:12]
    names: list[str] = []
    results: list[dict[str, object]] = []

    def start(kind: str, image: str, port: int, mounts: list[tuple[Path, str]],
              environment: dict[str, str]) -> tuple[str, str]:
        name = f"{kind}-release-preflight-{token}"
        arguments = ["run", "-d", "--read-only", "--tmpfs", "/tmp", "--name", name,
                     "--network", "haltewecker", "-p", f"127.0.0.1::{port}"]
        for source, target in mounts:
            arguments.extend(["-v", f"{source}:{target}:ro"])
        for key, value in environment.items():
            arguments.extend(["-e", f"{key}={value}"])
        names.append(name)
        subprocess.run(["docker", *arguments, image], check=True, stdout=subprocess.DEVNULL)
        state = docker_json("inspect", name)[0]
        binding = state["NetworkSettings"]["Ports"][f"{port}/tcp"][0]
        return name, "http://127.0.0.1:" + binding["HostPort"]

    def get(url: str, *, startup: bool = False):
        deadline = time.monotonic() + (60 if startup else 0)
        while True:
            started = time.monotonic()
            try:
                with urlopen(url, timeout=15) as response:
                    payload = json.load(response)
                    results.append({"url": url, "status": response.status,
                                    "elapsed": round(time.monotonic() - started, 3)})
                    return payload
            except Exception:
                if not startup or time.monotonic() >= deadline:
                    raise
                for name in names:
                    state = docker_json("inspect", name)[0]["State"]
                    if state["Status"] == "exited":
                        logs = subprocess.check_output(
                            ["docker", "logs", "--tail", "30", name], stderr=subprocess.STDOUT, text=True,
                        )
                        raise RuntimeError(f"consumer startup failed: {name}; exit={state['ExitCode']}\n{logs}")
                time.sleep(1)

    try:
        relative = release.relative_to(data_root).as_posix()
        pointer_relative = candidate_pointer.relative_to(data_root).as_posix()
        static_name, static = start("stopdata", static_image, 8080,
                       [(data_root, "/data")],
                       {"HALTEWECKER_STATIC_DEPARTURES_RUNTIME_MODE": "hybrid",
                        "HALTEWECKER_STATIC_DEPARTURES_PROVIDER_RUNTIME": "0",
                        "HALTEWECKER_STATIC_DEPARTURES_PROVIDER_IDS": ",".join(runtime),
                        "HALTEWECKER_STATIC_DEPARTURES_PROVIDER_RELEASE_POINTER": "/data/" + pointer_relative,
                        "HALTEWECKER_STATIC_DEPARTURES_HYBRID_RUNTIME": "1",
                        "HALTEWECKER_STATIC_DEPARTURES_HYBRID_PROVIDERS": ",".join(runtime),
                        "HALTEWECKER_STATIC_DEPARTURES_HYBRID_RELEASE_POINTER": "/data/" + pointer_relative,
                        "DEPARTURES_DATABASE": "/data/" + relative + "/departures.sqlite",
                        "STATIC_DATA_ROOT": "/data/" + relative + "/stop-data",
                        "APPLE_NOTIFICATION_STORE_PATH": "/tmp/preflight-events.sqlite3", "PORT": "8080"})
        health = get(static + "/static-departures/health", startup=True)
        metadata = health.get("database", {})
        if metadata.get("releaseID") != manifest["releaseID"]:
            raise ValueError("HalteWecker health resolved a different release")
        if metadata.get("runtimeMode") != "hybrid" or metadata.get("fallbackReleaseID") != manifest["releaseID"]:
            raise ValueError("candidate image has no validated hybrid fallback")
        candidate_coverage = json.loads(subprocess.check_output(
            ["docker", "exec", static_name, "python3", "-c", STATIC_CITY_PROBE,
             "/data/" + coverage_path.relative_to(data_root).as_posix()], text=True,
        ))
        for city in ("duisburg", "dusseldorf"):
            stops = get(static + f"/static-stop-data/stops/{city}.json")
            if not isinstance(stops, list) or not stops:
                raise ValueError(f"candidate stop package is empty: {city}")
            results[-1]["stopCount"] = len(stops)
        _route_name, route = start("routerecall", route_image, 8091,
                      [(data_root, "/data/haltewecker"), (geometry_root, "/data/routerecall/osm")],
                      {"ROUTERECALL_PROVIDER_RUNTIME": "1", "ROUTERECALL_HOST": "0.0.0.0",
                       "ROUTERECALL_PORT": "8091",
                       "ROUTERECALL_RELEASE_POINTER": "/data/haltewecker/" + pointer_relative,
                       "ROUTERECALL_STATIC_DATA_ROOT": "/data/haltewecker/" + relative + "/stop-data",
                       "ROUTERECALL_GEOMETRY_ARTIFACT": "/data/routerecall/osm/current/de/geometry.sqlite"})
        root = route + "/routerecall/v1"
        # Liveness alone is not readiness; the actual Germany flow follows.
        get(root + "/cities", startup=True)
        for city, line in (("berlin", "100"), ("wuppertal", "635")):
            payload = get(root + "/lines/search?" + urlencode({"city": city, "line": line}))
            selected = next(item for item in payload["lines"]
                            if item.get("providerID") == "germany" and item.get("patterns"))
            results[-1].update(routeID=selected["id"], providerID="germany",
                               patternCount=len(selected["patterns"]))
            pattern = selected["patterns"][0]["id"]
            path = "/lines/" + quote(selected["id"], safe="") + "/patterns/" + quote(pattern, safe="")
            payload = get(root + path + "?" + urlencode({"city": city}))
            if not payload["pattern"].get("stops"):
                raise ValueError("Germany pattern has no stops")
            results[-1].update(patternID=pattern, stopCount=len(payload["pattern"]["stops"]))
        if pointer.resolve() != initial_target or (geometry_root / "current/de/geometry.sqlite").resolve() != initial_geometry:
            raise RuntimeError("production pointers changed during preflight")
        return {"status": "PASS", "releaseID": manifest["releaseID"], "requirements": requirements,
                "buildProviders": build, "runtimeProviders": runtime, "supportedRuntimeProviders": supported,
                "staticImage": static_image, "routeImage": route_image,
                "cityCoverage": candidate_coverage, "coveragePlan": coverage, "results": results,
                "currentUnchanged": str(initial_target), "geometryUnchanged": str(initial_geometry)}
    finally:
        for name in reversed(names):
            subprocess.run(["docker", "stop", "--time", "5", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "rm", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pointer_workspace.cleanup()
        if pointer.resolve(strict=True) != initial_target or (geometry_root / "current/de/geometry.sqlite").resolve(strict=True) != initial_geometry:
            raise RuntimeError("production pointers changed during preflight")


def validate_release_pair(candidate_root: Path, rollback_root: Path, build: tuple[str, ...],
                          runtime: tuple[str, ...], static_container: str, route_container: str,
                          *, static_image: str | None = None) -> dict:
    rollback = rollback_root.resolve(strict=True)
    rollback_build = tuple(json.loads((rollback / "release.json").read_text())["providers"])
    verified_rollback = preflight(rollback, rollback_build, runtime, static_container,
                                 route_container, static_image=static_image)
    candidate = preflight(candidate_root, build, runtime, static_container, route_container,
                          reference=rollback, static_image=verified_rollback["staticImage"])
    if candidate["staticImage"] != verified_rollback["staticImage"] or candidate["routeImage"] != verified_rollback["routeImage"]:
        raise ValueError("consumer image changed between rollback and candidate preflight")
    require_preserved_schedules(candidate["coveragePlan"], verified_rollback["coveragePlan"])
    for report in (candidate, verified_rollback):
        del report["coveragePlan"]
    return {"status": "PASS", "candidate": candidate, "verifiedRollback": verified_rollback}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--build-providers", required=True)
    parser.add_argument("--runtime-providers", required=True)
    parser.add_argument("--rollback-release", type=Path, required=True)
    parser.add_argument("--static-image", help="Exact API image SHA to test and later activate")
    parser.add_argument("--reference-only", action="store_true",
                        help="Verify the recovered rollback with a new image before deploying that image")
    parser.add_argument("--static-container", default="static-departures-api")
    parser.add_argument("--route-container", default="routerecall-api")
    arguments = parser.parse_args()
    if arguments.reference_only:
        report = preflight(arguments.rollback_release, provider_ids(arguments.build_providers),
                           provider_ids(arguments.runtime_providers), arguments.static_container,
                           arguments.route_container, static_image=arguments.static_image)
        del report["coveragePlan"]
        print(json.dumps({"status": "PASS", "verifiedRollback": report}))
        return
    print(json.dumps(validate_release_pair(
        arguments.release, arguments.rollback_release, provider_ids(arguments.build_providers),
        provider_ids(arguments.runtime_providers), arguments.static_container,
        arguments.route_container, static_image=arguments.static_image,
    )))


if __name__ == "__main__":
    main()
