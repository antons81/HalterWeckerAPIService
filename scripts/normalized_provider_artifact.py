"""Persistent semantic normalized artifacts for selected external providers."""

from __future__ import annotations

import ast
import hashlib
import heapq
import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    from .gtfs_csv import normalized_dict_reader
except ImportError:
    from gtfs_csv import normalized_dict_reader

try:
    from .provider_artifact_capabilities import PERSISTENT_NORMALIZED, provider_capability
except ImportError:
    from provider_artifact_capabilities import PERSISTENT_NORMALIZED, provider_capability


ISRAEL_PROVIDER_ID = "israel-mot"
FEATURE_GATE = "HALTEWECKER_PERSISTENT_NORMALIZED_PROVIDER_ARTIFACT"
STATIC_DEPARTURES_FEATURE_GATE = "HALTEWECKER_STATIC_DEPARTURES_NORMALIZED_PROVIDER_ARTIFACT"
CACHE_ROOT_ENV = "HALTEWECKER_NORMALIZED_PROVIDER_CACHE_ROOT"
ARTIFACT_SCHEMA_VERSION = 2

NORMALIZED_FILES = (
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "calendar.txt",
    "calendar_dates.txt",
    "transfers.txt",
    "pathways.txt",
)

REQUIRED_TABLES = (
    "agencies",
    "routes",
    "stops",
    "trips",
    "stop_times",
    "calendar",
    "calendar_dates",
    "transfers",
    "pathways",
    "source_metadata",
)

REQUIRED_INDEXES = (
    "stop_times_trip_sequence",
    "stop_times_stop_trip",
    "trips_service",
)

NORMALIZED_SCHEMA_COLUMNS = {
    "agencies": ("agency_id", "agency_name"),
    "routes": (
        "route_id", "route_short_name", "route_long_name", "route_type",
        "agency_id", "agency_name",
    ),
    "stops": (
        "stop_id", "stop_name", "stop_lat", "stop_lon", "stop_code",
        "parent_station", "location_type", "platform_code", "stop_desc",
        "platform_display", "floor_display",
    ),
    "trips": ("trip_id", "route_id", "service_id", "trip_headsign", "direction_id"),
    "stop_times": (
        "trip_id", "stop_id", "arrival_time", "departure_time",
        "arrival_seconds", "departure_seconds", "stop_sequence",
    ),
    "calendar": (
        "service_id", "start_date", "end_date", "monday", "tuesday",
        "wednesday", "thursday", "friday", "saturday", "sunday",
    ),
    "calendar_dates": ("service_id", "service_date", "exception_type"),
    "transfers": (
        "from_stop_id", "to_stop_id", "from_trip_id", "to_trip_id",
        "from_route_id", "to_route_id", "transfer_type", "min_transfer_time",
    ),
    "pathways": ("payload",),
    "source_metadata": ("filename", "columns_json"),
}

# Only code that can change the normalized representation belongs here. External
# projection, departure, line, logging, and cache-retention code is deliberately
# excluded from this fingerprint.
BUILDER_SYMBOLS = {
    "scripts/external_staging.py": (
        "legacy_int_or_none",
        "SOURCE_FILENAMES",
        "SOURCE_COLUMNS",
        "_serialize_source_row",
        "_deserialize_source_row",
        "NormalizedProviderContext.__init__",
        "NormalizedProviderContext._populate",
    ),
    "scripts/gtfs_csv.py": (
        "_skip_leading_blank_lines",
        "_normalized_fieldnames",
        "NormalizedGTFSReader",
        "normalized_dict_reader",
    ),
    "scripts/gtfs_stop_metadata.py": ("display_stop_metadata",),
    "scripts/build_german_departure_index.py": ("parse_gtfs_time",),
    "scripts/normalized_provider_artifact.py": (
        "_canonical_row_payload",
        "_semantic_fingerprint_for_file",
        "_semantic_manifest",
        "_normalized_schema_fingerprint",
        "_semantic_key",
        "builder_fingerprint",
    ),
}


class NormalizedArtifactError(ValueError):
    """Raised when a persistent normalized artifact cannot be trusted."""


@dataclass(frozen=True)
class SemanticManifest:
    file_fingerprints: dict[str, str]
    row_counts: dict[str, int]
    file_columns: dict[str, list[str]]


@dataclass(frozen=True)
class NormalizedArtifactUse:
    status: str
    reason: str
    semantic_key: str
    artifact_directory: Path
    database_path: Path
    manifest: dict[str, object]
    duration_seconds: float


def feature_enabled(environ: dict[str, str] | None = None) -> bool:
    values = environ if environ is not None else os.environ
    return values.get(FEATURE_GATE, "0").strip() == "1"


def static_departures_feature_enabled(environ: dict[str, str] | None = None) -> bool:
    values = environ if environ is not None else os.environ
    return values.get(STATIC_DEPARTURES_FEATURE_GATE, "0").strip() == "1"


def provider_enabled(provider_id: str, repository_root: Path) -> bool:
    return provider_capability(repository_root, provider_id, PERSISTENT_NORMALIZED)


def default_cache_root(
    *,
    gtfs_cache_root: Path | None,
    environ: dict[str, str] | None = None,
) -> Path:
    values = environ if environ is not None else os.environ
    configured = values.get(CACHE_ROOT_ENV, "").strip()
    if configured:
        return Path(configured)
    if gtfs_cache_root is not None:
        return gtfs_cache_root.parent / "normalized-providers"
    gtfs_cache = values.get("GTFS_CACHE_ROOT", "").strip()
    if gtfs_cache:
        return Path(gtfs_cache).parent / "normalized-providers"
    return Path("cache") / "normalized-providers"


def _canonical_row_payload(row: dict[object, object], columns: tuple[str, ...]) -> bytes:
    values: list[object] = []
    for column in columns:
        if column == "__extra__":
            value = row.get(None, [])
        else:
            value = row.get(column, "")
        if value is None:
            value = ""
        values.append(value)
    return json.dumps(
        {"columns": columns, "values": values},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _iter_binary_digests(path: Path) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while True:
            value = stream.read(32)
            if not value:
                return
            if len(value) != 32:
                raise NormalizedArtifactError(f"Truncated semantic digest chunk: {path}")
            yield value


def _semantic_fingerprint_for_file(
    archive,
    filename: str,
    temporary_root: Path,
    *,
    chunk_size: int = 100_000,
) -> tuple[str, int, list[str]]:
    """Hash a CSV row multiset in deterministic order without retaining all rows."""
    if filename not in set(archive.namelist()):
        payload = json.dumps(
            {"filename": filename, "columns": [], "rowCount": 0},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest(), 0, []

    chunks: list[Path] = []
    row_buffer: list[bytes] = []
    row_count = 0
    columns: tuple[str, ...] = ()
    with archive.open(filename) as raw:
        reader = normalized_dict_reader(
            (line.decode("utf-8-sig") for line in raw)
        )
        columns = tuple(sorted(tuple(reader.fieldnames or ()) + ("__extra__",)))
        for row in reader:
            row_buffer.append(hashlib.sha256(_canonical_row_payload(row, columns)).digest())
            row_count += 1
            if len(row_buffer) >= chunk_size:
                chunk_path = temporary_root / f"{filename.replace('/', '_')}.{len(chunks)}.bin"
                row_buffer.sort()
                with chunk_path.open("wb") as chunk:
                    for digest in row_buffer:
                        chunk.write(digest)
                chunks.append(chunk_path)
                row_buffer = []

    if row_buffer:
        chunk_path = temporary_root / f"{filename.replace('/', '_')}.{len(chunks)}.bin"
        row_buffer.sort()
        with chunk_path.open("wb") as chunk:
            for digest in row_buffer:
                chunk.write(digest)
        chunks.append(chunk_path)

    digest = hashlib.sha256()
    digest.update(filename.encode("utf-8"))
    digest.update(b"\0")
    digest.update(json.dumps(columns, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(row_count).encode("ascii"))
    digest.update(b"\0")
    for row_digest in heapq.merge(*(_iter_binary_digests(path) for path in chunks)):
        digest.update(row_digest)
    for chunk in chunks:
        chunk.unlink(missing_ok=True)
    return digest.hexdigest(), row_count, list(columns)


def _semantic_manifest(archive) -> SemanticManifest:
    with tempfile.TemporaryDirectory(prefix="haltewecker-semantic-fingerprint-") as temporary:
        temporary_root = Path(temporary)
        fingerprints: dict[str, str] = {}
        row_counts: dict[str, int] = {}
        file_columns: dict[str, list[str]] = {}
        for filename in NORMALIZED_FILES:
            fingerprint, row_count, columns = _semantic_fingerprint_for_file(
                archive,
                filename,
                temporary_root,
            )
            fingerprints[filename] = fingerprint
            row_counts[filename] = row_count
            file_columns[filename] = columns
    return SemanticManifest(fingerprints, row_counts, file_columns)


def _ast_symbol_payload(path: Path, names: tuple[str, ...]) -> list[dict[str, object]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    top_level_nodes: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_level_nodes[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                if isinstance(target, ast.Name):
                    top_level_nodes[target.id] = node
    nodes: dict[str, ast.AST] = {}
    for name in names:
        if "." not in name:
            node = top_level_nodes.get(name)
            if node is not None:
                nodes[name] = node
            continue
        class_name, method_name = name.split(".", 1)
        class_node = top_level_nodes.get(class_name)
        if not isinstance(class_node, ast.ClassDef):
            continue
        method = next(
            (
                node
                for node in class_node.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method_name
            ),
            None,
        )
        if method is not None:
            nodes[name] = method
    missing = [name for name in names if name not in nodes]
    if missing:
        raise NormalizedArtifactError(
            f"Missing normalized builder symbols in {path}: {', '.join(missing)}"
        )
    return [
        {
            "name": name,
            "ast": ast.dump(nodes[name], annotate_fields=True, include_attributes=False),
        }
        for name in names
    ]


def builder_fingerprint(repository_root: Path) -> str:
    payload: list[dict[str, object]] = []
    for relative, names in sorted(BUILDER_SYMBOLS.items()):
        path = repository_root / relative
        if not path.is_file():
            raise NormalizedArtifactError(f"Missing normalized builder input: {relative}")
        payload.append(
            {
                "path": relative,
                "symbols": _ast_symbol_payload(path, names),
            }
        )
    payload.append({"normalizedSourceFiles": NORMALIZED_FILES})
    payload.append({"artifactSchemaVersion": ARTIFACT_SCHEMA_VERSION})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _semantic_key(
    *,
    provider_id: str,
    builder: str,
    semantic: SemanticManifest,
) -> str:
    payload = {
        "schemaVersion": ARTIFACT_SCHEMA_VERSION,
        "providerID": provider_id,
        "fileFingerprints": semantic.file_fingerprints,
        "normalizedSchemaFingerprint": _normalized_schema_fingerprint(),
        "builderFingerprint": builder,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalized_schema_fingerprint() -> str:
    payload = {
        "tables": NORMALIZED_SCHEMA_COLUMNS,
        "indexes": REQUIRED_INDEXES,
        "artifactSchemaVersion": ARTIFACT_SCHEMA_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _artifact_provenance(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _validate_sqlite(path: Path) -> None:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        missing_tables = set(REQUIRED_TABLES) - tables
        missing_indexes = set(REQUIRED_INDEXES) - indexes
        if missing_tables or missing_indexes:
            raise NormalizedArtifactError(
                "normalized SQLite schema is incomplete: "
                f"tables={sorted(missing_tables)} indexes={sorted(missing_indexes)}"
            )
        for table, expected_columns in NORMALIZED_SCHEMA_COLUMNS.items():
            actual_columns = tuple(
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual_columns != expected_columns:
                raise NormalizedArtifactError(
                    f"normalized SQLite table schema mismatch for {table}: "
                    f"expected={expected_columns!r} actual={actual_columns!r}"
                )
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check != ("ok",):
            raise NormalizedArtifactError(
                f"normalized SQLite quick_check failed: {quick_check!r}"
            )
    except sqlite3.DatabaseError as error:
        raise NormalizedArtifactError(f"normalized SQLite cannot be opened: {error}") from error
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass


def _manifest_for(
    *,
    provider_id: str,
    semantic_key: str,
    semantic: SemanticManifest,
    builder: str,
    raw_artifact_sha256: str,
    database_path: Path,
    created_at: str,
) -> dict[str, object]:
    digest, size = _artifact_provenance(database_path)
    return {
        "artifactSchemaVersion": ARTIFACT_SCHEMA_VERSION,
        "providerID": provider_id,
        "semanticKey": semantic_key,
        "rawArtifactSHA256": raw_artifact_sha256 or None,
        "fileFingerprints": semantic.file_fingerprints,
        "rowCounts": semantic.row_counts,
        "fileColumns": semantic.file_columns,
        "normalizedSchemaFingerprint": _normalized_schema_fingerprint(),
        "builderFingerprint": builder,
        "createdAt": created_at,
        "status": "complete",
        "normalizedSQLite": {
            "path": "normalized.sqlite",
            "sha256": digest,
            "size": size,
        },
    }


def _validate_manifest(
    manifest: object,
    *,
    directory: Path,
    provider_id: str,
    semantic_key: str,
    builder: str,
    expected_semantic: SemanticManifest | None = None,
    expected_raw_sha256: str | None = None,
) -> dict[str, object]:
    if not isinstance(manifest, dict):
        raise NormalizedArtifactError("manifest is not an object")
    if manifest.get("artifactSchemaVersion") != ARTIFACT_SCHEMA_VERSION:
        raise NormalizedArtifactError("artifact schema version mismatch")
    if manifest.get("providerID") != provider_id:
        raise NormalizedArtifactError("provider ID mismatch")
    if manifest.get("semanticKey") != semantic_key:
        raise NormalizedArtifactError("semantic key mismatch")
    if expected_raw_sha256 and manifest.get("rawArtifactSHA256") != expected_raw_sha256:
        raise NormalizedArtifactError("raw artifact provenance mismatch")
    if manifest.get("normalizedSchemaFingerprint") != _normalized_schema_fingerprint():
        raise NormalizedArtifactError("normalized schema fingerprint mismatch")
    if manifest.get("builderFingerprint") != builder:
        raise NormalizedArtifactError("normalized builder fingerprint mismatch")
    if manifest.get("status") != "complete":
        raise NormalizedArtifactError("artifact is not complete")
    fingerprints = manifest.get("fileFingerprints")
    row_counts = manifest.get("rowCounts")
    if not isinstance(fingerprints, dict) or set(fingerprints) != set(NORMALIZED_FILES):
        raise NormalizedArtifactError("file fingerprints are incomplete")
    if not isinstance(row_counts, dict) or set(row_counts) != set(NORMALIZED_FILES):
        raise NormalizedArtifactError("row counts are incomplete")
    file_columns = manifest.get("fileColumns")
    if not isinstance(file_columns, dict) or set(file_columns) != set(NORMALIZED_FILES):
        raise NormalizedArtifactError("file columns are incomplete")
    for filename in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
        columns = file_columns.get(filename)
        if not isinstance(columns, list) or not columns:
            raise NormalizedArtifactError(
                f"required normalized source file is absent: {filename}"
            )
    if expected_semantic is not None:
        if fingerprints != expected_semantic.file_fingerprints:
            raise NormalizedArtifactError("semantic file fingerprints mismatch")
        if row_counts != expected_semantic.row_counts:
            raise NormalizedArtifactError("semantic row counts mismatch")
        if manifest.get("fileColumns") != expected_semantic.file_columns:
            raise NormalizedArtifactError("semantic file columns mismatch")
    database = manifest.get("normalizedSQLite")
    if not isinstance(database, dict) or database.get("path") != "normalized.sqlite":
        raise NormalizedArtifactError("normalized SQLite provenance is invalid")
    database_path = directory / "normalized.sqlite"
    if not database_path.is_file():
        raise NormalizedArtifactError("normalized SQLite is missing")
    digest, size = _artifact_provenance(database_path)
    if digest != database.get("sha256") or size != database.get("size"):
        raise NormalizedArtifactError("normalized SQLite provenance mismatch")
    _validate_sqlite(database_path)
    return manifest


def _read_raw_index(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise NormalizedArtifactError(f"raw SHA index is unreadable: {error}") from error
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        raise NormalizedArtifactError("raw SHA index is invalid")
    return payload


def _write_raw_index(path: Path, values: dict[str, str]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(values, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_existing_for_raw_sha(
    *,
    repository_root: Path,
    provider_id: str,
    raw_artifact_sha256: str,
    gtfs_cache_root: Path | None,
    environ: dict[str, str] | None = None,
) -> tuple[object, NormalizedArtifactUse]:
    """Load a previously published artifact without opening the GTFS archive."""
    try:
        from .external_staging import NormalizedProviderContext
    except ImportError:
        from external_staging import NormalizedProviderContext

    if not provider_enabled(provider_id, repository_root):
        raise NormalizedArtifactError(f"provider is not enabled: {provider_id}")
    raw_sha = str(raw_artifact_sha256 or "").strip()
    if not raw_sha:
        raise NormalizedArtifactError("raw artifact SHA-256 is unavailable")
    started = time.monotonic()
    provider_root = default_cache_root(
        gtfs_cache_root=gtfs_cache_root,
        environ=environ,
    ) / provider_id
    raw_index = _read_raw_index(provider_root / "raw-sha-index.json")
    semantic_key = raw_index.get(raw_sha)
    if not semantic_key:
        raise NormalizedArtifactError(
            "persistent normalized artifact is absent for raw artifact SHA-256"
        )
    directory = provider_root / semantic_key
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        validated = _validate_manifest(
            manifest,
            directory=directory,
            provider_id=provider_id,
            semantic_key=semantic_key,
            builder=builder_fingerprint(repository_root),
            expected_raw_sha256=raw_sha,
        )
    except (OSError, TypeError, ValueError, NormalizedArtifactError) as error:
        raise NormalizedArtifactError(
            f"persistent normalized artifact INVALID: {error}"
        ) from error
    database_path = directory / "normalized.sqlite"
    context = NormalizedProviderContext.from_database(database_path)
    return context, NormalizedArtifactUse(
        status="HIT",
        reason="raw SHA index matched; persistent artifact validated",
        semantic_key=semantic_key,
        artifact_directory=directory,
        database_path=database_path,
        manifest=validated,
        duration_seconds=time.monotonic() - started,
    )


def load_or_build(
    *,
    archive,
    repository_root: Path,
    provider_id: str,
    raw_artifact_sha256: str | None,
    gtfs_cache_root: Path | None,
    environ: dict[str, str] | None = None,
) -> tuple[object, NormalizedArtifactUse]:
    """Load or atomically build the selected provider's normalized artifact."""
    try:
        from .external_staging import NormalizedProviderContext
    except ImportError:
        from external_staging import NormalizedProviderContext

    if not provider_enabled(provider_id, repository_root):
        raise NormalizedArtifactError(f"provider is not enabled: {provider_id}")
    cache_root = default_cache_root(
        gtfs_cache_root=gtfs_cache_root,
        environ=environ,
    )
    provider_root = cache_root / provider_id
    provider_root.mkdir(parents=True, exist_ok=True)
    builder = builder_fingerprint(repository_root)
    raw_sha = str(raw_artifact_sha256 or "")
    started = time.monotonic()

    raw_index_path = provider_root / "raw-sha-index.json"
    raw_index = _read_raw_index(raw_index_path)
    indexed_key = raw_index.get(raw_sha) if raw_sha else None
    if indexed_key:
        indexed_directory = provider_root / indexed_key
        manifest_path = indexed_directory / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            validated = _validate_manifest(
                manifest,
                directory=indexed_directory,
                provider_id=provider_id,
                semantic_key=indexed_key,
                builder=builder,
                expected_raw_sha256=raw_sha,
            )
        except (OSError, TypeError, ValueError, NormalizedArtifactError) as error:
            raise NormalizedArtifactError(
                f"normalized artifact INVALID via raw SHA index: {error}"
            ) from error
        context = NormalizedProviderContext.from_database(
            indexed_directory / "normalized.sqlite"
        )
        return context, NormalizedArtifactUse(
            status="HIT",
            reason="raw SHA index matched; semantic manifest validated",
            semantic_key=indexed_key,
            artifact_directory=indexed_directory,
            database_path=indexed_directory / "normalized.sqlite",
            manifest=validated,
            duration_seconds=time.monotonic() - started,
        )

    semantic = _semantic_manifest(archive)
    semantic_key = _semantic_key(
        provider_id=provider_id,
        builder=builder,
        semantic=semantic,
    )
    directory = provider_root / semantic_key
    manifest_path = directory / "manifest.json"
    if directory.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            validated = _validate_manifest(
                manifest,
                directory=directory,
                provider_id=provider_id,
                semantic_key=semantic_key,
                builder=builder,
                expected_semantic=semantic,
            )
        except (OSError, TypeError, ValueError, NormalizedArtifactError) as error:
            raise NormalizedArtifactError(
                f"normalized artifact INVALID for semantic key {semantic_key[:12]}: {error}"
            ) from error
        context = NormalizedProviderContext.from_database(directory / "normalized.sqlite")
        if raw_sha:
            raw_index[raw_sha] = semantic_key
            _write_raw_index(raw_index_path, raw_index)
        return context, NormalizedArtifactUse(
            status="HIT",
            reason="semantic manifest matched",
            semantic_key=semantic_key,
            artifact_directory=directory,
            database_path=directory / "normalized.sqlite",
            manifest=validated,
            duration_seconds=time.monotonic() - started,
        )

    temporary = Path(tempfile.mkdtemp(prefix=f".{semantic_key}.tmp-", dir=provider_root))
    database_path = temporary / "normalized.sqlite"
    try:
        context = NormalizedProviderContext.from_archive(
            archive,
            database_path=database_path,
        )
        context.close()
        manifest = _manifest_for(
            provider_id=provider_id,
            semantic_key=semantic_key,
            semantic=semantic,
            builder=builder,
            raw_artifact_sha256=raw_sha,
            database_path=database_path,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        manifest_temporary = temporary / ".manifest.json.tmp"
        manifest_temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(manifest_temporary, temporary / "manifest.json")
        if directory.exists():
            raise NormalizedArtifactError(
                f"normalized artifact appeared concurrently: {semantic_key[:12]}"
            )
        os.replace(temporary, directory)
        temporary = Path()
        if raw_sha:
            raw_index[raw_sha] = semantic_key
            _write_raw_index(raw_index_path, raw_index)
        context = NormalizedProviderContext.from_database(directory / "normalized.sqlite")
        return context, NormalizedArtifactUse(
            status="MISS",
            reason="semantic key not found; artifact built and published",
            semantic_key=semantic_key,
            artifact_directory=directory,
            database_path=directory / "normalized.sqlite",
            manifest=manifest,
            duration_seconds=time.monotonic() - started,
        )
    finally:
        if temporary != Path():
            shutil.rmtree(temporary, ignore_errors=True)
