"""Release-coherent provider-local runtime for shadow static departures queries."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import resource
import sqlite3
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

try:
    from provider_artifact_capabilities import (
        HYBRID_RUNTIME,
        SHARD_RUNTIME,
        provider_capability,
    )
except ImportError:
    from scripts.provider_artifact_capabilities import (
        HYBRID_RUNTIME,
        SHARD_RUNTIME,
        provider_capability,
    )

try:
    from artifact_trust import trusted_artifact
except ImportError:
    from scripts.artifact_trust import trusted_artifact


LOGGER = logging.getLogger("haltewecker.static_departures_runtime")
ISRAEL_PROVIDER_ID = "israel-mot"
SHADOW_ENV = "HALTEWECKER_STATIC_DEPARTURES_SHADOW_PROVIDER_RUNTIME"
SHADOW_RELEASE_ENV = "HALTEWECKER_STATIC_DEPARTURES_SHADOW_RELEASE"
SHADOW_PROVIDER_ENV = "HALTEWECKER_STATIC_DEPARTURES_SHADOW_PROVIDER"
SHADOW_MAX_PARALLEL_ENV = "HALTEWECKER_STATIC_DEPARTURES_SHADOW_MAX_PARALLEL_PROVIDER_QUERIES"
HYBRID_ENV = "HALTEWECKER_STATIC_DEPARTURES_HYBRID_RUNTIME"
HYBRID_PROVIDER_ENV = "HALTEWECKER_STATIC_DEPARTURES_HYBRID_PROVIDERS"
HYBRID_RELEASE_POINTER_ENV = "HALTEWECKER_STATIC_DEPARTURES_HYBRID_RELEASE_POINTER"
HYBRID_MAX_PARALLEL_ENV = "HALTEWECKER_STATIC_DEPARTURES_HYBRID_MAX_PARALLEL_PROVIDER_QUERIES"
HYBRID_COMPARE_ENV = "HALTEWECKER_STATIC_DEPARTURES_HYBRID_COMPARE_LEGACY"
DEFAULT_HYBRID_PROVIDERS = ("israel-mot", "ttc-surface", "ttc-subway")

GTFS_ROUTE_TYPE_TO_MODE = {
    "0": "tram",
    "1": "subway",
    "2": "train",
    "3": "bus",
    "4": "ferry",
    "5": "cableCar",
    "6": "gondola",
    "7": "funicular",
    "11": "trolleybus",
    "12": "monorail",
}

STRUCTURAL_TABLES = {
    "agencies",
    "raw_stops",
    "routes",
    "trips",
    "calendar",
    "calendar_dates",
    "stop_times",
    "transfers",
    "pathways",
    "provider_entities",
    "provider_city_stops",
    "provider_city_modes",
    "ownership_metadata",
}
TEMPORAL_TABLES = {"active_services"}
COMMON_TABLES = {
    "metadata",
    "provider_registry",
    "city_aliases",
    "city_stops",
    "provider_city_stops",
    "provider_city_modes",
}
STRUCTURAL_SCHEMA_VERSION = 1
TEMPORAL_SCHEMA_VERSION = 1
ValidationObserver = Callable[[str, Path, int, float], None]


class RuntimeUnavailable(RuntimeError):
    """The requested release or provider shard is not usable."""


@dataclass(frozen=True)
class ProviderMode:
    provider_id: str
    city_id: str
    mode: str
    timezone: str
    stop_id_prefix: str
    identifier_prefix: str


@dataclass(frozen=True)
class ArtifactReference:
    provider_id: str
    artifact_type: str
    database_path: Path
    manifest_path: Path
    artifact_key: str
    schema_version: int
    sha256: str
    size: int
    valid_from: str | None
    valid_through: str | None
    manifest: Mapping[str, object]


@dataclass(frozen=True)
class ProviderReferences:
    structural: ArtifactReference
    temporal: ArtifactReference


@dataclass(frozen=True)
class ReleaseManifest:
    release_id: str
    release_root: Path
    stop_data_root: Path
    common_database_path: Path
    common_sha256: str
    common_size: int
    providers: Mapping[str, ProviderReferences]
    payload: Mapping[str, object]


@dataclass(frozen=True)
class FanoutMetrics:
    query: str
    city_id: str
    provider_ids: tuple[str, ...]
    provider_count: int
    rows_fetched: int
    fanout_duration: float
    merge_duration: float


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


def _sha256_file(
    path: Path,
    *,
    observer: ValidationObserver | None = None,
    stage: str = "provider-file-hash-validation",
) -> tuple[str, int]:
    started = time.monotonic()
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    if observer is not None:
        observer(stage, path, size, time.monotonic() - started)
    return digest.hexdigest(), size


def _resolve_reference(root: Path, value: object, label: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise RuntimeUnavailable(f"{label} path is missing")
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RuntimeUnavailable(f"{label} path is unavailable: {path}") from error
    if not resolved.is_file():
        raise RuntimeUnavailable(f"{label} is not a file: {resolved}")
    return resolved


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeUnavailable(f"{label} manifest is invalid: {path}") from error
    if not isinstance(payload, dict):
        raise RuntimeUnavailable(f"{label} manifest must be an object: {path}")
    return payload


def _sqlite_tables(
    path: Path,
    *,
    observer: ValidationObserver | None = None,
    stage: str = "provider-sqlite-quick-check",
) -> set[str]:
    connection: sqlite3.Connection | None = None
    started = time.monotonic()
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check != ("ok",):
            raise RuntimeUnavailable(f"SQLite quick_check failed: {path}")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if observer is not None:
            observer(stage, path, int(path.stat().st_size), time.monotonic() - started)
        return tables
    except sqlite3.Error as error:
        raise RuntimeUnavailable(f"SQLite validation failed: {path}") from error
    finally:
        if connection is not None:
            connection.close()


def _validate_artifact(
    root: Path,
    provider_id: str,
    artifact_type: str,
    value: object,
    validation_observer: ValidationObserver | None = None,
) -> ArtifactReference:
    if not isinstance(value, dict):
        raise RuntimeUnavailable(
            f"provider={provider_id} {artifact_type} reference is missing"
        )
    database_path = _resolve_reference(
        root,
        value.get("path") or value.get("databasePath"),
        f"provider={provider_id} {artifact_type}",
    )
    manifest_reference = value.get("manifestPath")
    manifest_path = _resolve_reference(
        root,
        manifest_reference if manifest_reference else database_path.parent / "manifest.json",
        f"provider={provider_id} {artifact_type} manifest",
    )
    manifest = _read_object(manifest_path, f"provider={provider_id} {artifact_type}")
    if manifest.get("status") != "complete":
        raise RuntimeUnavailable(
            f"provider={provider_id} {artifact_type} is not complete"
        )
    if manifest.get("providerID") != provider_id:
        raise RuntimeUnavailable(
            f"provider={provider_id} {artifact_type} has wrong provider ID"
        )
    if manifest.get("artifactType") != artifact_type:
        raise RuntimeUnavailable(
            f"provider={provider_id} artifact type mismatch"
        )
    key = str(value.get("artifactKey") or manifest.get("artifactKey") or "")
    if not key or manifest.get("artifactKey") != key:
        raise RuntimeUnavailable(
            f"provider={provider_id} {artifact_type} artifact key mismatch"
        )
    schema_key = (
        "structuralSchemaVersion"
        if artifact_type == "structural"
        else "temporalSchemaVersion"
    )
    schema_version = manifest.get(schema_key)
    if not isinstance(schema_version, int):
        raise RuntimeUnavailable(
            f"provider={provider_id} {artifact_type} schema version is missing"
        )
    expected_schema_version = (
        STRUCTURAL_SCHEMA_VERSION
        if artifact_type == "structural"
        else TEMPORAL_SCHEMA_VERSION
    )
    if schema_version != expected_schema_version:
        raise RuntimeUnavailable(
            f"provider={provider_id} {artifact_type} schema version mismatch"
        )
    sqlite_provenance = manifest.get("sqlite")
    if not isinstance(sqlite_provenance, dict):
        raise RuntimeUnavailable(
            f"provider={provider_id} {artifact_type} SQLite provenance is missing"
        )
    expected_digest = str(value.get("sha256") or sqlite_provenance.get("sha256") or "")
    expected_size = int(value.get("size") or sqlite_provenance.get("size") or 0)
    trusted = trusted_artifact(
        database_path=database_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )
    LOGGER.info(
        "stage=artifact-validation provider=%s artifact_type=%s status=%s reason=%s",
        provider_id,
        artifact_type,
        "HIT" if trusted else "FULL",
        "trusted-reuse" if trusted else "full-revalidation",
    )
    if trusted:
        try:
            actual_size = database_path.stat().st_size
        except OSError as error:
            raise RuntimeUnavailable(
                f"provider={provider_id} {artifact_type} metadata is unavailable"
            ) from error
        if actual_size != expected_size:
            raise RuntimeUnavailable(f"provider={provider_id} {artifact_type} size mismatch")
        digest, size = expected_digest, expected_size
        if validation_observer is not None:
            validation_observer("artifact-trusted-reuse", database_path, 0, 0.0)
    else:
        digest, size = _sha256_file(
            database_path,
            observer=validation_observer,
            stage="provider-file-hash-validation",
        )
        if digest != expected_digest or size != expected_size:
            raise RuntimeUnavailable(
                f"provider={provider_id} {artifact_type} hash/size mismatch"
            )
    required_tables = STRUCTURAL_TABLES if artifact_type == "structural" else TEMPORAL_TABLES
    if trusted:
        missing = set()
    else:
        missing = required_tables - _sqlite_tables(
            database_path,
            observer=validation_observer,
            stage="provider-sqlite-quick-check",
        )
    if missing:
        raise RuntimeUnavailable(
            f"provider={provider_id} {artifact_type} missing tables: {sorted(missing)}"
        )
    dependencies = manifest.get("dependencies")
    valid_from = None
    valid_through = None
    if isinstance(dependencies, dict):
        valid_from = str(dependencies.get("validFrom")) if dependencies.get("validFrom") else None
        valid_through = str(dependencies.get("validThrough")) if dependencies.get("validThrough") else None
    return ArtifactReference(
        provider_id=provider_id,
        artifact_type=artifact_type,
        database_path=database_path,
        manifest_path=manifest_path,
        artifact_key=key,
        schema_version=schema_version,
        sha256=digest,
        size=size,
        valid_from=valid_from,
        valid_through=valid_through,
        manifest=manifest,
    )


def load_release_manifest(
    release_root: Path,
    *,
    provider_ids: tuple[str, ...] = (ISRAEL_PROVIDER_ID,),
    validation_observer: ValidationObserver | None = None,
) -> ReleaseManifest:
    """Load and validate a release without opening any mutable production pointer."""
    root = release_root.resolve()
    if not root.is_dir():
        raise RuntimeUnavailable(f"release root is unavailable: {root}")
    manifest_path = next(
        (candidate for candidate in (root / "release.json", root / "manifest.json") if candidate.is_file()),
        None,
    )
    if manifest_path is None:
        raise RuntimeUnavailable(f"release manifest is missing below {root}")
    payload = _read_object(manifest_path, "release")
    release_id = str(payload.get("releaseID") or "").strip()
    if not release_id:
        raise RuntimeUnavailable("releaseID is missing")
    common = payload.get("common")
    if not isinstance(common, dict):
        raise RuntimeUnavailable("common DB reference is missing")
    common_path = _resolve_reference(root, common.get("path"), "common DB")
    common_digest, common_size = _sha256_file(
        common_path,
        observer=validation_observer,
        stage="common-catalog-validation",
    )
    if common_digest != str(common.get("sha256") or "") or common_size != int(common.get("size") or 0):
        raise RuntimeUnavailable("common DB hash/size mismatch")
    stop_data = payload.get("stopData")
    stop_data_reference = stop_data.get("path") if isinstance(stop_data, dict) else stop_data
    stop_data_root = (root / str(stop_data_reference or "stop-data")).resolve()
    if not stop_data_root.is_dir() or not (stop_data_root / "manifest.json").is_file():
        raise RuntimeUnavailable("release stop-data root or manifest is missing")
    missing_common = COMMON_TABLES - _sqlite_tables(
        common_path,
        observer=validation_observer,
        stage="common-catalog-validation",
    )
    if missing_common:
        raise RuntimeUnavailable(f"common DB missing tables: {sorted(missing_common)}")
    common_connection = sqlite3.connect(f"file:{common_path}?mode=ro", uri=True)
    try:
        common_metadata = dict(common_connection.execute("SELECT key, value FROM metadata"))
    finally:
        common_connection.close()
    if common_metadata.get("releaseID") != release_id:
        raise RuntimeUnavailable("common DB releaseID does not match release manifest")
    with sqlite3.connect(f"file:{common_path}?mode=ro", uri=True) as connection:
        for provider_id in provider_ids:
            if connection.execute(
                "SELECT 1 FROM provider_city_modes WHERE provider_id=? LIMIT 1",
                (provider_id,),
            ).fetchone() is None:
                raise RuntimeUnavailable(
                    f"common DB has no city/provider routing for {provider_id}"
                )
    provider_payload = payload.get("providers")
    if not isinstance(provider_payload, dict):
        raise RuntimeUnavailable("provider registry is missing")
    providers: dict[str, ProviderReferences] = {}
    for provider_id in provider_ids:
        entry = provider_payload.get(provider_id)
        if not isinstance(entry, dict):
            raise RuntimeUnavailable(f"provider={provider_id} is missing from release")
        structural = _validate_artifact(
            root,
            provider_id,
            "structural",
            entry.get("structural"),
            validation_observer=validation_observer,
        )
        temporal = _validate_artifact(
            root,
            provider_id,
            "temporal",
            entry.get("temporal"),
            validation_observer=validation_observer,
        )
        structural_key = temporal.manifest.get("structuralArtifactKey")
        if structural_key != structural.artifact_key:
            raise RuntimeUnavailable(
                f"provider={provider_id} temporal/structural key mismatch"
            )
        providers[provider_id] = ProviderReferences(structural, temporal)
    with sqlite3.connect(f"file:{common_path}?mode=ro", uri=True) as connection:
        for provider_id, references in providers.items():
            row = connection.execute(
                """
                SELECT release_id, structural_artifact_key, structural_schema_version,
                       temporal_artifact_key, temporal_schema_version,
                       valid_from, valid_through
                FROM provider_registry WHERE provider_id=?
                """,
                (provider_id,),
            ).fetchone()
            if row is None:
                raise RuntimeUnavailable(f"common DB has no provider registry entry for {provider_id}")
            if str(row[0]) != release_id:
                raise RuntimeUnavailable(f"provider={provider_id} common release validity mismatch")
            if str(row[1]) != references.structural.artifact_key or int(row[2]) != references.structural.schema_version:
                raise RuntimeUnavailable(f"provider={provider_id} common structural reference mismatch")
            if str(row[3]) != references.temporal.artifact_key or int(row[4]) != references.temporal.schema_version:
                raise RuntimeUnavailable(f"provider={provider_id} common temporal reference mismatch")
            if str(row[5] or "") != str(references.temporal.valid_from or "") or str(row[6] or "") != str(references.temporal.valid_through or ""):
                raise RuntimeUnavailable(f"provider={provider_id} common temporal validity mismatch")
    return ReleaseManifest(
        release_id=release_id,
        release_root=root,
        stop_data_root=stop_data_root,
        common_database_path=common_path,
        common_sha256=common_digest,
        common_size=common_size,
        providers=providers,
        payload=payload,
    )


class CommonCatalog:
    """Read-only reader for the release-wide catalog boundary."""

    def __init__(self, connection: sqlite3.Connection, manifest: ReleaseManifest) -> None:
        self.connection = connection
        self.manifest = manifest
        self.lock = threading.RLock()

    def metadata(self) -> dict[str, str]:
        with self.lock:
            return dict(self.connection.execute("SELECT key, value FROM metadata"))

    def resolve_city(self, city_id: str) -> str:
        with self.lock:
            row = self.connection.execute(
                "SELECT canonical_city_id FROM city_aliases WHERE alias_city_id=?",
                (city_id,),
            ).fetchone()
        return str(row[0]) if row else city_id

    def city_has_stop(self, city_id: str, stop_id: str) -> bool:
        with self.lock:
            return self.connection.execute(
                "SELECT 1 FROM city_stops WHERE city_id=? AND stop_id=?",
                (city_id, stop_id),
            ).fetchone() is not None

    def city_stop_registry(self, city_id: str) -> set[str]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT stop_id FROM city_stops WHERE city_id=?",
                (city_id,),
            ).fetchall()
        return {str(row[0]) for row in rows if row[0] is not None}

    def providers_for_city(self, city_id: str) -> tuple[str, ...]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT provider_id FROM provider_city_modes
                WHERE city_id=?
                ORDER BY (SELECT provider_order FROM provider_registry WHERE provider_registry.provider_id=provider_city_modes.provider_id), provider_id
                """,
                (city_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def providers_for_stop(self, city_id: str, stop_id: str) -> tuple[str, ...]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT provider_id FROM provider_city_stops
                WHERE city_id=? AND stop_id=?
                ORDER BY (SELECT provider_order FROM provider_registry WHERE provider_registry.provider_id=provider_city_stops.provider_id), provider_id
                """,
                (city_id, stop_id),
            ).fetchall()
            if rows:
                return tuple(str(row[0]) for row in rows)
        return self.providers_for_city(city_id)

    def provider_mode(self, city_id: str, provider_id: str) -> ProviderMode:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT provider_id, city_id, mode, timezone, stop_id_prefix, identifier_prefix
                FROM provider_city_modes
                WHERE city_id=? AND provider_id=?
                """,
                (city_id, provider_id),
            ).fetchone()
        if row is None:
            raise RuntimeUnavailable(
                f"provider={provider_id} has no common city mapping for {city_id}"
            )
        return ProviderMode(*(str(value or "") for value in row))

    def provider_modes(self, city_id: str) -> tuple[ProviderMode, ...]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT provider_id, city_id, mode, timezone, stop_id_prefix, identifier_prefix
                FROM provider_city_modes
                WHERE city_id=? ORDER BY provider_id
                """,
                (city_id,),
            ).fetchall()
        return tuple(ProviderMode(*(str(value or "") for value in row)) for row in rows)

    def provider_registry(self) -> tuple[dict[str, object], ...]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT provider_id, status, provider_order, merge_group, release_id FROM provider_registry ORDER BY provider_order, provider_id"
            ).fetchall()
        return tuple(
            {
                "providerID": str(row[0]),
                "status": str(row[1]),
                "providerOrder": int(row[2]),
                "mergeGroup": str(row[3]),
                "releaseID": str(row[4]),
            }
            for row in rows
        )

    def provider_references(self, provider_id: str) -> ProviderReferences:
        try:
            return self.manifest.providers[provider_id]
        except KeyError as error:
            raise RuntimeUnavailable(
                f"provider={provider_id} has no validated artifact references"
            ) from error

    def close(self) -> None:
        with self.lock:
            self.connection.close()


class ProviderSnapshot:
    """One provider's structural DB with its temporal DB attached only locally."""

    def __init__(self, snapshot: "ReleaseSnapshot", provider_id: str) -> None:
        self.snapshot = snapshot
        self.provider_id = provider_id
        self.references = snapshot.manifest.providers[provider_id]
        self._connections: dict[int, sqlite3.Connection] = {}
        self.lock = threading.RLock()
        self.query_count = 0

    def _connection(self) -> sqlite3.Connection:
        thread_id = threading.get_ident()
        with self.lock:
            connection = self._connections.get(thread_id)
            if connection is None:
                connection = sqlite3.connect(
                    f"file:{self.references.structural.database_path}?mode=ro",
                    uri=True,
                    check_same_thread=False,
                )
                connection.execute("PRAGMA query_only=ON")
                connection.execute(
                    "ATTACH DATABASE ? AS temporal",
                    (f"file:{self.references.temporal.database_path}?mode=ro",),
                )
                connection.execute("PRAGMA temporal.query_only=ON")
                if self.snapshot.trace_queries:
                    connection.set_trace_callback(self._trace)
                self._connections[thread_id] = connection
                self.snapshot.connection_count += 1
                self.snapshot.peak_provider_connections = max(
                    self.snapshot.peak_provider_connections,
                    len(self.snapshot._active_provider_ids),
                )
            return connection

    def _trace(self, _sql: str) -> None:
        self.query_count += 1

    def _table_columns(self, table: str) -> set[str]:
        return {
            str(row[1])
            for row in self._connection().execute(f"PRAGMA table_info({table})")
        }

    def _mode(self, city_id: str) -> ProviderMode:
        if self.provider_id not in self.snapshot.catalog.providers_for_city(city_id):
            raise RuntimeUnavailable(
                f"provider={self.provider_id} has no mapping for city={city_id}"
            )
        return self.snapshot.catalog.provider_mode(city_id, self.provider_id)

    def _ensure_temporal_covers(self, requested_date: date) -> None:
        valid_from = self.references.temporal.valid_from
        valid_through = self.references.temporal.valid_through
        if not valid_from or not valid_through:
            raise RuntimeUnavailable(
                f"provider={self.provider_id} temporal validity window is missing"
            )
        if not (valid_from <= requested_date.isoformat() <= valid_through):
            raise RuntimeUnavailable(
                f"provider={self.provider_id} temporal artifact does not cover {requested_date.isoformat()}"
            )

    def _prefixes(self, city_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        mode = self._mode(city_id)
        return (mode.stop_id_prefix,), (mode.identifier_prefix,)

    @staticmethod
    def _public_identifier(value: object, prefixes: tuple[str, ...]) -> str:
        text = str(value or "")
        for prefix in sorted((item for item in prefixes if item), key=len, reverse=True):
            if text.startswith(prefix):
                return text[len(prefix):]
        return text

    @staticmethod
    def _direction_key(
        route_id: str,
        direction_id: str | None,
        destination_stop_id: str | None,
        destination: str,
    ) -> str:
        direction = (direction_id or "").strip()
        terminal = (destination_stop_id or "").strip()
        if terminal:
            return f"{route_id}|direction:{direction}|destination-stop:{terminal}"
        normalized = "".join(
            character
            for character in unicodedata.normalize("NFKD", destination).casefold()
            if not unicodedata.combining(character)
        )
        normalized = " ".join(normalized.split())
        return f"{route_id}|direction:{direction}|destination:{normalized}"

    def _canonical_candidates(self, city_id: str, stop_id: str) -> tuple[str, ...]:
        stop_prefixes, _ = self._prefixes(city_id)
        return tuple(dict.fromkeys(
            stop_id
            if prefix and stop_id.startswith(prefix)
            else f"{prefix}{stop_id}" if prefix else stop_id
            for prefix in stop_prefixes
        ))

    def _query_stop_id(self, city_id: str, stop_id: str) -> str:
        mode = self._mode(city_id)
        internal = (
            stop_id
            if mode.stop_id_prefix and stop_id.startswith(mode.stop_id_prefix)
            else f"{mode.stop_id_prefix}{stop_id}" if mode.stop_id_prefix else stop_id
        )
        if mode.mode != "exact-stop-with-parent-fallback":
            return internal
        connection = self._connection()
        if connection.execute(
            "SELECT 1 FROM stop_times WHERE raw_stop_id=? LIMIT 1", (internal,)
        ).fetchone() is not None:
            return internal
        row = connection.execute(
            "SELECT parent_station FROM raw_stops WHERE stop_id=?", (internal,)
        ).fetchone()
        return str(row[0]) if row and row[0] else internal

    def _external_stop_time_ids(self, city_id: str, stop_id: str) -> tuple[str, ...]:
        mode = self._mode(city_id)
        internal = stop_id if not mode.stop_id_prefix or stop_id.startswith(mode.stop_id_prefix) else mode.stop_id_prefix + stop_id
        connection = self._connection()
        row = connection.execute(
            "SELECT stop_id, location_type FROM raw_stops WHERE stop_id=?",
            (internal,),
        ).fetchone()
        if row is None or int(row[1] or 0) != 1:
            return (internal,)
        children = connection.execute(
            """
            SELECT stop_id FROM raw_stops
            WHERE parent_station=? AND location_type=0
            ORDER BY stop_id
            """,
            (internal,),
        ).fetchall()
        return (internal, *(str(child[0]) for child in children))

    @staticmethod
    def _departure_datetime(service_date: object, departure_time: object, timezone: ZoneInfo) -> datetime | None:
        try:
            parsed_date = datetime.strptime(str(service_date), "%Y%m%d").date()
            hour, minute, second = (int(part) for part in str(departure_time).split(":"))
        except (TypeError, ValueError):
            return None
        if hour < 0 or minute not in range(60) or second not in range(60):
            return None
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone) + timedelta(
            hours=hour, minutes=minute, seconds=second
        )

    def lines(self, city_id: str, stop_id: str) -> list[dict[str, str | None]]:
        mode = self._mode(city_id)
        stop_prefixes, identifier_prefixes = self._prefixes(city_id)
        query_stop_id = self._query_stop_id(city_id, stop_id)
        if mode.mode == "exact-stop-with-parent-fallback":
            predicate, parameters = "s.raw_stop_id=?", (query_stop_id,)
        else:
            candidates = self._canonical_candidates(city_id, stop_id)
            predicate = f"rs.canonical_stop_id IN ({','.join('?' for _ in candidates)})"
            parameters = candidates
        rows = self._connection().execute(
            f"""
            SELECT DISTINCT t.route_id,
                   COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),
                   t.direction_id,
                   COALESCE(NULLIF(t.headsign,''),NULLIF(destination_stops.stop_name,''),'Unbekanntes Ziel'),
                   t.terminal_stop_id
            FROM stop_times s
            JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id
            JOIN trips t ON t.trip_id=s.trip_id
            LEFT JOIN routes r ON r.route_id=t.route_id
            LEFT JOIN raw_stops destination_stops ON destination_stops.stop_id=t.terminal_stop_id
            WHERE {predicate}
            ORDER BY 2,1
            """,
            parameters,
        ).fetchall()
        result = []
        for route_id, line, direction, destination, destination_stop_id in rows:
            public_route = self._public_identifier(route_id, identifier_prefixes)
            public_destination = self._public_identifier(destination_stop_id, stop_prefixes)
            result.append({
                "routeID": public_route,
                "line": line,
                "directionID": direction or None,
                "direction": destination or None,
                "destination": destination or None,
                "destinationStopID": public_destination or None,
                "directionKey": self._direction_key(public_route, direction, public_destination, destination),
            })
        return result

    def external_departures_for(
        self,
        city_id: str,
        stop_id: str,
        limit: int,
        from_datetime: datetime | None,
        timezone_name: str,
        now_provider: Callable[[], datetime] | None = None,
    ) -> list[dict[str, object]]:
        timezone = ZoneInfo(timezone_name)
        lower_bound = from_datetime or (now_provider() if now_provider else datetime.now(timezone))
        if lower_bound.tzinfo is None:
            lower_bound = lower_bound.replace(tzinfo=timezone)
        lower_bound = lower_bound.astimezone(timezone)
        self._ensure_temporal_covers(lower_bound.date())
        stop_ids = self._external_stop_time_ids(city_id, stop_id)
        if not stop_ids:
            return []
        connection = self._connection()
        raw_columns = self._table_columns("raw_stops")
        route_columns = self._table_columns("routes")
        stop_time_columns = self._table_columns("stop_times")
        agency_columns = self._table_columns("agencies")
        platform = "COALESCE(NULLIF(rs.platform_display,''),NULLIF(rs.platform_code,''))" if "platform_display" in raw_columns else "rs.platform_code"
        floor = "rs.floor_display" if "floor_display" in raw_columns else "''"
        parent = "rs.parent_station" if "parent_station" in raw_columns else "''"
        agency_id = "r.agency_id" if "agency_id" in route_columns else "''"
        agency_join = "LEFT JOIN agencies ag ON ag.agency_id=r.agency_id" if "agency_name" in agency_columns and "agency_id" in route_columns else ""
        agency_name = "ag.agency_name" if agency_join else "''"
        route_type = "r.route_type" if "route_type" in route_columns else "''"
        seconds = "s.departure_seconds" if "departure_seconds" in stop_time_columns else (
            "CAST(substr(s.departure_time, 1, instr(s.departure_time, ':') - 1) AS INTEGER) * 3600 "
            "+ CAST(substr(s.departure_time, instr(s.departure_time, ':') + 1, 2) AS INTEGER) * 60 "
            "+ CAST(substr(s.departure_time, length(s.departure_time) - 1, 2) AS INTEGER)"
        )
        service_day = "julianday(substr(a.service_date,1,4) || '-' || substr(a.service_date,5,2) || '-' || substr(a.service_date,7,2))"
        absolute_key = f"({service_day} + ({seconds}) / 86400.0)"
        placeholders = ",".join("?" for _ in stop_ids)
        service_from = (lower_bound.date() - timedelta(days=1)).strftime("%Y%m%d")
        service_to = (lower_bound.date() + timedelta(days=1)).strftime("%Y%m%d")
        lower_date = lower_bound.date().isoformat()
        lower_seconds = lower_bound.hour * 3600 + lower_bound.minute * 60 + lower_bound.second + lower_bound.microsecond / 1_000_000
        requested_limit = max(1, int(limit))
        query = f"""
            SELECT a.service_date, s.departure_time, s.stop_sequence, s.raw_stop_id,
                   t.trip_id, t.route_id,
                   COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),
                   COALESCE(NULLIF(t.headsign,''),NULLIF(destination_stops.stop_name,''),'Unbekanntes Ziel'),
                   t.direction_id, {platform}, {floor}, {parent}, {agency_id}, {agency_name}, {route_type}
            FROM stop_times s
            JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id
            JOIN trips t ON t.trip_id=s.trip_id
            JOIN temporal.active_services a ON a.service_id=t.service_id
            LEFT JOIN routes r ON r.route_id=t.route_id
            {agency_join}
            LEFT JOIN raw_stops destination_stops ON destination_stops.stop_id=t.terminal_stop_id
            WHERE s.raw_stop_id IN ({placeholders})
              AND a.service_date BETWEEN ? AND ?
              AND {absolute_key} >= julianday(?) + ? / 86400.0
            ORDER BY {absolute_key}, t.trip_id, t.route_id, s.raw_stop_id, s.stop_sequence
            LIMIT ?
        """
        rows = connection.execute(
            query,
            (*stop_ids, service_from, service_to, lower_date, lower_seconds, requested_limit),
        ).fetchall()
        stop_prefixes, identifier_prefixes = self._prefixes(city_id)
        result: list[tuple[datetime, dict[str, object]]] = []
        for row in rows:
            service_date, departure_time, sequence, raw_stop, trip_id, route_id, line, destination, direction, platform_value, floor_value, parent_value, agency_value, operator, route_type_value = row
            absolute = self._departure_datetime(service_date, departure_time, timezone)
            if absolute is None or absolute < lower_bound:
                continue
            public_route = self._public_identifier(route_id, identifier_prefixes)
            public_stop = self._public_identifier(raw_stop, stop_prefixes)
            route_type_text = str(route_type_value or "")
            result.append((absolute, {
                "tripID": self._public_identifier(trip_id, identifier_prefixes),
                "routeID": public_route,
                "line": line or public_route,
                "destination": destination or None,
                "directionID": direction or None,
                "scheduledTime": departure_time or None,
                "scheduledDeparture": departure_time or None,
                "serviceDate": datetime.strptime(str(service_date), "%Y%m%d").date().isoformat(),
                "departureDateTime": absolute.isoformat(),
                "stopSequence": sequence,
                "operatorID": self._public_identifier(agency_value, identifier_prefixes) if agency_value else None,
                "operator": operator or None,
                "stopID": public_stop,
                "parentStation": self._public_identifier(parent_value, stop_prefixes) if parent_value else None,
                "platform": platform_value or None,
                "floor": floor_value or None,
                "transportMode": GTFS_ROUTE_TYPE_TO_MODE.get(route_type_text, "unknown"),
                "routeType": int(route_type_text) if route_type_text.isdigit() else None,
                "isRealtime": False,
                "source": "scheduled-static",
            }))
        result.sort(key=lambda item: (item[0], str(item[1].get("tripID") or ""), str(item[1].get("routeID") or ""), str(item[1].get("stopID") or ""), int(item[1].get("stopSequence") or 0)))
        return [item for _, item in result[:requested_limit]]

    def trip_details(self, city_id: str, trip_id: str, static_root: str, service_date: str | None = None) -> dict[str, object] | None:
        if not city_id or any(part in city_id for part in ("/", "\\", "..")):
            raise ValueError("invalid cityID")
        if service_date:
            service_date = datetime.fromisoformat(service_date).strftime("%Y%m%d")
            self._ensure_temporal_covers(datetime.strptime(service_date, "%Y%m%d").date())
        stop_prefixes, identifier_prefixes = self._prefixes(city_id)
        candidates = tuple(dict.fromkeys(
            trip_id if prefix and trip_id.startswith(prefix) else prefix + trip_id
            for prefix in identifier_prefixes
        ))
        connection = self._connection()
        route_columns = self._table_columns("routes")
        agency_join = "LEFT JOIN agencies ag ON ag.agency_id=r.agency_id" if "agency_id" in route_columns and self._table_columns("agencies") else ""
        agency_fields = "r.agency_id,ag.agency_name" if agency_join else "NULL,NULL"
        route_type = "r.route_type" if "route_type" in route_columns else "NULL"
        arrival = "s.arrival_time" if "arrival_time" in self._table_columns("stop_times") else "NULL"
        row = connection.execute(
            f"""
            SELECT t.trip_id,t.route_id,COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),
                   t.headsign,t.direction_id,{agency_fields},{route_type},t.service_id
            FROM trips t LEFT JOIN routes r ON r.route_id=t.route_id {agency_join}
            WHERE t.trip_id IN ({','.join('?' for _ in candidates)}) ORDER BY t.trip_id LIMIT 1
            """,
            candidates,
        ).fetchone()
        if row is None:
            return None
        if service_date and connection.execute(
            "SELECT 1 FROM temporal.active_services WHERE service_id=? AND service_date=?",
            (row[8], service_date),
        ).fetchone() is None:
            return None
        stops = connection.execute(
            f"""
            SELECT s.raw_stop_id,s.stop_sequence,{arrival},s.departure_time,rs.stop_name
            FROM stop_times s JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id
            WHERE s.trip_id=? ORDER BY s.stop_sequence,s.raw_stop_id
            """,
            (row[0],),
        ).fetchall()
        catalog: list[dict[str, object]] = []
        root = Path(static_root) if static_root else self.snapshot.stop_data_root
        try:
            value = json.loads((root / "stops" / f"{city_id}.json").read_text(encoding="utf-8"))
            catalog = value if isinstance(value, list) else []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            pass
        wanted = {self._public_identifier(stop[0], stop_prefixes) for stop in stops}
        coordinates = {
            self._public_identifier(item.get("id"), stop_prefixes): item
            for item in catalog
            if isinstance(item, dict) and self._public_identifier(item.get("id"), stop_prefixes) in wanted
        }
        ordered = []
        for stop_id, sequence, arrival_value, departure, name in stops:
            public_id = self._public_identifier(stop_id, stop_prefixes)
            metadata = coordinates.get(public_id, {})
            ordered.append({
                "id": public_id,
                "name": name,
                "stopSequence": sequence,
                "scheduledArrival": arrival_value or None,
                "scheduledDeparture": departure or None,
                "latitude": metadata.get("latitude"),
                "longitude": metadata.get("longitude"),
                "platform": metadata.get("platform"),
                "floor": metadata.get("floor"),
            })
        route_type_value = str(row[7] or "")
        return {
            "tripID": self._public_identifier(row[0], identifier_prefixes),
            "routeID": self._public_identifier(row[1], identifier_prefixes),
            "line": row[2],
            "destination": row[3] or (ordered[-1]["name"] if ordered else None),
            "directionID": row[4],
            "operatorID": self._public_identifier(row[5], identifier_prefixes) or None,
            "operator": row[6],
            "transportMode": GTFS_ROUTE_TYPE_TO_MODE.get(route_type_value, "unknown"),
            "timezone": self._mode(city_id).timezone,
            "serviceDate": datetime.strptime(service_date, "%Y%m%d").date().isoformat() if service_date else None,
            "stops": ordered,
            "geometry": None,
            "source": "scheduled-static",
            "isRealtime": False,
        }

    def board(self, city_id: str, stop_id: str, limit: int, from_date: datetime | None = None, to_date: datetime | None = None) -> list[dict[str, object]]:
        mode = self._mode(city_id)
        service_from = (from_date.date() - timedelta(days=1)).strftime("%Y%m%d") if from_date else "00000000"
        service_to = to_date.date().strftime("%Y%m%d") if to_date else "99999999"
        requested_date = (from_date or to_date or datetime.now()).date()
        self._ensure_temporal_covers(requested_date)
        stop_prefixes, identifier_prefixes = self._prefixes(city_id)
        query_stop_id = self._query_stop_id(city_id, stop_id)
        if mode.mode == "exact-stop-with-parent-fallback":
            predicate, parameters = "s.raw_stop_id=?", (query_stop_id,)
        else:
            candidates = self._canonical_candidates(city_id, stop_id)
            predicate = f"rs.canonical_stop_id IN ({','.join('?' for _ in candidates)})"
            parameters = candidates
        connection = self._connection()
        raw_columns = self._table_columns("raw_stops")
        route_columns = self._table_columns("routes")
        has_agencies = bool(self._table_columns("agencies"))
        platform = "COALESCE(NULLIF(rs.platform_display,''),NULLIF(rs.platform_code,''))" if "platform_display" in raw_columns else "rs.platform_code"
        floor = "rs.floor_display" if "floor_display" in raw_columns else "''"
        location_type = "rs.location_type" if "location_type" in raw_columns else "0"
        stop_desc = "rs.stop_desc" if "stop_desc" in raw_columns else "''"
        agency_id = "r.agency_id" if "agency_id" in route_columns else "''"
        agency_join = "LEFT JOIN agencies a ON a.agency_id=r.agency_id" if has_agencies and "agency_id" in route_columns else ""
        agency_name = "a.agency_name" if agency_join else "''"
        route_type = "r.route_type" if "route_type" in route_columns else "''"
        rows = connection.execute(
            f"""
            SELECT a.service_date,s.departure_time,s.departure_seconds,s.stop_sequence,s.raw_stop_id,
                   t.trip_id,t.route_id,
                   COALESCE(NULLIF(r.short_name,''),NULLIF(r.long_name,''),t.route_id),
                   COALESCE(NULLIF(t.headsign,''),NULLIF(destination_stops.stop_name,''),'Unbekanntes Ziel'),
                   t.direction_id,t.terminal_stop_id,{platform},{floor},rs.parent_station,{location_type},
                   {stop_desc},{agency_id},{agency_name},{route_type}
            FROM stop_times s
            JOIN raw_stops rs ON rs.stop_id=s.raw_stop_id
            JOIN trips t ON t.trip_id=s.trip_id
            JOIN temporal.active_services a ON a.service_id=t.service_id
            LEFT JOIN routes r ON r.route_id=t.route_id
            {agency_join}
            LEFT JOIN raw_stops destination_stops ON destination_stops.stop_id=t.terminal_stop_id
            WHERE {predicate} AND a.service_date BETWEEN ? AND ?
            ORDER BY a.service_date,s.departure_seconds,t.trip_id,s.stop_sequence
            LIMIT ?
            """,
            (*parameters, service_from, service_to, limit),
        ).fetchall()
        result = []
        for service_date, departure_time, _seconds, sequence, raw_stop, trip_id, route_id, line, destination, direction, terminal, platform_value, floor_value, parent, location, description, agency_value, operator, route_type_value in rows:
            public_route = self._public_identifier(route_id, identifier_prefixes)
            public_terminal = self._public_identifier(terminal, stop_prefixes)
            result.append({
                "serviceDate": f"{service_date[:4]}-{service_date[4:6]}-{service_date[6:]}",
                "scheduledTime": departure_time,
                "tripID": self._public_identifier(trip_id, identifier_prefixes),
                "routeID": public_route,
                "line": line,
                "destination": destination,
                "directionID": direction or None,
                "direction": direction or None,
                "directionKey": self._direction_key(public_route, direction, public_terminal, destination),
                "destinationStopID": public_terminal or None,
                "platform": platform_value or None,
                "stopID": self._public_identifier(raw_stop, stop_prefixes),
                "stopSequence": sequence,
                "isRealtime": False,
                "operator": operator or None,
                "agencyID": self._public_identifier(agency_value, identifier_prefixes) if agency_value else None,
                "parentStation": self._public_identifier(parent, stop_prefixes) if parent else None,
                "platformStopID": self._public_identifier(raw_stop, stop_prefixes),
                "floor": floor_value or None,
                "stopDesc": description or None,
                "locationType": location,
                "routeType": int(route_type_value) if route_type_value and str(route_type_value).isdigit() else None,
            })
        return result

    def trip_registry(self) -> tuple[set[str], dict[str, str]]:
        rows = self._connection().execute(
            "SELECT trip_id, route_id FROM trips"
        ).fetchall()
        return {str(row[0]) for row in rows}, {str(row[0]): str(row[1]) for row in rows if row[1]}

    def realtime_metadata(self) -> tuple[set[str], set[str], dict[str, str], dict[str, str]]:
        trips, route_by_trip = self.trip_registry()
        rows = self._connection().execute("SELECT route_id FROM routes").fetchall()
        headsigns = {
            str(row[0]): str(row[1] or "")
            for row in self._connection().execute("SELECT trip_id, headsign FROM trips")
        }
        return trips, {str(row[0]) for row in rows}, route_by_trip, headsigns

    def route_type_registry(self) -> dict[str, str]:
        return {str(row[0]): str(row[1]) for row in self._connection().execute("SELECT route_id, route_type FROM routes")}

    def route_metadata(self) -> dict[str, tuple[str, str]]:
        return {str(row[0]): (str(row[1] or ""), str(row[2] or "")) for row in self._connection().execute("SELECT route_id, short_name, route_type FROM routes")}

    def stop_registry(self) -> set[str]:
        rows = self._connection().execute("SELECT key_1 FROM provider_entities WHERE entity_type='raw_stops'").fetchall()
        return {str(row[0]) for row in rows}

    def trip_stop_registry(self, trip_ids: set[str]) -> dict[tuple[str, int], str]:
        if not trip_ids:
            return {}
        placeholders = ",".join("?" for _ in trip_ids)
        rows = self._connection().execute(
            f"SELECT trip_id, stop_sequence, raw_stop_id FROM stop_times WHERE trip_id IN ({placeholders})",
            tuple(sorted(trip_ids)),
        ).fetchall()
        return {(str(row[0]), int(row[1])): str(row[2]) for row in rows}

    def close(self) -> None:
        with self.lock:
            connections = tuple(self._connections.values())
            self._connections.clear()
            for connection in connections:
                connection.close()


class ReleaseSnapshot:
    """Immutable release generation with lazy, bounded provider runtimes."""

    def __init__(
        self,
        manifest: ReleaseManifest,
        *,
        trace_queries: bool = False,
        max_provider_connections: int = 4,
        max_parallel_provider_queries: int = 4,
    ) -> None:
        self.manifest = manifest
        self.release_id = manifest.release_id
        self.stop_data_root = manifest.stop_data_root
        self.trace_queries = trace_queries
        self.max_provider_connections = max(1, int(max_provider_connections))
        self.max_parallel_provider_queries = max(1, int(max_parallel_provider_queries))
        self.connection_count = 1
        self.peak_provider_connections = 0
        self._common_connection = sqlite3.connect(
            f"file:{manifest.common_database_path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self._common_connection.execute("PRAGMA query_only=ON")
        self.catalog = CommonCatalog(self._common_connection, manifest)
        self._providers: OrderedDict[str, ProviderSnapshot] = OrderedDict()
        self._active_provider_ids: set[str] = set()
        self._lock = threading.RLock()
        self._closed = False
        self.last_fanout_metrics: FanoutMetrics | None = None

    @classmethod
    def open(
        cls,
        release_root: Path | str,
        *,
        provider_ids: tuple[str, ...] = (ISRAEL_PROVIDER_ID,),
        trace_queries: bool = False,
        max_provider_connections: int = 4,
        max_parallel_provider_queries: int = 4,
    ) -> "ReleaseSnapshot":
        manifest = load_release_manifest(Path(release_root), provider_ids=provider_ids)
        return cls(
            manifest,
            trace_queries=trace_queries,
            max_provider_connections=max_provider_connections,
            max_parallel_provider_queries=max_parallel_provider_queries,
        )

    def provider(self, provider_id: str) -> ProviderSnapshot:
        with self._lock:
            if self._closed:
                raise RuntimeUnavailable("release snapshot is closed")
            if provider_id not in self.manifest.providers:
                raise RuntimeUnavailable(f"provider={provider_id} is not validated in this snapshot")
            existing = self._providers.get(provider_id)
            if existing is not None:
                self._providers.move_to_end(provider_id)
                return existing
            if len(self._providers) >= self.max_provider_connections:
                evict_id = next(
                    (candidate for candidate in self._providers if candidate not in self._active_provider_ids),
                    None,
                )
                if evict_id is None:
                    raise RuntimeUnavailable("provider connection cache capacity exceeded by active fan-out")
                evicted = self._providers.pop(evict_id)
                evicted.close()
            runtime = ProviderSnapshot(self, provider_id)
            self._providers[provider_id] = runtime
            return runtime

    def _fanout_batches(
        self,
        query: str,
        city_id: str,
        provider_ids: tuple[str, ...],
        operation: Callable[[ProviderSnapshot], list[object]],
    ) -> dict[str, list[object]]:
        if not provider_ids:
            self.last_fanout_metrics = FanoutMetrics(query, city_id, (), 0, 0, 0.0, 0.0)
            return {}
        if len(provider_ids) > len(self.manifest.providers):
            raise RuntimeUnavailable(f"query={query} requested unvalidated providers")
        parallelism = min(self.max_parallel_provider_queries, len(provider_ids))
        fanout_started = time.perf_counter()
        values: dict[str, list[object]] = {}
        errors: dict[str, Exception] = {}
        def run_provider(provider_id: str) -> list[object]:
            with self._lock:
                self._active_provider_ids.add(provider_id)
            provider: ProviderSnapshot | None = None
            try:
                provider = self.provider(provider_id)
                return operation(provider)
            finally:
                with self._lock:
                    self._active_provider_ids.discard(provider_id)
        with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="static-shard") as executor:
            futures = {
                executor.submit(run_provider, provider_id): provider_id
                for provider_id in provider_ids
            }
            for future in as_completed(futures):
                provider_id = futures[future]
                try:
                    values[provider_id] = future.result()
                except Exception as error:
                    errors[provider_id] = error
        fanout_duration = time.perf_counter() - fanout_started
        if errors:
            details = ", ".join(f"{provider}={type(error).__name__}" for provider, error in sorted(errors.items()))
            raise RuntimeUnavailable(f"query={query} required provider failure: {details}") from next(iter(errors.values()))
        rows_fetched = sum(len(values[provider_id]) for provider_id in provider_ids)
        self.last_fanout_metrics = FanoutMetrics(
            query=query,
            city_id=city_id,
            provider_ids=provider_ids,
            provider_count=len(provider_ids),
            rows_fetched=rows_fetched,
            fanout_duration=fanout_duration,
            merge_duration=0.0,
        )
        return values

    def _query_providers(
        self,
        query: str,
        city_id: str,
        provider_ids: tuple[str, ...],
        operation: Callable[[ProviderSnapshot], list[object]],
    ) -> list[object]:
        batches = self._fanout_batches(query, city_id, provider_ids, operation)
        return [item for provider_id in provider_ids for item in batches[provider_id]]

    def _query_scope(self, city_id: str, stop_id: str | None = None) -> tuple[str, tuple[str, ...]]:
        resolved_city = self.catalog.resolve_city(city_id)
        providers = (
            self.catalog.providers_for_stop(resolved_city, stop_id)
            if stop_id is not None
            else self.catalog.providers_for_city(resolved_city)
        )
        if not providers:
            raise RuntimeUnavailable(f"city={resolved_city} has no routed providers")
        return resolved_city, providers

    def provider_modes(self, city_id: str) -> tuple[ProviderMode, ...]:
        resolved_city = self.catalog.resolve_city(city_id)
        return self.catalog.provider_modes(resolved_city)

    def provider_contexts(self, city_id: str) -> tuple[dict[str, object], ...]:
        """Aggregate realtime ownership metadata only for providers routed to a city."""
        resolved_city, providers = self._query_scope(city_id)
        batches = self._fanout_batches(
            "provider-contexts",
            resolved_city,
            providers,
            lambda provider: [provider.realtime_metadata()],
        )
        contexts = []
        for provider_id in providers:
            trips, routes, route_by_trip, headsign_by_trip = batches[provider_id][0]
            contexts.append(
                {
                    "providerID": provider_id,
                    "trips": frozenset((provider_id, str(value)) for value in trips),
                    "routes": frozenset((provider_id, str(value)) for value in routes),
                    "routeByTrip": {(provider_id, str(trip)): (provider_id, str(route)) for trip, route in route_by_trip.items()},
                    "headsignByTrip": {(provider_id, str(trip)): value for trip, value in headsign_by_trip.items()},
                }
            )
        return tuple(contexts)

    def city_has_stop(self, city_id: str, stop_id: str) -> bool:
        resolved_city = self.catalog.resolve_city(city_id)
        return self.catalog.city_has_stop(resolved_city, stop_id)

    def lines(self, city_id: str, stop_id: str) -> list[dict[str, str | None]]:
        resolved_city, providers = self._query_scope(city_id, stop_id)
        started = time.perf_counter()
        batches = self._fanout_batches("lines", resolved_city, providers, lambda provider: provider.lines(resolved_city, stop_id))
        rows = [value for provider_id in providers for value in batches[provider_id]]
        decorated = [(provider_id, value) for provider_id in providers for value in batches[provider_id]]
        decorated.sort(key=lambda item: (str(item[1].get("line") or ""), str(item[1].get("routeID") or ""), item[0]))
        result = [dict(value) for _, value in decorated]
        self._finish_merge("lines", resolved_city, providers, len(rows), started)
        return result

    @staticmethod
    def _seconds(value: object) -> int:
        try:
            hour, minute, second = (int(part) for part in str(value).split(":"))
            return hour * 3600 + minute * 60 + second
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _absolute_datetime(value: object) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=ZoneInfo("UTC"))
        except (TypeError, ValueError):
            return datetime.max.replace(tzinfo=ZoneInfo("UTC"))

    def _finish_merge(self, query: str, city_id: str, providers: tuple[str, ...], rows_fetched: int, started: float) -> None:
        metrics = self.last_fanout_metrics
        if metrics is None:
            return
        self.last_fanout_metrics = FanoutMetrics(
            query=query,
            city_id=city_id,
            provider_ids=providers,
            provider_count=len(providers),
            rows_fetched=rows_fetched,
            fanout_duration=metrics.fanout_duration,
            merge_duration=time.perf_counter() - started - metrics.fanout_duration,
        )

    def external_departures_for(
        self,
        city_id: str,
        stop_id: str,
        limit: int,
        from_datetime: datetime | None,
        timezone_name: str,
        now_provider: Callable[[], datetime] | None = None,
    ) -> list[dict[str, object]]:
        resolved_city, providers = self._query_scope(city_id, stop_id)
        started = time.perf_counter()
        batches = self._fanout_batches(
            "departures",
            resolved_city,
            providers,
            lambda provider: provider.external_departures_for(
                resolved_city, stop_id, max(1, int(limit)), from_datetime, timezone_name, now_provider=now_provider
            ),
        )
        decorated: list[tuple[tuple[object, ...], dict[str, object]]] = []
        for provider_id in providers:
            provider_rows = batches[provider_id]
            for value in provider_rows:
                decorated.append((
                    (
                        self._absolute_datetime(value.get("departureDateTime")),
                        str(value.get("tripID") or ""),
                        str(value.get("routeID") or ""),
                        str(value.get("stopID") or ""),
                        int(value.get("stopSequence") or 0),
                        provider_id,
                    ),
                    dict(value),
                ))
        decorated.sort(key=lambda item: item[0])
        result = [value for _, value in decorated[:max(1, int(limit))]]
        self._finish_merge("departures", resolved_city, providers, sum(len(batch) for batch in batches.values()), started)
        return result

    def board(
        self,
        city_id: str,
        stop_id: str,
        limit: int,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> list[dict[str, object]]:
        resolved_city, providers = self._query_scope(city_id, stop_id)
        started = time.perf_counter()
        requested_limit = max(1, int(limit))
        batches = self._fanout_batches(
            "board",
            resolved_city,
            providers,
            lambda provider: provider.board(resolved_city, stop_id, requested_limit, from_date, to_date),
        )
        decorated: list[tuple[tuple[object, ...], dict[str, object]]] = []
        for provider_id in providers:
            provider_rows = batches[provider_id]
            for value in provider_rows:
                decorated.append((
                    (
                        str(value.get("serviceDate") or ""),
                        self._seconds(value.get("scheduledTime")),
                        str(value.get("tripID") or ""),
                        int(value.get("stopSequence") or 0),
                        provider_id,
                    ),
                    dict(value),
                ))
        decorated.sort(key=lambda item: item[0])
        result = [value for _, value in decorated[:requested_limit]]
        self._finish_merge("board", resolved_city, providers, sum(len(batch) for batch in batches.values()), started)
        return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            for provider in self._providers.values():
                provider.close()
            self._providers.clear()
            self._active_provider_ids.clear()
            self.catalog.close()
            self._closed = True

    def __enter__(self) -> "ReleaseSnapshot":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


@dataclass
class _ManagedGeneration:
    path: Path
    snapshot: ReleaseSnapshot
    references: int = 0


class ReleaseSnapshotLease:
    """Reference-counted request lease for one immutable release snapshot."""

    def __init__(self, manager: "ReleaseManager", generation: _ManagedGeneration) -> None:
        self._manager = manager
        self._generation = generation
        self._released = False

    @property
    def snapshot(self) -> ReleaseSnapshot:
        if self._released:
            raise RuntimeUnavailable("release snapshot lease is already released")
        return self._generation.snapshot

    @property
    def release_id(self) -> str:
        return self.snapshot.release_id

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._manager._release(self._generation)

    def __getattr__(self, name: str) -> object:
        return getattr(self.snapshot, name)

    def __enter__(self) -> "ReleaseSnapshotLease":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.release()


class ReleaseManager:
    """Generation manager for one atomic current-release pointer."""

    def __init__(
        self,
        current_release_pointer: Path | str,
        *,
        provider_ids: tuple[str, ...] = (ISRAEL_PROVIDER_ID,),
        trace_queries: bool = False,
        max_provider_connections: int = 4,
        max_parallel_provider_queries: int = 4,
    ) -> None:
        pointer = Path(current_release_pointer)
        self.current_release_pointer = pointer.parent.resolve() / pointer.name
        self.provider_ids = tuple(dict.fromkeys(value.strip() for value in provider_ids if value.strip()))
        if not self.provider_ids:
            raise RuntimeUnavailable("release manager requires at least one provider")
        self.trace_queries = trace_queries
        self.max_provider_connections = max_provider_connections
        self.max_parallel_provider_queries = max_parallel_provider_queries
        self._lock = threading.RLock()
        self._active: _ManagedGeneration | None = None
        self._old: list[_ManagedGeneration] = []
        self._closed = False

    def _pointer_target(self) -> Path:
        pointer = self.current_release_pointer.parent.resolve() / self.current_release_pointer.name
        if not pointer.is_symlink():
            raise RuntimeUnavailable(f"current-release pointer is missing or not a symlink: {pointer}")
        try:
            target = (pointer.parent / os.readlink(pointer)).resolve(strict=True)
        except OSError as error:
            raise RuntimeUnavailable(f"current-release pointer is unavailable: {pointer}") from error
        if not target.is_dir():
            raise RuntimeUnavailable(f"current-release target is not a directory: {target}")
        return target

    def _open_generation(self, target: Path) -> _ManagedGeneration:
        snapshot = ReleaseSnapshot.open(
            target,
            provider_ids=self.provider_ids,
            trace_queries=self.trace_queries,
            max_provider_connections=self.max_provider_connections,
            max_parallel_provider_queries=self.max_parallel_provider_queries,
        )
        return _ManagedGeneration(target, snapshot)

    def _reload_locked(self) -> bool:
        if self._closed:
            raise RuntimeUnavailable("release manager is closed")
        target = self._pointer_target()
        if self._active is not None and self._active.path == target:
            return False
        candidate = self._open_generation(target)
        previous = self._active
        self._active = candidate
        if previous is not None:
            if previous.references == 0:
                previous.snapshot.close()
            else:
                self._old.append(previous)
        self.drain_old_snapshots()
        return True

    def reload_if_changed(self) -> bool:
        with self._lock:
            return self._reload_locked()

    def acquire_snapshot(self) -> ReleaseSnapshotLease:
        with self._lock:
            self._reload_locked()
            if self._active is None:
                raise RuntimeUnavailable("release manager has no active snapshot")
            self._active.references += 1
            return ReleaseSnapshotLease(self, self._active)

    @property
    def active_release_id(self) -> str:
        with self._lock:
            if self._active is None:
                self._reload_locked()
            if self._active is None:
                raise RuntimeUnavailable("release manager has no active snapshot")
            return self._active.snapshot.release_id

    def _release(self, generation: _ManagedGeneration) -> None:
        with self._lock:
            if generation.references <= 0:
                raise RuntimeUnavailable("release snapshot lease was released twice")
            generation.references -= 1
            self.drain_old_snapshots()

    def drain_old_snapshots(self) -> int:
        with self._lock:
            remaining: list[_ManagedGeneration] = []
            closed = 0
            for generation in self._old:
                if generation.references == 0:
                    generation.snapshot.close()
                    closed += 1
                else:
                    remaining.append(generation)
            self._old = remaining
            return closed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            active_references = self._active.references if self._active is not None else 0
            old_references = sum(generation.references for generation in self._old)
            if active_references or old_references:
                raise RuntimeUnavailable("cannot close release manager while snapshots are leased")
            if self._active is not None:
                self._active.snapshot.close()
            for generation in self._old:
                generation.snapshot.close()
            self._active = None
            self._old = []
            self._closed = True


@dataclass(frozen=True)
class ComparisonResult:
    status: str
    diagnostic: str | None
    legacy_count: int | None
    shard_count: int | None


def _result_count(value: object) -> int | None:
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    if value is None:
        return 0
    return 1


def _bounded(value: object, limit: int = 320) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _first_difference(legacy: object, shard: object, path: str = "$") -> str | None:
    if type(legacy) is not type(shard):
        return f"{path}: type {_bounded(type(legacy).__name__)} != {_bounded(type(shard).__name__)}"
    if isinstance(legacy, dict):
        if set(legacy) != set(shard):
            return f"{path}: keys {_bounded(sorted(legacy))} != {_bounded(sorted(shard))}"
        for key in sorted(legacy, key=str):
            difference = _first_difference(legacy[key], shard[key], f"{path}.{key}")
            if difference:
                return difference
        return None
    if isinstance(legacy, (list, tuple)):
        if len(legacy) != len(shard):
            return f"{path}: length {len(legacy)} != {len(shard)}"
        for index, (left, right) in enumerate(zip(legacy, shard)):
            difference = _first_difference(left, right, f"{path}[{index}]")
            if difference:
                return difference
        return None
    if legacy != shard:
        return f"{path}: {_bounded(legacy)} != {_bounded(shard)}"
    return None


def compare_results(legacy: object, shard: object) -> ComparisonResult:
    diagnostic = _first_difference(legacy, shard)
    return ComparisonResult(
        status="MATCH" if diagnostic is None else "MISMATCH",
        diagnostic=diagnostic,
        legacy_count=_result_count(legacy),
        shard_count=_result_count(shard),
    )


class ShadowStaticDeparturesBackend:
    """Proxy that keeps legacy authoritative while comparing shard results."""

    def __init__(
        self,
        legacy: object,
        snapshot: ReleaseSnapshot,
        provider_id: str = ISRAEL_PROVIDER_ID,
        provider_ids: tuple[str, ...] | None = None,
    ) -> None:
        self.legacy = legacy
        self.snapshot = snapshot
        configured = provider_ids or (provider_id,)
        self.provider_ids = tuple(dict.fromkeys(value.strip() for value in configured if value.strip()))
        if not self.provider_ids:
            raise RuntimeUnavailable("shadow provider runtime requires at least one provider")
        self.provider_id = self.provider_ids[0]

    def _run(
        self,
        query: str,
        legacy_call: Callable[[], object],
        shard_call: Callable[[], object],
        *,
        city_id: str | None = None,
        providers: tuple[str, ...] = (),
    ) -> object:
        legacy_started = time.perf_counter()
        legacy_value: object = None
        legacy_error: Exception | None = None
        try:
            legacy_value = legacy_call()
        except Exception as error:
            legacy_error = error
        legacy_duration = time.perf_counter() - legacy_started
        shard_started = time.perf_counter()
        shard_value: object = None
        shard_error: Exception | None = None
        try:
            shard_value = shard_call()
        except Exception as error:
            shard_error = error
        shard_duration = time.perf_counter() - shard_started
        if legacy_error is not None or shard_error is not None:
            status = "MATCH" if type(legacy_error) is type(shard_error) else "ERROR"
            diagnostic = None
            if status != "MATCH":
                diagnostic = f"legacy={type(legacy_error).__name__ if legacy_error else 'none'} shard={type(shard_error).__name__ if shard_error else 'none'}"
            legacy_count = _result_count(legacy_value) if legacy_error is None else None
            shard_count = _result_count(shard_value) if shard_error is None else None
        else:
            comparison = compare_results(legacy_value, shard_value)
            status = comparison.status
            diagnostic = comparison.diagnostic
            legacy_count = comparison.legacy_count
            shard_count = comparison.shard_count
        metrics = self.snapshot.last_fanout_metrics
        routed_providers = providers or self.provider_ids
        provider_count = metrics.provider_count if metrics is not None else len(routed_providers)
        fanout_duration = metrics.fanout_duration if metrics is not None else shard_duration
        merge_duration = metrics.merge_duration if metrics is not None else 0.0
        LOGGER.info(
            "event=shard-shadow-query stage=shard-shadow-query provider=%s city=%s providers=%s query=%s status=%s legacy_duration=%.6f shard_duration=%.6f provider_count=%s fanout_duration=%.6f merge_duration=%.6f legacy_count=%s shard_count=%s release_id=%s",
            self.provider_id,
            city_id or "",
            ",".join(routed_providers),
            query,
            status,
            legacy_duration,
            shard_duration,
            provider_count,
            fanout_duration,
            merge_duration,
            legacy_count,
            shard_count,
            self.snapshot.release_id,
        )
        if status != "MATCH" and diagnostic:
            LOGGER.warning(
                "event=shard-shadow-diagnostic provider=%s query=%s release_id=%s diagnostic=%s",
                self.provider_id,
                query,
                self.snapshot.release_id,
                diagnostic,
            )
        if legacy_error is not None:
            raise legacy_error
        return legacy_value

    def __getattr__(self, name: str) -> object:
        return getattr(self.legacy, name)

    def resolve_city(self, city_id: str) -> str:
        return self._run("resolve-city", lambda: self.legacy.resolve_city(city_id), lambda: self.snapshot.catalog.resolve_city(city_id))

    def city_has_stop(self, city_id: str, stop_id: str) -> bool:
        providers = self.snapshot.catalog.providers_for_stop(city_id, stop_id)
        return self._run(
            "city-stop-validation",
            lambda: self.legacy.city_has_stop(city_id, stop_id),
            lambda: self.snapshot.city_has_stop(city_id, stop_id),
            city_id=city_id,
            providers=providers,
        )

    def city_departure_mode(self, city_id: str) -> tuple[str, str, str, str]:
        providers = self.snapshot.catalog.providers_for_city(city_id)
        mode_provider = providers[0] if providers else self.provider_id
        def shard() -> tuple[str, str, str, str]:
            mode = self.snapshot.catalog.provider_mode(city_id, mode_provider)
            return mode.mode, mode.timezone, mode.stop_id_prefix, mode.identifier_prefix
        return self._run("provider-city-mode", lambda: self.legacy.city_departure_mode(city_id), shard, city_id=city_id, providers=providers)

    def city_departure_prefixes(self, city_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        def shard() -> tuple[tuple[str, ...], tuple[str, ...]]:
            modes = self.snapshot.catalog.provider_modes(city_id)
            return tuple(mode.stop_id_prefix for mode in modes), tuple(mode.identifier_prefix for mode in modes)
        providers = self.snapshot.catalog.providers_for_city(city_id)
        return self._run("provider-city-prefixes", lambda: self.legacy.city_departure_prefixes(city_id), shard, city_id=city_id, providers=providers)

    def lines(self, city_id: str, stop_id: str) -> list[dict[str, str | None]]:
        providers = self.snapshot.catalog.providers_for_stop(city_id, stop_id)
        return self._run("lines", lambda: self.legacy.lines(city_id, stop_id), lambda: self.snapshot.lines(city_id, stop_id), city_id=city_id, providers=providers)

    def external_departures_for(self, city_id: str, stop_id: str, limit: int, from_datetime: datetime | None, timezone_name: str, now_provider: Callable[[], datetime] | None = None) -> list[dict[str, object]]:
        providers = self.snapshot.catalog.providers_for_stop(city_id, stop_id)
        return self._run(
            "departures",
            lambda: self.legacy.external_departures_for(city_id, stop_id, limit, from_datetime, timezone_name, now_provider=now_provider),
            lambda: self.snapshot.external_departures_for(city_id, stop_id, limit, from_datetime, timezone_name, now_provider=now_provider),
            city_id=city_id,
            providers=providers,
        )

    def board(self, city_id: str, stop_id: str, limit: int, from_date: datetime | None = None, to_date: datetime | None = None) -> list[dict[str, object]]:
        providers = self.snapshot.catalog.providers_for_stop(city_id, stop_id)
        return self._run("board", lambda: self.legacy.board(city_id, stop_id, limit, from_date, to_date), lambda: self.snapshot.board(city_id, stop_id, limit, from_date, to_date), city_id=city_id, providers=providers)

    def _trip_provider(self, trip_id: str) -> str:
        return next(
            (
                provider_id
                for provider_id in self.provider_ids
                if trip_id.startswith(f"{provider_id}:")
            ),
            self.provider_id,
        )

    def trip_details(self, city_id: str, trip_id: str, static_root: str, service_date: str | None = None) -> dict[str, object] | None:
        provider_id = self._trip_provider(trip_id)
        return self._run("trip-details", lambda: self.legacy.trip_details(city_id, trip_id, static_root, service_date), lambda: self.snapshot.provider(provider_id).trip_details(city_id, trip_id, static_root, service_date), providers=(provider_id,))

    def provider_trip_registry(self, provider_id: str) -> tuple[set[str], dict[str, str]]:
        return self._run("trip-registry", lambda: self.legacy.provider_trip_registry(provider_id), lambda: self.snapshot.provider(provider_id).trip_registry(), providers=(provider_id,)) if provider_id in self.provider_ids else self.legacy.provider_trip_registry(provider_id)

    def provider_realtime_metadata(self, provider_id: str) -> tuple[set[str], set[str], dict[str, str], dict[str, str]]:
        return self._run("realtime-metadata", lambda: self.legacy.provider_realtime_metadata(provider_id), lambda: self.snapshot.provider(provider_id).realtime_metadata(), providers=(provider_id,)) if provider_id in self.provider_ids else self.legacy.provider_realtime_metadata(provider_id)

    def provider_realtime_registry(self, provider_id: str) -> tuple[set[str], set[str], dict[str, str]]:
        return self._run("realtime-registry", lambda: self.legacy.provider_realtime_registry(provider_id), lambda: self.snapshot.provider(provider_id).realtime_metadata()[:3], providers=(provider_id,)) if provider_id in self.provider_ids else self.legacy.provider_realtime_registry(provider_id)

    def provider_route_type_registry(self, provider_id: str) -> dict[str, str]:
        return self._run("route-registry", lambda: self.legacy.provider_route_type_registry(provider_id), lambda: self.snapshot.provider(provider_id).route_type_registry(), providers=(provider_id,)) if provider_id in self.provider_ids else self.legacy.provider_route_type_registry(provider_id)

    def provider_route_metadata(self, provider_id: str) -> dict[str, tuple[str, str]]:
        return self._run("route-metadata", lambda: self.legacy.provider_route_metadata(provider_id), lambda: self.snapshot.provider(provider_id).route_metadata(), providers=(provider_id,)) if provider_id in self.provider_ids else self.legacy.provider_route_metadata(provider_id)

    def provider_stop_registry(self, provider_id: str) -> set[str]:
        return self._run("stop-registry", lambda: self.legacy.provider_stop_registry(provider_id), lambda: self.snapshot.provider(provider_id).stop_registry(), providers=(provider_id,)) if provider_id in self.provider_ids else self.legacy.provider_stop_registry(provider_id)

    def provider_trip_stop_registry(self, provider_id: str, trip_ids: set[str]) -> dict[tuple[str, int], str]:
        return self._run("trip-stop-registry", lambda: self.legacy.provider_trip_stop_registry(provider_id, trip_ids), lambda: self.snapshot.provider(provider_id).trip_stop_registry(trip_ids), providers=(provider_id,)) if provider_id in self.provider_ids else self.legacy.provider_trip_stop_registry(provider_id, trip_ids)

    def close(self) -> None:
        close = getattr(self.legacy, "close", None)
        if callable(close):
            close()
        self.snapshot.close()


@dataclass(frozen=True)
class _HybridScope:
    backend: str
    reason: str
    city_id: str | None
    providers: tuple[str, ...]


class HybridStaticDeparturesBackend:
    """Authoritative shard proxy for configured providers with legacy routing fallback."""

    def __init__(
        self,
        legacy: object,
        manager: ReleaseManager,
        provider_ids: tuple[str, ...],
        *,
        compare_legacy: bool = False,
    ) -> None:
        self.legacy = legacy
        self.manager = manager
        self.provider_ids = tuple(dict.fromkeys(value.strip() for value in provider_ids if value.strip()))
        if not self.provider_ids:
            raise RuntimeUnavailable("hybrid provider runtime requires at least one provider")
        self.eligible_provider_ids = frozenset(self.provider_ids)
        self.compare_legacy = compare_legacy

    @staticmethod
    def _scope(
        snapshot: ReleaseSnapshot,
        city_id: str,
        stop_id: str | None,
        eligible_provider_ids: frozenset[str],
        *,
        allow_multi_provider: bool = True,
    ) -> _HybridScope:
        resolved_city = snapshot.catalog.resolve_city(city_id)
        providers = (
            snapshot.catalog.providers_for_stop(resolved_city, stop_id)
            if stop_id is not None
            else snapshot.catalog.providers_for_city(resolved_city)
        )
        if not providers:
            return _HybridScope("legacy", "no-routed-providers", resolved_city, ())
        if any(provider_id not in eligible_provider_ids for provider_id in providers):
            reason = "mixed-provider-scope" if any(
                provider_id in eligible_provider_ids for provider_id in providers
            ) else "non-pilot-provider"
            return _HybridScope("legacy", reason, resolved_city, providers)
        if not allow_multi_provider and len(providers) > 1:
            return _HybridScope("legacy", "multi-provider-metadata", resolved_city, providers)
        return _HybridScope("shard", "pilot-provider-scope", resolved_city, providers)

    def _log_routing(
        self,
        query: str,
        scope: _HybridScope,
        *,
        provider: str = "",
        release_id: str = "",
    ) -> None:
        LOGGER.info(
            "event=hybrid-routing query=%s city=%s provider=%s providers=%s "
            "backend=%s reason=%s release_id=%s",
            query,
            scope.city_id or "",
            provider,
            ",".join(scope.providers),
            scope.backend,
            scope.reason,
            release_id,
        )

    def _run_shard(
        self,
        snapshot: ReleaseSnapshot,
        query: str,
        scope: _HybridScope,
        release_id: str,
        shard_call: Callable[[ReleaseSnapshot], object],
        legacy_call: Callable[[], object],
        *,
        provider: str = "",
    ) -> object:
        shard_started = time.perf_counter()
        try:
            shard_value = shard_call(snapshot)
        except Exception:
            shard_duration = time.perf_counter() - shard_started
            LOGGER.error(
                "event=hybrid-authoritative-query status=ERROR query=%s city=%s "
                "provider=%s providers=%s shard_duration=%.6f legacy_compare_duration=0 "
                "release_id=%s",
                query,
                scope.city_id or "",
                provider,
                ",".join(scope.providers),
                shard_duration,
                release_id,
                exc_info=True,
            )
            raise
        shard_duration = time.perf_counter() - shard_started
        status = "OK"
        legacy_duration = 0.0
        diagnostic: str | None = None
        if self.compare_legacy:
            legacy_started = time.perf_counter()
            try:
                legacy_value = legacy_call()
            except Exception as error:
                legacy_duration = time.perf_counter() - legacy_started
                status = "ERROR"
                diagnostic = f"legacy={type(error).__name__}"
            else:
                legacy_duration = time.perf_counter() - legacy_started
                comparison = compare_results(legacy_value, shard_value)
                status = comparison.status
                diagnostic = comparison.diagnostic
        LOGGER.info(
            "event=hybrid-authoritative-query status=%s query=%s city=%s provider=%s "
            "providers=%s shard_duration=%.6f legacy_compare_duration=%.6f release_id=%s",
            status,
            query,
            scope.city_id or "",
            provider,
            ",".join(scope.providers),
            shard_duration,
            legacy_duration,
            release_id,
        )
        if diagnostic:
            LOGGER.warning(
                "event=hybrid-pilot-failure status=%s query=%s city=%s providers=%s "
                "release_id=%s",
                status,
                query,
                scope.city_id or "",
                ",".join(scope.providers),
                release_id,
            )
            LOGGER.warning(
                "event=hybrid-authoritative-diagnostic query=%s city=%s providers=%s "
                "release_id=%s diagnostic=%s",
                query,
                scope.city_id or "",
                ",".join(scope.providers),
                release_id,
                diagnostic,
            )
        return shard_value

    def _run_city(
        self,
        query: str,
        city_id: str,
        stop_id: str | None,
        legacy_call: Callable[[], object],
        shard_call: Callable[[ReleaseSnapshot], object],
        *,
        allow_multi_provider: bool = True,
    ) -> object:
        with self.manager.acquire_snapshot() as lease:
            scope = self._scope(
                lease.snapshot,
                city_id,
                stop_id,
                self.eligible_provider_ids,
                allow_multi_provider=allow_multi_provider,
            )
            self._log_routing(query, scope, release_id=lease.release_id)
            if scope.backend == "legacy":
                return legacy_call()
            return self._run_shard(
                lease.snapshot,
                query,
                scope,
                lease.release_id,
                shard_call,
                legacy_call,
            )

    def _run_provider(
        self,
        query: str,
        provider_id: str,
        legacy_call: Callable[[], object],
        shard_call: Callable[[ReleaseSnapshot], object],
    ) -> object:
        scope = _HybridScope(
            "shard" if provider_id in self.eligible_provider_ids else "legacy",
            "pilot-provider-scope" if provider_id in self.eligible_provider_ids else "non-pilot-provider",
            None,
            (provider_id,),
        )
        if scope.backend == "legacy":
            self._log_routing(query, scope, provider=provider_id)
            return legacy_call()
        with self.manager.acquire_snapshot() as lease:
            self._log_routing(query, scope, provider=provider_id, release_id=lease.release_id)
            return self._run_shard(
                lease.snapshot,
                query,
                scope,
                lease.release_id,
                shard_call,
                legacy_call,
                provider=provider_id,
            )

    def resolve_city(self, city_id: str) -> str:
        return self.legacy.resolve_city(city_id)

    def city_has_stop(self, city_id: str, stop_id: str) -> bool:
        return bool(self._run_city(
            "city-stop-validation",
            city_id,
            stop_id,
            lambda: self.legacy.city_has_stop(city_id, stop_id),
            lambda snapshot: snapshot.city_has_stop(city_id, stop_id),
        ))

    def city_departure_mode(self, city_id: str) -> tuple[str, str, str, str]:
        return self._run_city(
            "provider-city-mode",
            city_id,
            None,
            lambda: self.legacy.city_departure_mode(city_id),
            lambda snapshot: self._provider_mode(snapshot, city_id),
            allow_multi_provider=False,
        )  # type: ignore[return-value]

    @staticmethod
    def _provider_mode(snapshot: ReleaseSnapshot, city_id: str) -> tuple[str, str, str, str]:
        resolved_city = snapshot.catalog.resolve_city(city_id)
        providers = snapshot.catalog.providers_for_city(resolved_city)
        mode = snapshot.catalog.provider_mode(resolved_city, providers[0])
        return mode.mode, mode.timezone, mode.stop_id_prefix, mode.identifier_prefix

    def city_departure_prefixes(self, city_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return self._run_city(
            "provider-city-prefixes",
            city_id,
            None,
            lambda: self.legacy.city_departure_prefixes(city_id),
            lambda snapshot: (
                tuple(mode.stop_id_prefix for mode in snapshot.provider_modes(city_id)),
                tuple(mode.identifier_prefix for mode in snapshot.provider_modes(city_id)),
            ),
            allow_multi_provider=False,
        )  # type: ignore[return-value]

    def lines(self, city_id: str, stop_id: str) -> list[dict[str, str | None]]:
        return self._run_city(
            "lines",
            city_id,
            stop_id,
            lambda: self.legacy.lines(city_id, stop_id),
            lambda snapshot: snapshot.lines(city_id, stop_id),
        )  # type: ignore[return-value]

    def external_departures_for(
        self,
        city_id: str,
        stop_id: str,
        limit: int,
        from_datetime: datetime | None,
        timezone_name: str,
        now_provider: Callable[[], datetime] | None = None,
    ) -> list[dict[str, object]]:
        return self._run_city(
            "departures",
            city_id,
            stop_id,
            lambda: self.legacy.external_departures_for(
                city_id, stop_id, limit, from_datetime, timezone_name, now_provider=now_provider
            ),
            lambda snapshot: snapshot.external_departures_for(
                city_id, stop_id, limit, from_datetime, timezone_name, now_provider=now_provider
            ),
        )  # type: ignore[return-value]

    def board(
        self,
        city_id: str,
        stop_id: str,
        limit: int,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> list[dict[str, object]]:
        return self._run_city(
            "board",
            city_id,
            stop_id,
            lambda: self.legacy.board(city_id, stop_id, limit, from_date, to_date),
            lambda snapshot: snapshot.board(city_id, stop_id, limit, from_date, to_date),
        )  # type: ignore[return-value]

    def _trip_provider(self, snapshot: ReleaseSnapshot, city_id: str, trip_id: str) -> str | None:
        for provider_id in self.provider_ids:
            if trip_id.startswith(f"{provider_id}:"):
                return provider_id
        providers = snapshot.catalog.providers_for_city(snapshot.catalog.resolve_city(city_id))
        return providers[0] if len(providers) == 1 else None

    def trip_details(
        self,
        city_id: str,
        trip_id: str,
        static_root: str,
        service_date: str | None = None,
    ) -> dict[str, object] | None:
        with self.manager.acquire_snapshot() as lease:
            provider_id = self._trip_provider(lease.snapshot, city_id, trip_id)
            if provider_id not in self.eligible_provider_ids:
                scope = _HybridScope("legacy", "non-pilot-provider", city_id, (provider_id,) if provider_id else ())
                self._log_routing("trip-details", scope, provider=provider_id or "", release_id=lease.release_id)
                return self.legacy.trip_details(city_id, trip_id, static_root, service_date)
            scope = _HybridScope("shard", "pilot-provider-scope", city_id, (provider_id,))
            self._log_routing("trip-details", scope, provider=provider_id, release_id=lease.release_id)
            return self._run_shard(
                lease.snapshot,
                "trip-details",
                scope,
                lease.release_id,
                lambda snapshot: snapshot.provider(provider_id).trip_details(city_id, trip_id, static_root, service_date),
                lambda: self.legacy.trip_details(city_id, trip_id, static_root, service_date),
                provider=provider_id,
            )  # type: ignore[return-value]

    def provider_trip_registry(self, provider_id: str) -> tuple[set[str], dict[str, str]]:
        return self._run_provider(
            "trip-registry",
            provider_id,
            lambda: self.legacy.provider_trip_registry(provider_id),
            lambda snapshot: snapshot.provider(provider_id).trip_registry(),
        )  # type: ignore[return-value]

    def provider_realtime_metadata(self, provider_id: str) -> tuple[set[str], set[str], dict[str, str], dict[str, str]]:
        return self._run_provider(
            "realtime-metadata",
            provider_id,
            lambda: self.legacy.provider_realtime_metadata(provider_id),
            lambda snapshot: snapshot.provider(provider_id).realtime_metadata(),
        )  # type: ignore[return-value]

    def provider_realtime_registry(self, provider_id: str) -> tuple[set[str], set[str], dict[str, str]]:
        return self._run_provider(
            "realtime-registry",
            provider_id,
            lambda: self.legacy.provider_realtime_registry(provider_id),
            lambda snapshot: snapshot.provider(provider_id).realtime_metadata()[:3],
        )  # type: ignore[return-value]

    def provider_route_type_registry(self, provider_id: str) -> dict[str, str]:
        return self._run_provider(
            "route-registry",
            provider_id,
            lambda: self.legacy.provider_route_type_registry(provider_id),
            lambda snapshot: snapshot.provider(provider_id).route_type_registry(),
        )  # type: ignore[return-value]

    def provider_route_metadata(self, provider_id: str) -> dict[str, tuple[str, str]]:
        return self._run_provider(
            "route-metadata",
            provider_id,
            lambda: self.legacy.provider_route_metadata(provider_id),
            lambda snapshot: snapshot.provider(provider_id).route_metadata(),
        )  # type: ignore[return-value]

    def provider_stop_registry(self, provider_id: str) -> set[str]:
        return self._run_provider(
            "stop-registry",
            provider_id,
            lambda: self.legacy.provider_stop_registry(provider_id),
            lambda snapshot: snapshot.provider(provider_id).stop_registry(),
        )  # type: ignore[return-value]

    def provider_trip_stop_registry(self, provider_id: str, trip_ids: set[str]) -> dict[tuple[str, int], str]:
        return self._run_provider(
            "trip-stop-registry",
            provider_id,
            lambda: self.legacy.provider_trip_stop_registry(provider_id, trip_ids),
            lambda snapshot: snapshot.provider(provider_id).trip_stop_registry(trip_ids),
        )  # type: ignore[return-value]

    def close(self) -> None:
        try:
            self.manager.close()
        finally:
            close = getattr(self.legacy, "close", None)
            if callable(close):
                close()

    def __getattr__(self, name: str) -> object:
        return getattr(self.legacy, name)


def _environment_flag(values: Mapping[str, str], name: str) -> bool:
    return str(values.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def hybrid_runtime_enabled(*, environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return _environment_flag(values, HYBRID_ENV)


def _configured_provider_ids(values: Mapping[str, str]) -> tuple[str, ...]:
    configured = str(values.get(HYBRID_PROVIDER_ENV, ",".join(DEFAULT_HYBRID_PROVIDERS)))
    return tuple(dict.fromkeys(value.strip() for value in configured.split(",") if value.strip()))


def hybrid_backend_from_environment(
    legacy: object,
    *,
    environ: Mapping[str, str] | None = None,
) -> HybridStaticDeparturesBackend | None:
    values = os.environ if environ is None else environ
    if not _environment_flag(values, HYBRID_ENV):
        return None
    provider_ids = _configured_provider_ids(values)
    if not provider_ids:
        raise RuntimeUnavailable(f"{HYBRID_PROVIDER_ENV} requires at least one provider")
    repository_root = Path(__file__).resolve().parents[1]
    ineligible = tuple(
        provider_id
        for provider_id in provider_ids
        if not provider_capability(repository_root, provider_id, SHARD_RUNTIME)
        or not provider_capability(repository_root, provider_id, HYBRID_RUNTIME)
    )
    if ineligible:
        raise RuntimeUnavailable(
            f"authoritative hybrid runtime is not enabled for: {', '.join(ineligible)}"
        )
    pointer = str(values.get(HYBRID_RELEASE_POINTER_ENV, "")).strip()
    if not pointer:
        raise RuntimeUnavailable(f"{HYBRID_RELEASE_POINTER_ENV} is required when hybrid mode is enabled")
    try:
        max_parallel = max(1, int(values.get(HYBRID_MAX_PARALLEL_ENV, "4")))
    except (TypeError, ValueError) as error:
        raise RuntimeUnavailable(f"{HYBRID_MAX_PARALLEL_ENV} must be a positive integer") from error
    compare_legacy = _environment_flag(values, HYBRID_COMPARE_ENV)
    manager = ReleaseManager(
        pointer,
        provider_ids=provider_ids,
        max_provider_connections=max_parallel,
        max_parallel_provider_queries=max_parallel,
    )
    try:
        with manager.acquire_snapshot() as lease:
            release_id = lease.release_id
    except Exception:
        manager.close()
        raise
    LOGGER.info(
        "event=hybrid-runtime status=READY providers=%s release_id=%s compare_legacy=%s rss_bytes=%d",
        ",".join(provider_ids),
        release_id,
        str(compare_legacy).lower(),
        _rss_bytes(),
    )
    return HybridStaticDeparturesBackend(
        legacy,
        manager,
        provider_ids,
        compare_legacy=compare_legacy,
    )


def shadow_backend_from_environment(legacy: object, *, environ: Mapping[str, str] | None = None) -> ShadowStaticDeparturesBackend | None:
    values = os.environ if environ is None else environ
    enabled = str(values.get(SHADOW_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    provider_ids = tuple(
        dict.fromkeys(
            value.strip()
            for value in str(values.get(SHADOW_PROVIDER_ENV, ISRAEL_PROVIDER_ID)).split(",")
            if value.strip()
        )
    )
    if not provider_ids:
        raise RuntimeUnavailable(f"{SHADOW_PROVIDER_ENV} requires at least one provider")
    repository_root = Path(__file__).resolve().parents[1]
    ineligible = tuple(
        provider_id
        for provider_id in provider_ids
        if not provider_capability(repository_root, provider_id, SHARD_RUNTIME)
    )
    if ineligible:
        raise RuntimeUnavailable(
            f"shadow provider runtime is not enabled for: {', '.join(ineligible)}"
        )
    release_root = str(values.get(SHADOW_RELEASE_ENV, "")).strip()
    if not release_root:
        raise RuntimeUnavailable(f"{SHADOW_RELEASE_ENV} is required when shadow mode is enabled")
    try:
        max_parallel = max(1, int(values.get(SHADOW_MAX_PARALLEL_ENV, "4")))
    except (TypeError, ValueError) as error:
        raise RuntimeUnavailable(f"{SHADOW_MAX_PARALLEL_ENV} must be a positive integer") from error
    snapshot = ReleaseSnapshot.open(
        Path(release_root),
        provider_ids=provider_ids,
        max_provider_connections=max_parallel,
        max_parallel_provider_queries=max_parallel,
    )
    LOGGER.info(
        "event=shard-shadow-runtime status=READY provider=%s release_id=%s rss_bytes=%d",
        ",".join(provider_ids),
        snapshot.release_id,
        _rss_bytes(),
    )
    return ShadowStaticDeparturesBackend(
        legacy,
        snapshot,
        provider_id=provider_ids[0],
        provider_ids=provider_ids,
    )
