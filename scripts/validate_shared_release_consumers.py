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


def provider_ids(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def docker_json(*arguments: str):
    return json.loads(subprocess.check_output(["docker", *arguments], text=True))


def image_supported_providers(image: str, providers: tuple[str, ...]) -> tuple[str, ...]:
    code = (
        "import json,sys; from pathlib import Path; import static_departures_runtime as r; "
        "root=Path(r.__file__).resolve().parents[1]; "
        "print(json.dumps([p for p in json.loads(sys.argv[1]) "
        "if r.provider_capability(root,p,r.SHARD_RUNTIME)]))"
    )
    return tuple(docker_json("run", "--rm", "--read-only", "--network", "none",
                             "--entrypoint", "python3", image, "-c", code, json.dumps(providers)))


def preflight(release: Path, build: tuple[str, ...], runtime: tuple[str, ...],
              static_container: str, route_container: str) -> dict[str, object]:
    release = release.resolve(strict=True)
    manifest = json.loads((release / "release.json").read_text())
    if set(manifest["providers"]) != set(build):
        raise ValueError("candidate build membership does not match configuration")
    static_image = docker_json("inspect", static_container)[0]["Image"]
    route_image = docker_json("inspect", route_container)[0]["Image"]
    supported = image_supported_providers(static_image, runtime)
    validate_provider_contract(build, runtime, supported)
    requirements = validate_shared_release(release)
    data_root = release.parents[2]
    pointer = data_root / "current-release"
    initial_target = pointer.resolve(strict=True)
    geometry_root = Path("/srv/routerecall/data/osm")
    initial_geometry = (geometry_root / "current/de/geometry.sqlite").resolve(strict=True)
    pointer_workspace = tempfile.TemporaryDirectory(prefix="consumer-preflight-", dir=data_root / "staging")
    candidate_pointer = Path(pointer_workspace.name) / "current-release"
    candidate_pointer.symlink_to(os.path.relpath(release, candidate_pointer.parent))
    token = uuid.uuid4().hex[:12]
    names: list[str] = []
    results: list[dict[str, object]] = []

    def start(kind: str, image: str, port: int, mounts: list[tuple[Path, str]],
              environment: dict[str, str]) -> str:
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
        return "http://127.0.0.1:" + binding["HostPort"]

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
        runtime_module = Path(__file__).resolve().parents[1] / "services/static_departures_runtime.py"
        static = start("stopdata", static_image, 8080,
                       [(data_root, "/data"), (runtime_module, "/app/static_departures_runtime.py")],
                       {"HALTEWECKER_STATIC_DEPARTURES_RUNTIME_MODE": "provider",
                        "HALTEWECKER_STATIC_DEPARTURES_PROVIDER_RUNTIME": "1",
                        "HALTEWECKER_STATIC_DEPARTURES_PROVIDER_IDS": ",".join(runtime),
                        "HALTEWECKER_STATIC_DEPARTURES_PROVIDER_RELEASE_POINTER": "/data/" + pointer_relative,
                        "STATIC_DATA_ROOT": "/data/" + relative + "/stop-data",
                        "APPLE_NOTIFICATION_STORE_PATH": "/tmp/preflight-events.sqlite3", "PORT": "8080"})
        health = get(static + "/static-departures/health", startup=True)
        if health.get("database", {}).get("releaseID") != manifest["releaseID"]:
            raise ValueError("HalteWecker health resolved a different release")
        for city in ("duisburg", "dusseldorf"):
            stops = get(static + f"/static-stop-data/stops/{city}.json")
            if not isinstance(stops, list) or not stops:
                raise ValueError(f"candidate stop package is empty: {city}")
            results[-1]["stopCount"] = len(stops)
        route = start("routerecall", route_image, 8091,
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
                "runtimeModuleOverlay": str(runtime_module), "results": results,
                "currentUnchanged": str(initial_target), "geometryUnchanged": str(initial_geometry)}
    finally:
        for name in reversed(names):
            subprocess.run(["docker", "stop", "--time", "5", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "rm", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pointer_workspace.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--build-providers", required=True)
    parser.add_argument("--runtime-providers", required=True)
    parser.add_argument("--static-container", default="static-departures-api")
    parser.add_argument("--route-container", default="routerecall-api")
    arguments = parser.parse_args()
    print(json.dumps(preflight(arguments.release, provider_ids(arguments.build_providers),
                              provider_ids(arguments.runtime_providers), arguments.static_container,
                              arguments.route_container)))


if __name__ == "__main__":
    main()
