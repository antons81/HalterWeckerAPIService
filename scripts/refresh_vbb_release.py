#!/usr/bin/env python3
"""Build, validate, and optionally activate a VBB-only release refresh."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Existing pipeline modules use both package and script-local imports.
SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, SCRIPT_DIRECTORY)

try:
    from .artifact_provenance import artifact_provenance
    from .build_stop_packages import build_vbb_network_indexes, load_cities
    from .gtfs_source_cache import DEFAULT_CACHE_ROOT, GTFSArtifactCache
    from .static_departures_scoped import run_readiness
    from .validate_release_consistency import validate_release
except ImportError:
    from artifact_provenance import artifact_provenance
    from build_stop_packages import build_vbb_network_indexes, load_cities
    from gtfs_source_cache import DEFAULT_CACHE_ROOT, GTFSArtifactCache
    from static_departures_scoped import run_readiness
    from validate_release_consistency import validate_release


DEFAULT_VBB_URL = (
    "https://unternehmen.vbb.de/fileadmin/user_upload/VBB/Dokumente/"
    "API-Datensaetze/gtfs-mastscharf/GTFS.zip"
)
PRODUCTION_FLOOR_BYTES = 45 * 1024**3


class VBBRefreshError(ValueError):
    """Raised when a scoped refresh cannot produce a coherent candidate."""


def _json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VBBRefreshError(f"invalid JSON: {path}") from error
    if not isinstance(payload, dict):
        raise VBBRefreshError(f"expected JSON object: {path}")
    return payload


def _same_device(first: Path, second: Path) -> bool:
    return first.stat().st_dev == second.stat().st_dev


def _hardlink_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise VBBRefreshError(f"base release is not a directory: {source}")
    if not _same_device(source, destination.parent):
        raise VBBRefreshError("base release and candidate are on different filesystems")
    for source_path in sorted(source.rglob("*")):
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if source_path.is_dir():
            destination_path.mkdir(parents=True, exist_ok=True)
        elif source_path.is_symlink():
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.symlink_to(os.readlink(source_path))
        elif source_path.is_file():
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source_path, destination_path, follow_symlinks=False)
            except OSError as error:
                raise VBBRefreshError(
                    f"cannot hardlink base release file {source_path}: {error}"
                ) from error
        else:
            raise VBBRefreshError(f"unsupported file in base release: {source_path}")


def _write_copy_on_write(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.refresh-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _resolve_vbb_artifact(
    cache_root: Path,
    url: str,
) -> tuple[Path, str, int]:
    artifact = GTFSArtifactCache(cache_root).resolve(
        "vbb",
        url,
        allow_stale=False,
        metadata_probe=True,
    )
    digest, size = artifact_provenance(artifact.path)
    return artifact.path, digest, size


def _base_release(data_root: Path) -> tuple[Path, str]:
    pointer = data_root / "current-release"
    if not pointer.is_symlink():
        raise VBBRefreshError("current-release is not a symlink")
    release = pointer.resolve()
    releases_root = (data_root / "releases").resolve()
    if releases_root not in release.parents:
        raise VBBRefreshError("current-release resolves outside releases root")
    required = (
        release / "release-metadata.json",
        release / "stop-data" / "manifest.json",
        release / "departures.sqlite",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise VBBRefreshError("base release is incomplete: " + ", ".join(missing))
    metadata = _json_object(release / "release-metadata.json")
    release_id = metadata.get("releaseID")
    if not isinstance(release_id, str) or not release_id:
        raise VBBRefreshError("base release metadata has no releaseID")
    return release, release_id


def _disk_preflight(data_root: Path, expected_output_bytes: int) -> dict[str, int]:
    usage = shutil.disk_usage(data_root)
    packaging_overhead = 512 * 1024 * 1024
    projected_minimum = usage.free - expected_output_bytes - packaging_overhead
    result = {
        "free_before": usage.free,
        "expected_vbb_output": expected_output_bytes,
        "packaging_overhead": packaging_overhead,
        "projected_minimum": projected_minimum,
    }
    print(
        "[VBBRefresh] disk "
        f"free_before={usage.free} expected_vbb_output={expected_output_bytes} "
        f"packaging_overhead={packaging_overhead} "
        f"projected_minimum={projected_minimum} floor={PRODUCTION_FLOOR_BYTES}",
        flush=True,
    )
    if projected_minimum < PRODUCTION_FLOOR_BYTES:
        raise VBBRefreshError("projected disk free is below production floor")
    return result


def _replace_vbb_tree(stop_data: Path) -> None:
    vbb_root = stop_data / "transit" / "vbb"
    if vbb_root.exists() or vbb_root.is_symlink():
        shutil.rmtree(vbb_root)


def _refresh_metadata(
    candidate: Path,
    base_release_id: str,
    candidate_id: str,
    vbb_digest: str,
    vbb_size: int,
    vbb_url: str,
) -> None:
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    refresh = {
        "baseReleaseID": base_release_id,
        "candidateID": candidate_id,
        "refreshedProviders": ["vbb"],
        "generatedAt": generated_at,
        "vbb": {
            "sha256": vbb_digest,
            "size": vbb_size,
            "origin": vbb_url,
        },
    }
    metadata_path = candidate / "release-metadata.json"
    metadata = _json_object(metadata_path)
    metadata["baseReleaseID"] = base_release_id
    metadata["candidateID"] = candidate_id
    metadata["refreshedProviders"] = ["vbb"]
    metadata["generatedAt"] = generated_at
    metadata["scopedRefresh"] = refresh
    _write_copy_on_write(metadata_path, metadata)

    manifest_path = candidate / "stop-data" / "manifest.json"
    manifest = _json_object(manifest_path)
    manifest["baseReleaseID"] = base_release_id
    manifest["candidateID"] = candidate_id
    manifest["refreshedProviders"] = ["vbb"]
    manifest["generatedAt"] = generated_at
    manifest["scopedRefresh"] = refresh
    _write_copy_on_write(manifest_path, manifest)


def _validate_vbb_packages(
    stop_data: Path,
    now: datetime | None = None,
) -> dict[str, int | str]:
    berlin = stop_data / "transit" / "vbb" / "berlin"
    if not berlin.is_dir():
        raise VBBRefreshError("VBB Berlin package directory is missing")
    files = sorted(berlin.glob("*.json"))
    if not files:
        raise VBBRefreshError("VBB package directory is empty")
    current = now or datetime.now(ZoneInfo("Europe/Berlin"))
    keys = [
        (current - timedelta(hours=1)).strftime("%Y%m%d-%H"),
        current.strftime("%Y%m%d-%H"),
    ]
    for key in keys:
        path = berlin / f"{key}.json"
        if not path.is_file() or path.stat().st_size == 0:
            raise VBBRefreshError(f"required VBB package is missing: {path.name}")
        payload = _json_object(path)
        if not isinstance(payload.get("stops"), list) or not isinstance(payload.get("trips"), list):
            raise VBBRefreshError(f"invalid VBB package payload: {path.name}")
    return {
        "fileCount": len(files),
        "currentHour": keys[1],
        "previousHour": keys[0],
        "currentBytes": (berlin / f"{keys[1]}.json").stat().st_size,
        "previousBytes": (berlin / f"{keys[0]}.json").stat().st_size,
    }


def build_candidate(
    data_root: Path,
    repository_root: Path,
    *,
    vbb_url: str = DEFAULT_VBB_URL,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    run_readiness_check: bool = True,
) -> tuple[Path, dict[str, object]]:
    base, base_release_id = _base_release(data_root)
    cached_vbb = cache_root / "vbb" / "current.zip"
    expected_output = max(
        4 * 1024**3,
        3 * cached_vbb.stat().st_size if cached_vbb.is_file() else 0,
    )
    disk = _disk_preflight(data_root, expected_output)
    vbb_archive, vbb_digest, vbb_size = _resolve_vbb_artifact(cache_root, vbb_url)
    candidate_id = (
        f"vbb-refresh-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        f"-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    releases_root = data_root / "releases"
    staging = releases_root / f".{candidate_id}.staging"
    final = releases_root / candidate_id
    staging.mkdir(parents=True, exist_ok=False)
    try:
        print(
            f"[VBBRefresh] base={base} baseReleaseID={base_release_id} "
            f"vbbSHA={vbb_digest} vbbBytes={vbb_size}",
            flush=True,
        )
        _hardlink_tree(base, staging)
        _replace_vbb_tree(staging / "stop-data")
        with zipfile.ZipFile(vbb_archive) as archive:
            build_vbb_network_indexes(
                archive,
                staging / "stop-data",
                load_cities(repository_root / "config" / "cities.json"),
            )
        _refresh_metadata(
            staging,
            base_release_id,
            candidate_id,
            vbb_digest,
            vbb_size,
            vbb_url,
        )
        vbb_report = _validate_vbb_packages(staging / "stop-data")
        validate_release(staging)
        if run_readiness_check:
            environment = dict(os.environ)
            run_readiness(
                repository_root,
                environment,
                base_release_id,
                staged_release=staging,
            )
        os.replace(staging, final)
        validate_release(final)
        final_vbb_report = _validate_vbb_packages(final / "stop-data")
        usage = shutil.disk_usage(data_root)
        report = {
            "candidateID": candidate_id,
            "candidatePath": str(final),
            "baseReleaseID": base_release_id,
            "vbb": {
                "sha256": vbb_digest,
                "size": vbb_size,
                "files": final_vbb_report["fileCount"],
                "currentHour": final_vbb_report["currentHour"],
                "previousHour": final_vbb_report["previousHour"],
            },
            "disk": {**disk, "free_after": usage.free},
            "readiness": "PASS" if run_readiness_check else "NOT_RUN",
        }
        print(f"[VBBRefresh] candidate={final} validation=PASS", flush=True)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        return final, report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _relative_target(data_root: Path, candidate: Path) -> str:
    return os.path.relpath(candidate, data_root)


def _link_target(path: Path) -> str | None:
    return os.readlink(path) if path.is_symlink() else None


def _replace_link(path: Path, target: str | None) -> None:
    temporary = path.with_name(f".{path.name}.vbb-refresh-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    if target is None:
        path.unlink(missing_ok=True)
        return
    temporary.symlink_to(target)
    os.replace(temporary, path)


def activate_candidate(data_root: Path, repository_root: Path, candidate: Path) -> None:
    candidate = candidate.resolve()
    releases_root = (data_root / "releases").resolve()
    if releases_root not in candidate.parents or not candidate.is_dir():
        raise VBBRefreshError("candidate is outside releases root")
    metadata = _json_object(candidate / "release-metadata.json")
    base_release_id = metadata.get("releaseID")
    if not isinstance(base_release_id, str) or not base_release_id:
        raise VBBRefreshError("candidate has no compatible releaseID")
    validate_release(candidate)
    previous = {
        data_root / "current-release": _link_target(data_root / "current-release"),
        data_root / "current": _link_target(data_root / "current"),
        data_root / "departures-current.sqlite": _link_target(
            data_root / "departures-current.sqlite"
        ),
    }
    target = _relative_target(data_root, candidate)
    try:
        _replace_link(data_root / "current-release", target)
        _replace_link(data_root / "current", f"{target}/stop-data")
        _replace_link(data_root / "departures-current.sqlite", f"{target}/departures.sqlite")
        environment = dict(os.environ)
        environment["STATIC_DATA_ROOT"] = "/data/current-release/stop-data"
        environment["DEPARTURES_DATABASE"] = "/data/current-release/departures.sqlite"
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(repository_root / "deploy" / "static-departures.compose.yml"),
                "up",
                "-d",
                "--no-build",
                "--force-recreate",
            ],
            cwd=repository_root,
            env=environment,
            check=True,
        )
        deadline = time.monotonic() + int(environment.get("HEALTH_TIMEOUT_SECONDS", "60"))
        container = environment.get("STATIC_DEPARTURES_CONTAINER_NAME", "static-departures-api")
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    "docker", "exec", container, "python3", "-c",
                    "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/static-departures/health', timeout=5).read().decode())",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                health = json.loads(result.stdout)
                if health.get("database", {}).get("releaseID") == base_release_id:
                    print(f"[VBBRefresh] activation=PASS candidate={candidate}", flush=True)
                    return
            time.sleep(2)
        raise VBBRefreshError("production container did not become healthy for candidate")
    except Exception:
        for path, link_target in previous.items():
            _replace_link(path, link_target)
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(repository_root / "deploy" / "static-departures.compose.yml"),
                "up",
                "-d",
                "--no-build",
                "--force-recreate",
            ],
            cwd=repository_root,
            env=dict(os.environ),
            check=False,
        )
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stage-only", action="store_true", help="build and validate without activation")
    mode.add_argument("--activate", type=Path, metavar="CANDIDATE", help="activate a validated candidate")
    parser.add_argument("--data-root", type=Path, default=Path("/srv/haltewecker/data"))
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--vbb-url", default=DEFAULT_VBB_URL)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.activate is not None:
            activate_candidate(args.data_root, args.repository_root, args.activate)
        else:
            build_candidate(
                args.data_root,
                args.repository_root,
                vbb_url=args.vbb_url,
                cache_root=args.cache_root,
                run_readiness_check=True,
            )
        return 0
    except (VBBRefreshError, OSError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(f"[VBBRefresh] ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
