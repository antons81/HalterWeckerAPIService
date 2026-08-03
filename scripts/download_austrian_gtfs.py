#!/usr/bin/env python3
"""Download and atomically activate the currently active MVO GTFS versions."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from austrian_sources import DEFAULT_REGISTRY, load_austrian_sources
from gtfs_source_cache import DEFAULT_CACHE_ROOT, GTFSArtifactCache


DEFAULT_ENV_FILE = Path("/srv/haltewecker/data/austria/.env")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def request_json(url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> object:
    request = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"MVO request failed with HTTP {response.status}")
        return json.load(response)


def token(env: dict[str, str]) -> str:
    body = urllib.parse.urlencode({
        "grant_type": "password",
        "username": env["MVO_USERNAME"],
        "password": env["MVO_PASSWORD"],
        "client_id": env.get("MVO_CLIENT_ID", "dbp-public-ui"),
    }).encode()
    payload = request_json(env["MVO_TOKEN_URL"], {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, body)
    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
        raise RuntimeError("MVO token response did not contain access_token")
    return payload["access_token"]


def active_version(dataset: dict[str, object]) -> tuple[int, dict[str, object] | None]:
    candidates: list[int] = []
    version_by_year: dict[int, dict[str, object]] = {}
    versions = dataset.get("activeVersions")
    if isinstance(versions, list):
        for version in versions:
            value = version.get("year") if isinstance(version, dict) else version
            try:
                year = int(value)
                candidates.append(year)
                if isinstance(version, dict):
                    version_by_year[year] = version
            except (TypeError, ValueError):
                pass
    try:
        candidates.append(int(dataset.get("year")))
    except (TypeError, ValueError):
        pass
    if not candidates:
        raise RuntimeError(f"MVO dataset {dataset.get('id')} has no active year")
    year = max(candidates)
    return year, version_by_year.get(year)


def download_source(
    source: dict[str, object],
    catalog: list[dict[str, object]],
    token_value: str,
    target_dir: Path,
    env: dict[str, str],
    cache: GTFSArtifactCache,
) -> dict[str, object]:
    dataset = next((item for item in catalog if int(item.get("id", -1)) == int(source["datasetId"])), None)
    if dataset is None:
        raise RuntimeError(f"MVO dataset {source['datasetId']} is absent from catalog")
    year, version = active_version(dataset)
    version = version or dataset
    original_name = str(version.get("originalName") or dataset.get("originalName") or f"{source['id']}-{year}.zip")
    expected_size = version.get("size") or dataset.get("size")
    url = f"{env['MVO_API_BASE'].rstrip('/')}/data-sets/{source['datasetId']}/{year}/file"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source['id']}-{year}.zip"
    result = cache.resolve(
        str(source["id"]),
        url,
        headers={"Authorization": f"Bearer {token_value}", "Accept": "application/zip"},
        source_version={
            "datasetId": int(source["datasetId"]),
            "year": year,
            "originalName": original_name,
            "size": expected_size,
        },
        allow_stale=True,
        seed_path=target,
        minimum_size=int(expected_size) if expected_size is not None else None,
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    temporary_link = target.with_name(f".{target.name}.next")
    temporary_link.unlink(missing_ok=True)
    temporary_link.symlink_to(result.path)
    os.replace(temporary_link, target)
    return {
        "source": source["id"],
        "datasetId": source["datasetId"],
        "year": year,
        "originalName": original_name,
        "size": result.state.get("size") if result.state else expected_size,
        "path": str(target),
        "status": result.status,
        "reason": result.reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--output", type=Path, default=Path("/srv/haltewecker/data/austria"))
    parser.add_argument("--cache-root", type=Path, default=Path(os.environ.get("GTFS_CACHE_ROOT", str(DEFAULT_CACHE_ROOT))))
    args = parser.parse_args()
    env = read_env(args.env_file)
    required = ("MVO_USERNAME", "MVO_PASSWORD", "MVO_TOKEN_URL", "MVO_API_BASE")
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise RuntimeError(f"Missing MVO configuration: {', '.join(missing)}")
    catalog_payload = request_json(f"{env['MVO_API_BASE'].rstrip('/')}/data-sets?tagFilterModeInclusive=true", {"Accept": "application/json"})
    catalog = catalog_payload.get("dataSets", catalog_payload) if isinstance(catalog_payload, dict) else catalog_payload
    if not isinstance(catalog, list):
        raise RuntimeError("MVO catalog has no dataset list")
    results = []
    access_token = token(env)
    cache = GTFSArtifactCache(args.cache_root / "austria")
    for source in load_austrian_sources(args.registry):
        started = time.monotonic()
        result = download_source(source, [item for item in catalog if isinstance(item, dict)], access_token, args.output, env, cache)
        results.append(result)
        print(f"[GTFSCache] source=austria:{source['id']} stage=artifact status={result['status']} duration={time.monotonic() - started:.2f}s")
    print(json.dumps({"sources": results}, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
