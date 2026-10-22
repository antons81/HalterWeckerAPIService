#!/usr/bin/env python3
"""Read-only consumer preflight before switching the shared release pointer."""

from __future__ import annotations

import argparse
import hashlib
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
from scripts.static_departures_coverage import validate_city_coverage


PRODUCTION_CONTROL_CITIES = frozenset({
    "wien", "helsinki", "wuppertal", "bochum", "berlin",
    "bad-homburg-vor-der-hohe", "israel", "toronto", "chicago",
    "oslo", "stockholm", "boston", "montreal",
})


def candidate_control_cities(coverage: dict[str, object]) -> list[str]:
    checks = coverage.get("checks", [])
    if not isinstance(checks, list):
        return []
    present = {
        str(row["cityID"])
        for row in checks
        if isinstance(row, dict) and row.get("cityID")
    }
    return sorted(PRODUCTION_CONTROL_CITIES & present)


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
for smoke in plan["smokeChecks"]:
    city, stop = smoke["cityID"], smoke["stopID"]
    check = next(row for row in plan["checks"] if row["cityID"] == city)
    rows = board(city, stop, check["from"], check["to"])
    if smoke["hasSchedule"] and not rows:
        raise RuntimeError("candidate lost scheduled smoke departures: " + city + "/" + stop)
for check in plan["packageChecks"]:
    with urlopen(root + "/static-stop-data/departures/" + check["cityID"] + ".json", timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload.get("stops"), dict) or len(payload["stops"]) != check["stopCount"] or not any(isinstance(rows, list) and rows for rows in payload["stops"].values()):
        raise RuntimeError("candidate package city is unavailable: " + check["cityID"])
print("HALTEWECKER_PREFLIGHT_RESULT=" + json.dumps({"status": "PASS", "releaseID": plan["releaseID"], "checkedCities": plan["supportedCities"], "apiCities": plan["apiCities"], "packageCities": plan["packageCities"], "samples": samples}))
'''


CONTROL_FLOW_PROBE = '''
import json, sys
from urllib.parse import urlencode
from urllib.request import urlopen
with open(sys.argv[1]) as source:
    plan = json.load(source)
root = "http://127.0.0.1:8080"
def get(path):
    with urlopen(root + path, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("control flow unavailable: " + path)
        return json.load(response)
controls = set(plan.get("controlCities", []))
results = {}
for check in plan["checks"]:
    city, stop = check["cityID"], check["stopID"]
    if city not in controls:
        continue
    stops = get("/static-stop-data/stops/" + city + ".json")
    selected = next((row for row in stops if str(row.get("id")) == stop), None)
    if selected is None or not all(isinstance(selected.get(key), (int, float)) for key in ("latitude", "longitude")):
        raise RuntimeError("Nearby stop lookup/coordinates missing: " + city + "/" + stop)
    query = {"cityID": city, "stopID": stop, "limit": 3}
    default_board = get("/static-departures/board?" + urlencode(query))
    if not isinstance(default_board.get("departures"), list):
        raise RuntimeError("invalid default board: " + city)
    query.update({"from": check["from"], "to": check["to"]})
    rows = get("/static-departures/board?" + urlencode(query))["departures"]
    if check["hasSchedule"] and not rows:
        raise RuntimeError("Nearby scheduled board missing: " + city)
    trip_count = 0
    if rows:
        row = rows[0]
        query = {"cityID": city, "tripID": row["tripID"]}
        if row.get("serviceDate"):
            query["serviceDate"] = row["serviceDate"]
        trip = get("/static-departures/trip?" + urlencode(query))
        stops = trip.get("stops")
        if not isinstance(stops, list) or not stops:
            raise RuntimeError("trip details lost stops: " + city)
        trip_count = len(stops)
    results[city] = {"stopLookup": "PASS", "nearbyBoard": "PASS", "defaultRows": len(default_board["departures"]),
                     "rangedRows": len(rows), "tripStops": trip_count}
print("HALTEWECKER_PREFLIGHT_RESULT=" + json.dumps({"status": "PASS", "controls": results}))
'''


def parse_probe_result(output: str) -> dict:
    """Read the explicit result record without interpreting diagnostic logs."""
    prefix = "HALTEWECKER_PREFLIGHT_RESULT="
    records = [line[len(prefix):] for line in output.splitlines() if line.startswith(prefix)]
    if len(records) != 1:
        raise ValueError("consumer probe must emit exactly one result record")
    result = json.loads(records[0])
    if not isinstance(result, dict) or result.get("status") != "PASS":
        raise ValueError("consumer probe did not pass")
    return result


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


def image_runtime_contract(image: str, repository: Path) -> dict[str, str]:
    """Reject stale runtime semantics before accepting candidate validation."""
    files = ("services/static_departures_api.py", "services/static_departures_runtime.py",
             "services/gtfs_board_window.py", "services/vbb_overlay_provider.py",
             "services/validation_receipt.py", "scripts/provider_artifact_capabilities.py",
             "config/incremental-provider-sources.json")
    expected = {name: hashlib.sha256((repository / name).read_bytes()).hexdigest() for name in files}
    code = (
        "import hashlib,json,sys; from pathlib import Path; "
        "files=json.loads(sys.argv[1]); "
        "print(json.dumps({p:hashlib.sha256((Path('/app') / "
        "(p if p.startswith('config/') else Path(p).name)).read_bytes()).hexdigest() for p in files}))"
    )
    actual = docker_json("run", "--rm", "--read-only", "--network", "none",
                         "--entrypoint", "python3", image, "-c", code, json.dumps(files))
    changed = [name for name in files if actual.get(name) != expected[name]]
    if changed:
        raise ValueError("preflight/API image runtime version mismatch: " + ", ".join(changed))
    return expected


def static_consumer_environment(runtime_mode: str, runtime: tuple[str, ...],
                                release_path: str, pointer_path: str) -> dict[str, str]:
    hybrid_enabled = "1" if runtime_mode == "hybrid" else "0"
    return {
        "HALTEWECKER_STATIC_DEPARTURES_RUNTIME_MODE": runtime_mode,
        "HALTEWECKER_STATIC_DEPARTURES_PROVIDER_RUNTIME": "0",
        "HALTEWECKER_STATIC_DEPARTURES_SHADOW_RUNTIME": "0",
        "HALTEWECKER_STATIC_DEPARTURES_PROVIDER_IDS": ",".join(runtime),
        "HALTEWECKER_STATIC_DEPARTURES_PROVIDER_RELEASE_POINTER": "/data/" + pointer_path,
        "HALTEWECKER_STATIC_DEPARTURES_HYBRID_RUNTIME": hybrid_enabled,
        "HALTEWECKER_STATIC_DEPARTURES_HYBRID_PROVIDERS": ",".join(runtime),
        "HALTEWECKER_STATIC_DEPARTURES_HYBRID_RELEASE_POINTER": "/data/" + pointer_path,
        "DEPARTURES_DATABASE": "/data/" + release_path + "/departures.sqlite",
        "STATIC_DATA_ROOT": "/data/" + release_path + "/stop-data",
        "APPLE_NOTIFICATION_STORE_PATH": "/tmp/preflight-events.sqlite3",
        "PORT": "8080",
    }


def route_consumer_configuration(
    release: Path,
    data_root: Path,
    geometry_root: Path,
    *,
    full_database_candidate: bool,
    release_relative: str,
    pointer_relative: str,
) -> tuple[dict[str, str], list[tuple[Path, str]]]:
    environment = {
        "ROUTERECALL_PROVIDER_RUNTIME": "0" if full_database_candidate else "1",
        "ROUTERECALL_HOST": "0.0.0.0",
        "ROUTERECALL_PORT": "8091",
        "ROUTERECALL_GEOMETRY_ARTIFACT": "/data/routerecall/osm/current/de/geometry.sqlite",
    }
    if full_database_candidate:
        transit_root = "/data/haltewecker/current-release"
        environment.update({
            "ROUTERECALL_RELEASE_POINTER": transit_root,
            "ROUTERECALL_DATABASE": transit_root + "/departures.sqlite",
            "ROUTERECALL_STATIC_DATA_ROOT": transit_root + "/stop-data",
        })
        mounts = [(release.resolve(), transit_root)]
    else:
        environment.update({
            "ROUTERECALL_RELEASE_POINTER": "/data/haltewecker/" + pointer_relative,
            "ROUTERECALL_STATIC_DATA_ROOT": "/data/haltewecker/" + release_relative + "/stop-data",
        })
        mounts = [(data_root, "/data/haltewecker")]
    mounts.append((geometry_root, "/data/routerecall/osm"))
    return environment, mounts


def preflight(release: Path, build: tuple[str, ...], runtime: tuple[str, ...],
              static_container: str, route_container: str, *, reference: Path | None = None,
              static_image: str | None = None,
              require_current_schedules: bool = False) -> dict[str, object]:
    release = release.resolve(strict=True)
    release_manifest = release / "release.json"
    full_database_candidate = not release_manifest.is_file()
    manifest_path = release_manifest if release_manifest.is_file() else release / "release-metadata.json"
    manifest = json.loads(manifest_path.read_text())
    if full_database_candidate:
        if build or runtime:
            raise ValueError("full SQLite candidate must not declare provider runtime shards")
        if not (release / "departures.sqlite").is_file():
            raise ValueError("full SQLite candidate has no departures.sqlite")
        requirements = {"mode": "full-sqlite", "database": str(release / "departures.sqlite")}
        supported = ()
    else:
        published_providers = set(manifest["providers"])
        if not set(runtime) <= published_providers:
            raise ValueError("candidate omits a configured runtime provider shard")
        if not published_providers <= set(build):
            raise ValueError("candidate publishes a provider outside the build configuration")
        if "germany" not in published_providers:
            raise ValueError("candidate omits the Germany shard required by RouteRecall")
        supported = image_supported_providers(static_image or docker_json("inspect", static_container)[0]["Image"], runtime)
        validate_provider_contract(build, runtime, supported)
        requirements = validate_shared_release(
            release, require_current_services=require_current_schedules,
        )
    static_image = static_image or docker_json("inspect", static_container)[0]["Image"]
    route_image = docker_json("inspect", route_container)[0]["Image"]
    runtime_contract = image_runtime_contract(static_image, Path(__file__).resolve().parents[1])
    coverage = validate_city_coverage(
        release, runtime, Path(__file__).resolve().parents[1], reference=reference,
        require_current_schedules=require_current_schedules,
    )
    coverage["controlCities"] = candidate_control_cities(coverage)
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
        runtime_mode = "legacy" if full_database_candidate else "hybrid"
        static_name, static = start("stopdata", static_image, 8080,
                       [(data_root, "/data")],
                       static_consumer_environment(runtime_mode, runtime, relative, pointer_relative))
        health = get(static + "/static-departures/health", startup=True)
        metadata = health.get("database", {})
        if metadata.get("releaseID") != manifest["releaseID"]:
            raise ValueError("HalteWecker health resolved a different release")
        if runtime_mode == "legacy":
            if metadata.get("runtimeMode") in {"provider", "hybrid"} or metadata.get("fallbackReleaseID"):
                raise ValueError("candidate API is still using provider or hybrid runtime")
            if metadata.get("fallbackProfile") == "hybrid-only":
                raise ValueError("candidate API database omits provider schedules")
        elif metadata.get("runtimeMode") != "hybrid" or metadata.get("fallbackReleaseID") != manifest["releaseID"]:
            raise ValueError("rollback image has no validated hybrid fallback")
        candidate_coverage = parse_probe_result(subprocess.check_output(
            ["docker", "exec", static_name, "python3", "-c", STATIC_CITY_PROBE,
             "/data/" + coverage_path.relative_to(data_root).as_posix()], text=True,
        ))
        control_flows = parse_probe_result(subprocess.check_output(
            ["docker", "exec", static_name, "python3", "-c", CONTROL_FLOW_PROBE,
             "/data/" + coverage_path.relative_to(data_root).as_posix()], text=True,
        ))
        for city in ("duisburg", "dusseldorf"):
            stops = get(static + f"/static-stop-data/stops/{city}.json")
            if not isinstance(stops, list) or not stops:
                raise ValueError(f"candidate stop package is empty: {city}")
            results[-1]["stopCount"] = len(stops)
        route_environment, route_mounts = route_consumer_configuration(
            release,
            data_root,
            geometry_root,
            full_database_candidate=full_database_candidate,
            release_relative=relative,
            pointer_relative=pointer_relative,
        )
        _route_name, route = start("routerecall", route_image, 8091,
                                   route_mounts, route_environment)
        root = route + "/routerecall/v1"
        # Liveness alone is not readiness; the actual Germany flow follows.
        get(root + "/cities", startup=True)
        for city, line in (("berlin", "100"), ("wuppertal", "635")):
            payload = get(root + "/lines/search?" + urlencode({"city": city, "line": line}))
            selected = next(item for item in payload["lines"] if item.get("patterns"))
            results[-1].update(routeID=selected["id"], providerID=selected.get("providerID"),
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
                "databaseMode": "full-sqlite" if full_database_candidate else "hybrid-shards",
                "routeRecallDatabase": route_environment.get("ROUTERECALL_DATABASE"),
                "routeRecallProviderRuntime": route_environment["ROUTERECALL_PROVIDER_RUNTIME"],
                "buildProviders": build, "runtimeProviders": runtime, "supportedRuntimeProviders": supported,
                "staticImage": static_image, "routeImage": route_image, "runtimeContract": runtime_contract,
                "cityCoverage": candidate_coverage, "controlFlows": control_flows, "coveragePlan": coverage, "results": results,
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
    full_database_candidate = not (candidate_root / "release.json").is_file()
    if full_database_candidate:
        static_image = static_image or docker_json("inspect", static_container)[0]["Image"]
        route_image = docker_json("inspect", route_container)[0]["Image"]
        candidate = preflight(
            candidate_root, build, runtime, static_container, route_container,
            reference=rollback, static_image=static_image,
            require_current_schedules=False,
        )
        if candidate["staticImage"] != static_image or candidate["routeImage"] != route_image:
            raise ValueError("consumer image changed during full SQLite candidate preflight")
        if candidate["currentUnchanged"] != str(rollback):
            raise ValueError("production rollback pointer changed during candidate preflight")
        del candidate["coveragePlan"]
        return {
            "status": "PASS",
            "candidate": candidate,
            "verifiedRollback": {
                "status": "PASS",
                "release": str(rollback),
                "reason": "active production release and pointer remained unchanged during candidate preflight",
            },
        }

    rollback_build = tuple(json.loads((rollback / "release.json").read_text())["providers"])
    verified_rollback = preflight(rollback, rollback_build, runtime, static_container,
                                 route_container, static_image=static_image,
                                 require_current_schedules=False)
    candidate = preflight(candidate_root, build, runtime, static_container, route_container,
                          reference=rollback, static_image=verified_rollback["staticImage"],
                          require_current_schedules=False)
    if candidate["staticImage"] != verified_rollback["staticImage"] or candidate["routeImage"] != verified_rollback["routeImage"]:
        raise ValueError("consumer image changed between rollback and candidate preflight")
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
                           arguments.route_container, static_image=arguments.static_image,
                           require_current_schedules=False)
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
