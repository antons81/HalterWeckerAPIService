"""Assemble and atomically publish immutable multi-provider releases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import shutil
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from services.static_departures_runtime import (  # noqa: E402
    ReleaseSnapshot,
    load_release_manifest,
)

try:
    from .artifact_provenance import artifact_provenance
    from .artifact_trust import trusted_artifact
except ImportError:
    from artifact_provenance import artifact_provenance
    from artifact_trust import trusted_artifact


RELEASE_FORMAT_VERSION = 2
RUNTIME_SCHEMA_VERSION = 1
RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ReleaseAssemblyError(ValueError):
    """Raised when an immutable release cannot be assembled or validated."""


@dataclass(frozen=True)
class ReleaseAssembly:
    release_id: str
    release_directory: Path
    manifest_path: Path
    provider_ids: tuple[str, ...]
    reused_artifacts: int


def _rss_bytes() -> int:
    try:
        with Path("/proc/self/status").open(encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _io_bytes() -> tuple[int, int]:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/self/io").read_text(encoding="ascii").splitlines():
            key, value = line.split(":", 1)
            if key in {"read_bytes", "write_bytes"}:
                values[key] = int(value.strip())
        return values.get("read_bytes", 0), values.get("write_bytes", 0)
    except (OSError, ValueError):
        return 0, 0


class _AssemblyProfiler:
    def __init__(self) -> None:
        self.enabled = os.environ.get(
            "HALTEWECKER_RELEASE_ASSEMBLY_INSTRUMENTATION", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}

    def _emit(
        self,
        stage: str,
        *,
        duration: float,
        files: int,
        bytes_scanned: int,
        bytes_written: int,
        read_bytes: int,
        write_bytes: int,
        status: str = "OK",
    ) -> None:
        if not self.enabled:
            return
        print(
            "[ReleaseAssembler] "
            f"stage={stage} status={status} duration={duration:.6f}s "
            f"files={files} bytes_scanned={bytes_scanned} bytes_written={bytes_written} "
            f"rss={_rss_bytes()} read_bytes={read_bytes} write_bytes={write_bytes}",
            flush=True,
        )

    @contextmanager
    def stage(
        self,
        name: str,
        *,
        files: int = 0,
        bytes_scanned: int = 0,
        bytes_written: int = 0,
    ):
        started = time.monotonic()
        before_read, before_write = _io_bytes()
        try:
            yield
        except Exception:
            after_read, after_write = _io_bytes()
            self._emit(
                name,
                duration=time.monotonic() - started,
                files=files,
                bytes_scanned=bytes_scanned,
                bytes_written=bytes_written,
                read_bytes=max(0, after_read - before_read),
                write_bytes=max(0, after_write - before_write),
                status="ERROR",
            )
            raise
        after_read, after_write = _io_bytes()
        self._emit(
            name,
            duration=time.monotonic() - started,
            files=files,
            bytes_scanned=bytes_scanned,
            bytes_written=bytes_written,
            read_bytes=max(0, after_read - before_read),
            write_bytes=max(0, after_write - before_write),
        )

    def observed(
        self,
        stage: str,
        _path: Path,
        bytes_scanned: int,
        duration: float,
    ) -> None:
        self._emit(
            stage,
            duration=duration,
            files=1,
            bytes_scanned=bytes_scanned,
            bytes_written=0,
            read_bytes=bytes_scanned,
            write_bytes=0,
        )


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonical(item) for item in value]
    return value


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseAssemblyError(f"invalid JSON: {path}") from error
    if not isinstance(payload, dict):
        raise ReleaseAssemblyError(f"JSON object expected: {path}")
    return payload


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tree_files(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    ]


def _tree_directories(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
        if path.is_dir() and not path.is_symlink()
    ]


def _fsync_files(root: Path) -> None:
    for path in _tree_files(root):
        with path.open("rb") as source:
            os.fsync(source.fileno())


def _fsync_directories(root: Path) -> None:
    for path in _tree_directories(root):
        _fsync_directory(path)
    _fsync_directory(root)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _validate_release_id(release_id: str) -> None:
    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise ReleaseAssemblyError(f"invalid release ID: {release_id!r}")


def _artifact_source(
    value: object,
    *,
    provider_id: str,
    artifact_type: str,
    profiler: _AssemblyProfiler | None = None,
) -> tuple[Path, Path, dict[str, object]]:
    if isinstance(value, Mapping):
        raw_database = value.get("path") or value.get("databasePath")
        raw_manifest = value.get("manifestPath")
    else:
        raw_database = value
        raw_manifest = None
    if not raw_database:
        raise ReleaseAssemblyError(f"provider={provider_id} {artifact_type} database is missing")
    database = Path(str(raw_database)).resolve()
    if database.is_dir():
        database = database / "provider.sqlite"
    manifest = Path(str(raw_manifest)).resolve() if raw_manifest else database.parent / "manifest.json"
    if not database.is_file() or not manifest.is_file():
        raise ReleaseAssemblyError(
            f"provider={provider_id} {artifact_type} source is incomplete: {database}"
        )
    manifest_stage = (
        profiler.stage("provider-manifest-validation", files=1)
        if profiler is not None
        else nullcontext()
    )
    with manifest_stage:
        payload = _read_object(manifest)
    if payload.get("status") != "complete":
        raise ReleaseAssemblyError(f"provider={provider_id} {artifact_type} is not complete")
    if payload.get("providerID") != provider_id or payload.get("artifactType") != artifact_type:
        raise ReleaseAssemblyError(f"provider={provider_id} {artifact_type} manifest identity mismatch")
    expected_key = str(payload.get("artifactKey") or "")
    if not expected_key:
        raise ReleaseAssemblyError(f"provider={provider_id} {artifact_type} artifact key is missing")
    expected_schema = payload.get(
        "structuralSchemaVersion" if artifact_type == "structural" else "temporalSchemaVersion"
    )
    if not isinstance(expected_schema, int):
        raise ReleaseAssemblyError(f"provider={provider_id} {artifact_type} schema version is missing")
    sqlite_payload = payload.get("sqlite")
    if not isinstance(sqlite_payload, Mapping):
        raise ReleaseAssemblyError(f"provider={provider_id} {artifact_type} SQLite provenance is missing")
    if trusted_artifact(
        database_path=database,
        manifest_path=manifest,
        manifest=payload,
    ):
        digest = str(sqlite_payload.get("sha256") or "")
        size = int(sqlite_payload.get("size") or 0)
    else:
        hash_stage = (
            profiler.stage(
                "provider-file-hash-validation",
                files=1,
                bytes_scanned=database.stat().st_size,
            )
            if profiler is not None
            else nullcontext()
        )
        with hash_stage:
            digest, size = artifact_provenance(database)
        if digest != sqlite_payload.get("sha256") or size != sqlite_payload.get("size"):
            raise ReleaseAssemblyError(f"provider={provider_id} {artifact_type} SQLite provenance mismatch")
    return database, manifest, {
        "artifactKey": expected_key,
        "sha256": digest,
        "size": size,
        "schemaVersion": expected_schema,
        "validFrom": payload.get("dependencies", {}).get("validFrom") if artifact_type == "temporal" and isinstance(payload.get("dependencies"), Mapping) else None,
        "validThrough": payload.get("dependencies", {}).get("validThrough") if artifact_type == "temporal" and isinstance(payload.get("dependencies"), Mapping) else None,
    }


def _link_reference(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(source), destination)


def _provider_entry(provider_id: str, value: Mapping[str, object]) -> dict[str, object]:
    cities = value.get("cities", ())
    if not isinstance(cities, (list, tuple, set, frozenset)):
        raise ReleaseAssemblyError(f"provider={provider_id} cities must be a list")
    normalized_cities = sorted({str(city).strip() for city in cities if str(city).strip()})
    if not normalized_cities:
        raise ReleaseAssemblyError(f"provider={provider_id} has no cities")
    status = str(value.get("status", "active")).strip() or "active"
    if status not in {"active", "optional", "retired"}:
        raise ReleaseAssemblyError(f"provider={provider_id} has invalid status: {status}")
    return {
        "providerID": provider_id,
        "cities": normalized_cities,
        "status": status,
        "required": bool(value.get("required", status == "active")),
        "providerOrder": int(value.get("providerOrder", 0)),
        "mergeGroup": str(value.get("mergeGroup", "")),
    }


def _stop_data_reference(root: Path) -> dict[str, object]:
    manifest_path = root / "manifest.json"
    if not root.is_dir() or not manifest_path.is_file():
        raise ReleaseAssemblyError(f"stop-data root is incomplete: {root}")
    manifest = _read_object(manifest_path)
    digest, size = artifact_provenance(root)
    return {
        "path": "stop-data",
        "manifestPath": "stop-data/manifest.json",
        "sha256": digest,
        "size": size,
        "releaseID": manifest.get("releaseID"),
        "version": manifest.get("version"),
    }


def _provider_set_fingerprint(providers: Mapping[str, Mapping[str, object]]) -> str:
    return _fingerprint(
        [
            {
                "providerID": provider_id,
                "cities": entry["cities"],
                "status": entry["status"],
                "required": entry["required"],
                "providerOrder": entry["providerOrder"],
                "mergeGroup": entry["mergeGroup"],
                "structuralArtifactKey": entry["structural"]["artifactKey"],
                "structuralSchemaVersion": entry["structural"]["schemaVersion"],
                "temporalArtifactKey": entry["temporal"]["artifactKey"],
                "temporalSchemaVersion": entry["temporal"]["schemaVersion"],
                "temporalValidFrom": entry["temporal"].get("validFrom"),
                "temporalValidThrough": entry["temporal"].get("validThrough"),
            }
            for provider_id, entry in sorted(providers.items())
        ]
    )


def validate_candidate_release(
    release_directory: Path | str,
    *,
    profiler: _AssemblyProfiler | None = None,
) -> dict[str, object]:
    """Validate a candidate before it can become the authoritative pointer."""
    root = Path(release_directory).resolve()
    manifest_path = root / "release.json"
    payload = _read_object(manifest_path)
    if payload.get("formatVersion") != RELEASE_FORMAT_VERSION:
        raise ReleaseAssemblyError("release manifest format version is unsupported")
    release_id = str(payload.get("releaseID") or "")
    _validate_release_id(release_id)
    created_at = payload.get("createdAt")
    if not isinstance(created_at, str):
        raise ReleaseAssemblyError("release createdAt is missing")
    try:
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseAssemblyError("release createdAt is invalid") from error
    if parsed_created_at.tzinfo is None:
        raise ReleaseAssemblyError("release createdAt must include a timezone")
    compatibility = payload.get("compatibility")
    if not isinstance(compatibility, Mapping) or compatibility.get("runtimeSchemaVersion") != RUNTIME_SCHEMA_VERSION:
        raise ReleaseAssemblyError("release runtime compatibility is missing or unsupported")
    provider_payload = payload.get("providers")
    if not isinstance(provider_payload, Mapping) or not provider_payload:
        raise ReleaseAssemblyError("release provider registry is missing")
    for provider_id, value in provider_payload.items():
        if not isinstance(value, Mapping):
            raise ReleaseAssemblyError(f"provider={provider_id} entry is invalid")
    provider_ids = tuple(
        provider_id
        for provider_id, value in sorted(
            provider_payload.items(),
            key=lambda item: (int(item[1].get("providerOrder", 0)) if isinstance(item[1], Mapping) else 0, str(item[0])),
        )
    )
    normalized_providers: dict[str, dict[str, object]] = {}
    for provider_id in provider_ids:
        value = provider_payload[provider_id]
        if not isinstance(value, Mapping):
            raise ReleaseAssemblyError(f"provider={provider_id} entry is invalid")
        normalized = _provider_entry(provider_id, value)
        for artifact_type in ("structural", "temporal"):
            reference = value.get(artifact_type)
            if not isinstance(reference, Mapping):
                raise ReleaseAssemblyError(f"provider={provider_id} {artifact_type} reference is missing")
            required_fields = ("path", "manifestPath", "artifactKey", "sha256", "size", "schemaVersion")
            if artifact_type == "temporal":
                required_fields += ("validFrom", "validThrough")
            for required in required_fields:
                if required not in reference:
                    raise ReleaseAssemblyError(f"provider={provider_id} {artifact_type} reference lacks {required}")
            normalized[artifact_type] = dict(reference)
        normalized_providers[provider_id] = normalized
    if payload.get("providerSetFingerprint") != _provider_set_fingerprint(normalized_providers):
        raise ReleaseAssemblyError("provider-set fingerprint mismatch")
    common = payload.get("common")
    if not isinstance(common, Mapping) or not isinstance(common.get("catalogFingerprint"), str):
        raise ReleaseAssemblyError("common catalog fingerprint is missing")
    stop_data = payload.get("stopData")
    if not isinstance(stop_data, Mapping) or not all(
        key in stop_data for key in ("path", "manifestPath", "sha256", "size")
    ):
        raise ReleaseAssemblyError("stop-data provenance is missing")

    common_path = root / str(common["path"])
    if not common_path.is_file():
        raise ReleaseAssemblyError(f"common catalog is missing: {common_path}")
    try:
        import sqlite3

        with sqlite3.connect(f"file:{common_path}?mode=ro", uri=True) as connection:
            common_registry = {
                (str(row[0]), str(row[1]))
                for row in connection.execute("SELECT provider_id, status FROM provider_registry")
            }
    except sqlite3.Error as error:
        raise ReleaseAssemblyError(f"common provider registry is unreadable: {common_path}") from error
    common_provider_ids = {provider_id for provider_id, _status in common_registry}
    if not common_provider_ids.issuperset(provider_ids):
        unknown = sorted(set(provider_ids) - common_provider_ids)
        raise ReleaseAssemblyError(f"release contains providers absent from common catalog: {unknown}")
    required_provider_ids = {
        provider_id for provider_id, status in common_registry if status == "active"
    }
    missing_required = sorted(required_provider_ids - set(provider_ids))
    if missing_required:
        raise ReleaseAssemblyError(f"release is missing required providers: {missing_required}")

    try:
        load_release_manifest(
            root,
            provider_ids=provider_ids,
            validation_observer=profiler.observed if profiler is not None else None,
        )
    except Exception as error:
        raise ReleaseAssemblyError(f"runtime release validation failed: {error}") from error

    common_metadata = _read_object(root / "common-metadata.json") if (root / "common-metadata.json").is_file() else {}
    if common_metadata and common_metadata.get("inputFingerprint") != common.get("catalogFingerprint"):
        raise ReleaseAssemblyError("common catalog fingerprint does not match common metadata")
    stop_root = root / str(stop_data["path"])
    stop_stage = (
        profiler.stage("stop-data-validation", bytes_scanned=stop_data["size"])
        if profiler is not None
        else nullcontext()
    )
    with stop_stage:
        actual_stop_digest, actual_stop_size = artifact_provenance(stop_root)
    if actual_stop_digest != stop_data["sha256"] or actual_stop_size != stop_data["size"]:
        raise ReleaseAssemblyError("stop-data provenance mismatch")
    common_stage = (
        profiler.stage("common-catalog-validation", bytes_scanned=common.get("size", 0))
        if profiler is not None
        else nullcontext()
    )
    with common_stage:
        actual_common_digest, actual_common_size = artifact_provenance(common_path)
    if actual_common_digest != common.get("sha256") or actual_common_size != common.get("size"):
        raise ReleaseAssemblyError("common catalog provenance mismatch")
    return payload


def assemble_release(
    releases_root: Path | str,
    release_id: str,
    *,
    common_database: Path | str,
    stop_data_root: Path | str,
    providers: Mapping[str, Mapping[str, object]],
    created_at: str | None = None,
) -> ReleaseAssembly:
    """Build, validate, fsync, and rename one immutable release directory."""
    _validate_release_id(release_id)
    releases = Path(releases_root).resolve()
    releases.mkdir(parents=True, exist_ok=True)
    final = releases / release_id
    if final.exists() or final.is_symlink():
        raise ReleaseAssemblyError(f"release already exists: {final}")
    common_source = Path(common_database).resolve()
    stop_source = Path(stop_data_root).resolve()
    if not common_source.is_file():
        raise ReleaseAssemblyError(f"common database is missing: {common_source}")
    normalized: dict[str, dict[str, object]] = {
        provider_id: _provider_entry(provider_id, value)
        for provider_id, value in providers.items()
    }
    if not normalized:
        raise ReleaseAssemblyError("release has no providers")
    staging = releases / f".{release_id}.staging-{uuid.uuid4().hex}"
    reused = 0
    profiler = _AssemblyProfiler()
    total_started = time.monotonic()
    total_read, total_write = _io_bytes()
    try:
        with profiler.stage("release-prepare"):
            staging.mkdir()
        with profiler.stage(
            "common-catalog-build",
            files=1,
            bytes_written=common_source.stat().st_size,
        ):
            shutil.copy2(common_source, staging / "common.sqlite")
            common_digest, common_size = artifact_provenance(staging / "common.sqlite")
        with profiler.stage("stop-data-reference"):
            _link_reference(stop_source, staging / "stop-data")
            stop_data = _stop_data_reference(staging / "stop-data")
        common_metadata_path = staging / "common-metadata.json"
        with __import__("sqlite3").connect(staging / "common.sqlite") as connection:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        catalog_fingerprint = metadata.get("inputFingerprint")
        if not isinstance(catalog_fingerprint, str) or not catalog_fingerprint:
            raise ReleaseAssemblyError("common catalog inputFingerprint is missing")
        _write_json_atomic(common_metadata_path, {"inputFingerprint": catalog_fingerprint})
        provider_payload: dict[str, dict[str, object]] = {}
        for provider_id, entry in normalized.items():
            output_entry = dict(entry)
            with profiler.stage("reference/symlink-assembly"):
                for artifact_type in ("structural", "temporal"):
                    source_db, source_manifest, info = _artifact_source(
                        providers[provider_id].get(artifact_type),
                        provider_id=provider_id,
                        artifact_type=artifact_type,
                        profiler=profiler,
                    )
                    relative_dir = Path("providers") / provider_id / artifact_type
                    destination_db = staging / relative_dir / "provider.sqlite"
                    destination_manifest = staging / relative_dir / "manifest.json"
                    _link_reference(source_db, destination_db)
                    _link_reference(source_manifest, destination_manifest)
                    output_entry[artifact_type] = {
                        "path": (relative_dir / "provider.sqlite").as_posix(),
                        "manifestPath": (relative_dir / "manifest.json").as_posix(),
                        "artifactKey": info["artifactKey"],
                        "sha256": info["sha256"],
                        "size": info["size"],
                        "schemaVersion": info["schemaVersion"],
                        "validFrom": info["validFrom"],
                        "validThrough": info["validThrough"],
                        "storage": "immutable-external-reference",
                        "sourcePath": str(source_db),
                    }
                    reused += 1
            provider_payload[provider_id] = output_entry
        with profiler.stage("release-json-build"):
            created = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            manifest = {
                "formatVersion": RELEASE_FORMAT_VERSION,
                "releaseID": release_id,
                "createdAt": created,
                "compatibility": {"runtimeSchemaVersion": RUNTIME_SCHEMA_VERSION},
                "common": {
                    "path": "common.sqlite",
                    "sha256": common_digest,
                    "size": common_size,
                    "schemaVersion": metadata.get("commonSchemaVersion"),
                    "catalogFingerprint": catalog_fingerprint,
                },
                "stopData": stop_data,
                "providerSetFingerprint": _provider_set_fingerprint(provider_payload),
                "providers": provider_payload,
            }
            _write_json_atomic(staging / "release.json", manifest)
        with profiler.stage("release-json-validation"):
            validate_candidate_release(staging, profiler=profiler)
        files = _tree_files(staging)
        with profiler.stage(
            "fsync-files",
            files=len(files),
            bytes_scanned=sum(path.stat().st_size for path in files),
        ):
            _fsync_files(staging)
        directories = _tree_directories(staging)
        with profiler.stage("fsync-directories", files=len(directories) + 1):
            _fsync_directories(staging)
        with profiler.stage("final-rename"):
            os.replace(staging, final)
            _fsync_directory(releases)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    finally:
        after_read, after_write = _io_bytes()
        profiler._emit(
            "total",
            duration=time.monotonic() - total_started,
            files=0,
            bytes_scanned=0,
            bytes_written=0,
            read_bytes=max(0, after_read - total_read),
            write_bytes=max(0, after_write - total_write),
        )
    return ReleaseAssembly(
        release_id=release_id,
        release_directory=final,
        manifest_path=final / "release.json",
        provider_ids=tuple(provider_payload),
        reused_artifacts=reused,
    )


def atomic_switch_current_release(
    current_pointer: Path | str,
    release_directory: Path | str,
) -> Path:
    """Atomically switch the only authoritative multi-shard generation pointer."""
    pointer_value = Path(current_pointer)
    pointer = pointer_value.parent.resolve() / pointer_value.name
    target = Path(release_directory).resolve()
    validate_candidate_release(target)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    relative_target = os.path.relpath(target, pointer.parent)
    temporary = pointer.parent / f".{pointer.name}.tmp-{uuid.uuid4().hex}"
    os.symlink(relative_target, temporary)
    try:
        _fsync_directory(pointer.parent)
        os.replace(temporary, pointer)
        _fsync_directory(pointer.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def readiness_probe(
    release_directory: Path | str,
    *,
    provider_ids: tuple[str, ...],
    israel_case: Mapping[str, object],
    toronto_case: Mapping[str, object],
    trip_case: Mapping[str, object],
) -> dict[str, object]:
    profiler = _AssemblyProfiler()
    with profiler.stage("readiness"):
        return _readiness_probe(
            release_directory,
            provider_ids=provider_ids,
            israel_case=israel_case,
            toronto_case=toronto_case,
            trip_case=trip_case,
        )


def _readiness_probe(
    release_directory: Path | str,
    *,
    provider_ids: tuple[str, ...],
    israel_case: Mapping[str, object],
    toronto_case: Mapping[str, object],
    trip_case: Mapping[str, object],
) -> dict[str, object]:
    """Run the local candidate probe through the same runtime abstraction."""
    def case_datetime(case: Mapping[str, object], key: str) -> datetime | None:
        value = case.get(key)
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return datetime.fromisoformat(value)
            except ValueError as error:
                raise ReleaseAssemblyError(f"readiness case {key} is invalid") from error
        return None

    with ReleaseSnapshot.open(release_directory, provider_ids=provider_ids) as snapshot:
        metadata = snapshot.catalog.metadata()
        israel_provider = str(israel_case.get("providerID") or "")
        israel_city = str(israel_case.get("cityID") or "")
        israel_stop = str(israel_case.get("stopID") or "")
        if not israel_provider or not israel_city or not israel_stop:
            raise ReleaseAssemblyError("Israel readiness case is incomplete")
        israel_runtime = snapshot.provider(israel_provider)
        israel_lines = israel_runtime.lines(israel_city, israel_stop)
        israel_board = israel_runtime.board(
            israel_city,
            israel_stop,
            int(israel_case.get("limit", 1)),
            case_datetime(israel_case, "fromDate"),
            case_datetime(israel_case, "toDate"),
        )

        toronto_city = str(toronto_case.get("cityID") or "")
        toronto_stop = str(toronto_case.get("stopID") or "")
        routed_providers = snapshot.catalog.providers_for_city(toronto_city)
        if len(routed_providers) < 2:
            raise ReleaseAssemblyError("Toronto readiness case does not fan out to multiple providers")
        toronto_lines = snapshot.lines(toronto_city, toronto_stop)
        toronto_board = snapshot.board(
            toronto_city,
            toronto_stop,
            int(toronto_case.get("limit", 1)),
            case_datetime(toronto_case, "fromDate"),
            case_datetime(toronto_case, "toDate"),
        )

        trip_provider = str(trip_case.get("providerID") or israel_provider)
        trip_city = str(trip_case.get("cityID") or israel_city)
        trip_id = str(trip_case.get("tripID") or "")
        if not trip_id:
            raise ReleaseAssemblyError("trip readiness case is incomplete")
        trip_details = snapshot.provider(trip_provider).trip_details(
            trip_city,
            trip_id,
            str(trip_case.get("staticRoot") or snapshot.stop_data_root),
            str(trip_case.get("serviceDate")) if trip_case.get("serviceDate") else None,
        )
        if trip_details is None:
            raise ReleaseAssemblyError("trip readiness query returned no details")
        return {
            "status": "READY",
            "releaseID": snapshot.release_id,
            "commonMetadata": metadata,
            "israel": {
                "providerCount": 1,
                "lines": len(israel_lines),
                "board": len(israel_board),
            },
            "toronto": {
                "providerIDs": routed_providers,
                "lines": len(toronto_lines),
                "board": len(toronto_board),
            },
            "tripDetails": {"providerID": trip_provider, "tripID": trip_id},
        }


__all__ = [
    "RELEASE_FORMAT_VERSION",
    "RUNTIME_SCHEMA_VERSION",
    "ReleaseAssembly",
    "ReleaseAssemblyError",
    "assemble_release",
    "atomic_switch_current_release",
    "readiness_probe",
    "validate_candidate_release",
]
