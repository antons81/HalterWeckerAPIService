#!/usr/bin/env python3
"""Download and atomically activate the currently active MVO GTFS versions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import ssl
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

from austrian_sources import DEFAULT_REGISTRY, load_austrian_sources
from gtfs_source_cache import DEFAULT_CACHE_ROOT, ArtifactResult, GTFSArtifactCache, validate_gtfs_archive
from artifact_provenance import artifact_provenance, immutable_file_path
from prepare_gtfs_artifacts import gtfs_calendar_horizon, source_calendar_local_date


DEFAULT_ENV_FILE = Path("/srv/haltewecker/data/austria/.env")
MVO_INTERMEDIATE_CERTIFICATE = (
    Path(__file__).resolve().parents[1] / "config" / "tls" / "godaddy-g2-intermediate.pem"
)
# Published DER fingerprint: https://certs.godaddy.com/repository/
MVO_INTERMEDIATE_SHA256 = "973a41276ffd01e027a2aad49e34c37846d3e976ff6a620b6712e33832041aa6"


def mvo_tls_context() -> ssl.SSLContext:
    """Complete MVO's missing intermediate while retaining trusted-root verification."""
    context = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        pass
    else:
        context.load_verify_locations(cafile=certifi.where())
    pem = MVO_INTERMEDIATE_CERTIFICATE.read_text(encoding="ascii")
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    if fingerprint != MVO_INTERMEDIATE_SHA256:
        raise RuntimeError("MVO intermediate certificate fingerprint mismatch")
    context.load_verify_locations(cadata=pem)
    # An intermediate must complete a chain to a trusted root, never become its own anchor.
    context.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN
    return context


def configure_mvo_https() -> None:
    # This dedicated downloader process shares verified TLS for catalog, token and cache HEAD/GET.
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=mvo_tls_context()))
    urllib.request.install_opener(opener)


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
    for attempt in range(1, 4):
        request = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if not 200 <= response.status < 300:
                    raise urllib.error.HTTPError(url, response.status, "MVO request failed", response.headers, None)
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if not mvo_source_unavailable(error) or attempt == 3:
                raise
            print(f"[GTFSCache] source=austria stage=request status=WARN attempt={attempt} "
                  f"reason={mvo_error_reason(error)}", flush=True)
            time.sleep(attempt)
    raise AssertionError("MVO request attempts exhausted")


def mvo_source_unavailable(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {429, 500, 502, 503, 504}
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


def mvo_error_reason(error: BaseException) -> str:
    # Do not include request headers, credentials or token response bodies in reports.
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    return f"network unavailable ({type(error).__name__})"


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
    cache_arguments = dict(
        headers={"Authorization": f"Bearer {token_value}", "Accept": "application/zip"},
        source_version={
            "datasetId": int(source["datasetId"]),
            "year": year,
            "originalName": original_name,
            "size": expected_size,
        },
        seed_path=target,
        minimum_size=int(expected_size) if expected_size is not None else None,
    )
    result = cache.resolve(
        str(source["id"]),
        url,
        allow_stale=True,
        **cache_arguments,
    )
    today = source_calendar_local_date("Europe/Vienna")
    horizon = gtfs_calendar_horizon(result.path)
    if horizon is None or horizon < today:
        if result.status not in {"updated", "local", "snapshot", "preserved-stale"}:
            result = cache.resolve(
                str(source["id"]), url, allow_stale=False,
                force_download=True, **cache_arguments,
            )
            horizon = gtfs_calendar_horizon(result.path)
        if horizon is None or horizon < today:
            horizon_text = horizon.isoformat() if horizon else "no active service dates"
            print(f"[GTFSCache] source=austria:{source['id']} stage=calendar status=WARN "
                  f"serviceThrough={horizon_text} today={today}", flush=True)
    return publish_source_artifact(source, result, target_dir, year, original_name, horizon)


def publish_source_artifact(
    source: dict[str, object], result: ArtifactResult, target_dir: Path,
    year: int, original_name: str, horizon: date | None,
) -> dict[str, object]:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source['id']}-{year}.zip"
    digest = str((result.state or {}).get("sha256") or "")
    if not digest:
        digest, _size = artifact_provenance(result.path)
    immutable_path = immutable_file_path(result.path, digest)
    temporary_link = target.with_name(f".{target.name}.next")
    temporary_link.unlink(missing_ok=True)
    try:
        temporary_link.symlink_to(immutable_path)
        os.replace(temporary_link, target)
    finally:
        temporary_link.unlink(missing_ok=True)
    artifact_size = (result.state or {}).get("size")
    if not isinstance(artifact_size, int) or artifact_size <= 0:
        artifact_size = immutable_path.stat().st_size
    return {
        "source": source["id"],
        "datasetId": source["datasetId"],
        "year": year,
        "originalName": original_name,
        "size": artifact_size,
        "sha256": digest,
        "immutablePath": str(immutable_path),
        "path": str(target),
        "status": result.status,
        "reason": result.reason,
        "serviceThrough": horizon.isoformat() if horizon is not None else "",
        "downloadedAt": (result.state or {}).get("downloadedAt") or (result.state or {}).get("validatedAt"),
    }


def preserve_cached_sources(
    sources: list[dict[str, object]], cache: GTFSArtifactCache, target_dir: Path,
    failure: BaseException,
) -> list[dict[str, object]]:
    """Validate every required cached feed before publishing any fallback links."""
    prepared = []
    errors = []
    reason = "MVO refresh unavailable: " + mvo_error_reason(failure)
    for source in sources:
        source_id = str(source["id"])
        try:
            state = cache.state(source_id)
            version = (state or {}).get("sourceVersion")
            if (not state or state.get("validated") is not True or state.get("sourceID") != source_id
                    or not isinstance(version, dict) or version.get("datasetId") != source["datasetId"]):
                raise ValueError("missing validated cache or dataset ownership mismatch")
            year = int(version["year"])
            if year <= 0:
                raise ValueError("invalid cached dataset year")
            path = cache.root / source_id / "current.zip"
            digest, size = validate_gtfs_archive(path)
            if digest != state.get("sha256") or size != state.get("size"):
                raise ValueError("cached archive SHA-256 or size mismatch")
            immutable = path.with_name(f"{digest}.zip")
            if immutable.exists() and not immutable.samefile(path):
                if validate_gtfs_archive(immutable) != (digest, size):
                    raise ValueError("immutable cached archive does not match current archive")
            horizon = gtfs_calendar_horizon(path)
            result = ArtifactResult(source_id, path, "preserved-stale", reason, state)
            original_name = str(version.get("originalName") or f"{source_id}-{year}.zip")
            prepared.append((source, result, year, original_name, horizon))
        except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as error:
            errors.append(f"{source_id}: {error}")
    if errors:
        raise RuntimeError(reason + "; required Austrian cache is unusable: " + "; ".join(errors)) from failure
    results = []
    for source, result, year, original_name, horizon in prepared:
        entry = publish_source_artifact(source, result, target_dir, year, original_name, horizon)
        print(f"[GTFSCache] source=austria:{source['id']} stage=refresh status=WARN "
              f"artifact=preserved-stale reason={reason} serviceThrough={entry['serviceThrough']} "
              f"downloadedAt={entry['downloadedAt']}", flush=True)
        results.append(entry)
    return results


def refresh_sources(
    sources: list[dict[str, object]], env: dict[str, str], target_dir: Path, cache: GTFSArtifactCache,
) -> list[dict[str, object]]:
    try:
        catalog_payload = request_json(
            f"{env['MVO_API_BASE'].rstrip('/')}/data-sets?tagFilterModeInclusive=true",
            {"Accept": "application/json"},
        )
        catalog = catalog_payload.get("dataSets", catalog_payload) if isinstance(catalog_payload, dict) else catalog_payload
        if not isinstance(catalog, list):
            raise RuntimeError("MVO catalog has no dataset list")
        access_token = token(env)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        if not mvo_source_unavailable(error):
            raise
        return preserve_cached_sources(sources, cache, target_dir, error)
    results = []
    for source in sources:
        started = time.monotonic()
        try:
            result = download_source(source, [item for item in catalog if isinstance(item, dict)],
                                     access_token, target_dir, env, cache)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if not mvo_source_unavailable(error):
                raise
            result = preserve_cached_sources([source], cache, target_dir, error)[0]
        results.append(result)
        status = "WARN" if result["status"] == "preserved-stale" else result["status"]
        print(f"[GTFSCache] source=austria:{source['id']} stage=artifact status={status} "
              f"artifact={result['status']} duration={time.monotonic() - started:.2f}s "
              f"reason={result['reason']}", flush=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--output", type=Path, default=Path("/srv/haltewecker/data/austria"))
    parser.add_argument("--cache-root", type=Path, default=Path(os.environ.get("GTFS_CACHE_ROOT", str(DEFAULT_CACHE_ROOT))))
    parser.add_argument("--output-json", type=Path, default=None)
    args = parser.parse_args()
    env = read_env(args.env_file)
    required = ("MVO_USERNAME", "MVO_PASSWORD", "MVO_TOKEN_URL", "MVO_API_BASE")
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise RuntimeError(f"Missing MVO configuration: {', '.join(missing)}")
    configure_mvo_https()
    cache = GTFSArtifactCache(args.cache_root / "austria")
    results = refresh_sources(load_austrian_sources(args.registry), env, args.output, cache)
    payload = {"sources": results}
    destinations = {args.output / "artifacts.json"}
    if args.output_json is not None:
        destinations.add(args.output_json)
    for destination in sorted(destinations):
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.next")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
