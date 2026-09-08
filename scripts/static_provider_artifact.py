#!/usr/bin/env python3
"""Immutable provider-local structural and temporal StaticDepartures artifacts."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import resource
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

try:
    from .artifact_provenance import artifact_provenance
    from .build_german_departure_index import (
        connect,
        populate_gtfs,
        resolve_canonical_stops,
        update_terminal_stops,
    )
    from .static_departures_ownership import register_city_mode
except ImportError:
    from artifact_provenance import artifact_provenance
    from build_german_departure_index import (
        connect,
        populate_gtfs,
        resolve_canonical_stops,
        update_terminal_stops,
    )
    from static_departures_ownership import register_city_mode

try:
    from .provider_artifact_capabilities import STATIC_PROVIDER, provider_capability
except ImportError:
    from provider_artifact_capabilities import STATIC_PROVIDER, provider_capability


ISRAEL_PROVIDER_ID = "israel-mot"
EXPERIMENTAL_FEATURE_GATE = "HALTEWECKER_STATIC_PROVIDER_ARTIFACT_EXPERIMENTAL"
ARTIFACT_ROOT_ENV = "HALTEWECKER_STATIC_PROVIDER_ARTIFACT_ROOT"
STRUCTURAL_SCHEMA_VERSION = 1
TEMPORAL_SCHEMA_VERSION = 1

STRUCTURAL_TABLES = (
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
)
TEMPORAL_TABLES = ("active_services",)

STRUCTURAL_INDEXES = (
    "stop_times_by_trip",
    "stop_times_by_stop_departure",
    "provider_entities_by_provider",
    "provider_entities_by_type_key",
    "provider_city_stops_by_city_stop",
)

STRUCTURAL_COLUMNS = {
    "agencies": ("agency_id", "agency_name"),
    "raw_stops": (
        "stop_id", "parent_station", "stop_name", "platform_code",
        "location_type", "stop_desc", "platform_display", "floor_display",
        "source_order", "canonical_stop_id",
    ),
    "routes": ("route_id", "short_name", "long_name", "route_type", "agency_id"),
    "trips": (
        "trip_id", "service_id", "route_id", "headsign", "direction_id",
        "terminal_stop_id",
    ),
    "calendar": (
        "service_id", "start_date", "end_date", "monday", "tuesday",
        "wednesday", "thursday", "friday", "saturday", "sunday",
    ),
    "calendar_dates": ("service_id", "service_date", "exception_type"),
    "stop_times": (
        "trip_id", "raw_stop_id", "arrival_time", "departure_time",
        "departure_seconds", "stop_sequence",
    ),
    "transfers": (
        "from_stop_id", "to_stop_id", "from_trip_id", "to_trip_id",
        "from_route_id", "to_route_id", "transfer_type", "min_transfer_time",
    ),
    "pathways": (
        "pathway_id", "from_stop_id", "to_stop_id", "pathway_mode",
        "is_bidirectional", "length", "traversal_time", "stair_count",
        "max_slope", "min_width", "signposted_as", "reversed_signposted_as",
    ),
    "provider_entities": ("entity_type", "provider_id", "key_1", "key_2", "key_3"),
    "provider_city_stops": ("provider_id", "city_id", "stop_id"),
    "provider_city_modes": (
        "provider_id", "city_id", "mode", "timezone", "stop_id_prefix",
        "identifier_prefix",
    ),
    "ownership_metadata": ("key", "value"),
}

TEMPORAL_COLUMNS = {"active_services": ("service_id", "service_date")}

STRUCTURAL_BUILDER_SYMBOLS = {
    "scripts/build_german_departure_index.py": (
        "connect",
        "populate_gtfs",
        "resolve_canonical_stops",
        "update_terminal_stops",
    ),
    "scripts/import_static_departures_database.py": (
        "CityScopedStopIDPrefixes",
        "populate_provider_city_memberships",
        "_populate_provider_city_memberships_indexed",
    ),
    "scripts/static_departures_ownership.py": (
        "register_city_mode",
        "register_city_stops",
    ),
    "scripts/static_provider_artifact.py": (
        "STRUCTURAL_TABLES",
        "STRUCTURAL_INDEXES",
        "STRUCTURAL_COLUMNS",
        "_structural_schema_fingerprint",
        "_projection_config_fingerprint",
        "_provider_city_prefixes",
        "_stop_data_fingerprint",
        "_structural_key",
        "_build_structural_database",
    ),
}

TEMPORAL_BUILDER_SYMBOLS = {
    "scripts/static_provider_artifact.py": (
        "TEMPORAL_TABLES",
        "TEMPORAL_COLUMNS",
        "_temporal_schema_fingerprint",
        "_temporal_key",
        "_build_temporal_database",
    ),
}


class StaticProviderArtifactError(ValueError):
    """Raised when an immutable provider artifact cannot be trusted."""


@dataclass(frozen=True)
class StaticProviderArtifactUse:
    status: str
    reason: str
    artifact_key: str
    artifact_directory: Path
    database_path: Path
    manifest: dict[str, object]
    size: int
    bytes_written: int
    duration_seconds: float


@dataclass(frozen=True)
class StaticProviderArtifacts:
    structural: StaticProviderArtifactUse
    temporal: StaticProviderArtifactUse


def feature_enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = environ if environ is not None else os.environ
    return values.get(EXPERIMENTAL_FEATURE_GATE, "0").strip() == "1"


def default_artifact_root(environ: Mapping[str, str] | None = None) -> Path:
    values = environ if environ is not None else os.environ
    configured = values.get(ARTIFACT_ROOT_ENV, "").strip()
    if configured:
        return Path(configured)
    normalized_root = values.get("HALTEWECKER_NORMALIZED_PROVIDER_CACHE_ROOT", "").strip()
    if normalized_root:
        return Path(normalized_root).parent / "static-provider-artifacts"
    gtfs_cache_root = values.get("GTFS_CACHE_ROOT", "").strip()
    if gtfs_cache_root:
        return Path(gtfs_cache_root).parent / "static-provider-artifacts"
    return Path("cache") / "static-provider-artifacts"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_payload(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        values = [_canonical_value(item) for item in value]
        return sorted(values, key=lambda item: _canonical_json(item))
    return value


def _ast_symbol_payload(path: Path, names: Iterable[str]) -> list[dict[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    top_level: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_level[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                if isinstance(target, ast.Name):
                    top_level[target.id] = node
    payload: list[dict[str, str]] = []
    missing: list[str] = []
    for name in names:
        if "." in name:
            class_name, method_name = name.split(".", 1)
            class_node = top_level.get(class_name)
            node = None
            if isinstance(class_node, ast.ClassDef):
                node = next(
                    (
                        child
                        for child in class_node.body
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and child.name == method_name
                    ),
                    None,
                )
        else:
            node = top_level.get(name)
        if node is None:
            missing.append(name)
            continue
        payload.append(
            {
                "name": name,
                "ast": ast.dump(node, annotate_fields=True, include_attributes=False),
            }
        )
    if missing:
        raise StaticProviderArtifactError(
            f"Missing static artifact builder symbols in {path}: {', '.join(missing)}"
        )
    return payload


def _builder_fingerprint(repository_root: Path, *, temporal: bool = False) -> str:
    payload: list[object] = []
    symbols = TEMPORAL_BUILDER_SYMBOLS if temporal else STRUCTURAL_BUILDER_SYMBOLS
    for relative, names in sorted(symbols.items()):
        path = repository_root / relative
        if not path.is_file():
            raise StaticProviderArtifactError(
                f"Missing static artifact builder input: {relative}"
            )
        payload.append(
            {
                "path": relative,
                "symbols": _ast_symbol_payload(path, names),
            }
        )
    return _sha256_payload(payload)


def _structural_schema_fingerprint() -> str:
    return _sha256_payload(
        {
            "schemaVersion": STRUCTURAL_SCHEMA_VERSION,
            "tables": STRUCTURAL_TABLES,
            "indexes": STRUCTURAL_INDEXES,
            "columns": STRUCTURAL_COLUMNS,
            "terminalTieRule": "max stop_sequence then max stop_times.rowid",
        }
    )


def _temporal_schema_fingerprint() -> str:
    return _sha256_payload(
        {
            "schemaVersion": TEMPORAL_SCHEMA_VERSION,
            "tables": TEMPORAL_TABLES,
            "columns": TEMPORAL_COLUMNS,
        }
    )


def _projection_config_fingerprint(
    source: Mapping[str, object],
    cities: list[dict[str, object]],
) -> str:
    relevant_source = {
        key: source.get(key)
        for key in (
            "id",
            "identifierPrefix",
            "staticIdentifierPrefix",
            "staticStopIDPrefix",
            "namespace",
            "staticDepartureMode",
            "timezone",
        )
    }
    return _sha256_payload(
        {
            "source": _canonical_value(relevant_source),
            "cities": _canonical_value(cities),
        }
    )


def _provider_city_prefixes(
    repository_root: Path,
    provider_id: str,
    cities: list[dict[str, object]],
    source: Mapping[str, object],
) -> tuple[dict[str, list[dict[str, object]]], dict[str, str]]:
    registry_path = repository_root / "config" / "external-gtfs-sources.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StaticProviderArtifactError(
            f"Cannot read external GTFS source registry: {registry_path}"
        ) from error
    if not isinstance(registry, list):
        raise StaticProviderArtifactError("External GTFS source registry must be a list")
    sources = {
        str(item.get("id")): item
        for item in registry
        if isinstance(item, Mapping) and item.get("id")
    }
    provider_cities: dict[str, list[dict[str, object]]] = {}
    prefixes: dict[str, str] = {}
    for city in cities:
        configured_providers = city.get("externalGTFSProviders")
        if not isinstance(configured_providers, list):
            configured_provider = city.get("externalGTFSProvider")
            configured_providers = [configured_provider] if configured_provider else []
        for configured_provider in configured_providers:
            configured_id = str(configured_provider or "").strip()
            configured_source = sources.get(configured_id)
            if not configured_id or not isinstance(configured_source, Mapping):
                continue
            prefix = str(
                configured_source.get("staticStopIDPrefix")
                or configured_source.get("namespace")
                or configured_source.get("identifierPrefix")
                or ""
            )
            if not prefix:
                raise StaticProviderArtifactError(
                    f"Provider {configured_id} has no static stop ID prefix"
                )
            provider_cities.setdefault(configured_id, []).append(city)
            prefixes[configured_id] = prefix
    current_cities = provider_cities.setdefault(provider_id, [])
    known_city_ids = {str(city.get("id", "")) for city in current_cities}
    current_cities.extend(
        city for city in cities if str(city.get("id", "")) not in known_city_ids
    )
    current_prefix = str(
        source.get("staticStopIDPrefix")
        or source.get("namespace")
        or source.get("identifierPrefix")
        or ""
    )
    if not current_prefix:
        raise StaticProviderArtifactError(
            f"Provider {provider_id} has no static stop ID prefix"
        )
    prefixes[provider_id] = current_prefix
    return provider_cities, prefixes


def _stop_data_fingerprint(stop_data: Path, city_ids: set[str]) -> str:
    manifest_path = stop_data / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StaticProviderArtifactError(
            f"Cannot read stop-data manifest: {manifest_path}"
        ) from error
    cities = manifest.get("cities") if isinstance(manifest, dict) else None
    if not isinstance(cities, list):
        raise StaticProviderArtifactError("Stop-data manifest must contain cities")
    packages: list[object] = []
    for city in cities:
        if not isinstance(city, Mapping) or str(city.get("id", "")) not in city_ids:
            continue
        relative = str(city.get("url", ""))
        package_path = stop_data / relative
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StaticProviderArtifactError(
                f"Cannot read stop-data package: {package_path}"
            ) from error
        packages.append(
            {
                "city": _canonical_value(dict(city)),
                "package": _canonical_value(package),
            }
        )
    return _sha256_payload(
        {
            "releaseID": manifest.get("releaseID"),
            "version": manifest.get("version"),
            "cities": packages,
        }
    )


def _structural_key(
    *,
    provider_id: str,
    normalized_semantic_key: str,
    projection_config_fingerprint: str,
    stop_data_fingerprint: str,
    builder_fingerprint: str,
    structural_schema_fingerprint: str,
) -> str:
    return _sha256_payload(
        {
            "providerID": provider_id,
            "structuralSchemaVersion": STRUCTURAL_SCHEMA_VERSION,
            "normalizedArtifactSemanticKey": normalized_semantic_key,
            "providerProjectionConfigFingerprint": projection_config_fingerprint,
            "stopDataFingerprint": stop_data_fingerprint,
            "staticImporterFingerprint": builder_fingerprint,
            "structuralSchemaFingerprint": structural_schema_fingerprint,
        }
    )


def _temporal_key(
    *,
    provider_id: str,
    structural_key: str,
    timezone: str,
    valid_from: date,
    valid_through: date,
    calendar_fingerprints: Mapping[str, object],
    builder_fingerprint: str,
    temporal_schema_fingerprint: str,
) -> str:
    return _sha256_payload(
        {
            "providerID": provider_id,
            "temporalSchemaVersion": TEMPORAL_SCHEMA_VERSION,
            "structuralArtifactKey": structural_key,
            "timezone": timezone,
            "validFrom": valid_from.isoformat(),
            "validThrough": valid_through.isoformat(),
            "calendarSemantics": _canonical_value(calendar_fingerprints),
            "temporalBuilderFingerprint": builder_fingerprint,
            "temporalSchemaFingerprint": temporal_schema_fingerprint,
        }
    )


def _table_rows(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _schema_info(connection: sqlite3.Connection) -> tuple[set[str], set[str]]:
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
    return tables, indexes


def _validate_database(
    database_path: Path,
    *,
    required_tables: tuple[str, ...],
    required_indexes: tuple[str, ...] = (),
    expected_columns: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, int]:
    if not database_path.is_file():
        raise StaticProviderArtifactError(f"Missing artifact database: {database_path}")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check != ("ok",):
            raise StaticProviderArtifactError(
                f"SQLite quick_check failed for {database_path}: {quick_check!r}"
            )
        tables, indexes = _schema_info(connection)
        missing_tables = set(required_tables) - tables
        missing_indexes = set(required_indexes) - indexes
        if missing_tables or missing_indexes:
            raise StaticProviderArtifactError(
                f"Artifact schema incomplete: tables={sorted(missing_tables)} "
                f"indexes={sorted(missing_indexes)}"
            )
        for table, expected in (expected_columns or {}).items():
            actual = tuple(
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual != expected:
                raise StaticProviderArtifactError(
                    f"Artifact column schema mismatch for {table}: "
                    f"expected={expected!r} actual={actual!r}"
                )
        return {table: _table_rows(connection, table) for table in required_tables}
    except sqlite3.DatabaseError as error:
        raise StaticProviderArtifactError(
            f"Cannot validate artifact database {database_path}: {error}"
        ) from error
    finally:
        if connection is not None:
            connection.close()


def _manifest_provenance(database_path: Path) -> tuple[str, int]:
    return artifact_provenance(database_path)


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _validate_manifest(
    directory: Path,
    *,
    expected_artifact_key: str,
    expected_provider_id: str,
    expected_type: str,
    required_tables: tuple[str, ...],
    required_indexes: tuple[str, ...],
    expected_schema_version: int,
    expected_dependencies: Mapping[str, object],
    expected_fields: Mapping[str, object] | None = None,
) -> dict[str, object]:
    manifest_path = directory / "manifest.json"
    database_path = directory / "provider.sqlite"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise StaticProviderArtifactError(
            f"Artifact manifest is invalid: {manifest_path}"
        ) from error
    if not isinstance(manifest, dict):
        raise StaticProviderArtifactError("Artifact manifest must be an object")
    checks = {
        "status": "complete",
        "artifactType": expected_type,
        "providerID": expected_provider_id,
        "artifactKey": expected_artifact_key,
    }
    for key, expected in checks.items():
        if manifest.get(key) != expected:
            raise StaticProviderArtifactError(
                f"Artifact manifest mismatch for {key}: "
                f"expected={expected!r} actual={manifest.get(key)!r}"
            )
    schema_key = (
        "structuralSchemaVersion"
        if expected_type == "structural"
        else "temporalSchemaVersion"
    )
    if manifest.get(schema_key) != expected_schema_version:
        raise StaticProviderArtifactError(
            f"Artifact manifest mismatch for {schema_key}: "
            f"expected={expected_schema_version!r} actual={manifest.get(schema_key)!r}"
        )
    for key, expected in (expected_fields or {}).items():
        if manifest.get(key) != expected:
            raise StaticProviderArtifactError(
                f"Artifact manifest mismatch for {key}: "
                f"expected={expected!r} actual={manifest.get(key)!r}"
            )
    dependencies = manifest.get("dependencies")
    if _canonical_value(dependencies) != _canonical_value(dict(expected_dependencies)):
        raise StaticProviderArtifactError(
            "Artifact dependency fingerprints do not match current inputs"
        )
    if not isinstance(manifest.get("dependencies"), dict):
        raise StaticProviderArtifactError("Artifact dependencies are missing")
    counts = _validate_database(
        database_path,
        required_tables=required_tables,
        required_indexes=required_indexes,
        expected_columns=(
            STRUCTURAL_COLUMNS if expected_type == "structural" else TEMPORAL_COLUMNS
        ),
    )
    provenance = manifest.get("sqlite")
    if not isinstance(provenance, dict):
        raise StaticProviderArtifactError("Artifact SQLite provenance is missing")
    digest, size = _manifest_provenance(database_path)
    if provenance.get("sha256") != digest or provenance.get("size") != size:
        raise StaticProviderArtifactError("Artifact SQLite provenance mismatch")
    row_counts = manifest.get("rowCounts")
    if row_counts != counts:
        raise StaticProviderArtifactError(
            f"Artifact row counts mismatch: expected={counts!r} actual={row_counts!r}"
        )
    return manifest


def _artifact_root_for(
    provider_id: str,
    repository_root: Path,
    environ: Mapping[str, str] | None,
) -> Path:
    if not provider_capability(repository_root, provider_id, STATIC_PROVIDER):
        raise StaticProviderArtifactError(
            f"Static provider artifacts are not enabled for {provider_id}"
        )
    return default_artifact_root(environ) / provider_id


def _source_prefix(source: Mapping[str, object], key: str) -> str:
    return str(
        source.get(key)
        or source.get("namespace", "").strip()
        or source.get("identifierPrefix", "")
    )


def _build_structural_database(
    database_path: Path,
    *,
    repository_root: Path,
    normalized_context,
    source: Mapping[str, object],
    cities: list[dict[str, object]],
    stop_data: Path,
    provider_id: str,
    city_ids: set[str],
) -> None:
    connection = connect(database_path)
    try:
        populate_gtfs(
            connection,
            normalized_context,
            identifier_prefix=str(source["identifierPrefix"]),
            stop_id_prefix=_source_prefix(source, "staticStopIDPrefix"),
            provider_id=provider_id,
        )
        resolve_canonical_stops(connection, provider_ids=(provider_id,))
        update_terminal_stops(connection, provider_ids=(provider_id,))

        from import_static_departures_database import (
            CityScopedStopIDPrefixes,
            populate_provider_city_memberships,
        )

        mode = str(source.get("staticDepartureMode", "canonical")).strip() or "canonical"
        # Multi-namespace city modes keep provider identifiers public. The legacy
        # merged database uses empty projection prefixes whenever a source owns
        # an explicit namespace; provider-local ownership still uses the source
        # namespace below for stop membership and internal joins.
        projection_stop_prefix = "" if str(source.get("namespace", "")).strip() else _source_prefix(source, "staticStopIDPrefix")
        projection_identifier_prefix = "" if str(source.get("namespace", "")).strip() else str(source.get("staticIdentifierPrefix", ""))
        for city in cities:
            city_id = str(city["id"])
            register_city_mode(
                connection,
                provider_id,
                city_id,
                mode,
                str(source["timezone"]),
                projection_stop_prefix,
                projection_identifier_prefix,
            )
        provider_cities, stop_prefixes = _provider_city_prefixes(
            repository_root,
            provider_id,
            cities,
            source,
        )
        city_prefixes = CityScopedStopIDPrefixes.from_authoritative_provider_cities(
            provider_cities,
            stop_prefixes,
        )
        populate_provider_city_memberships(
            connection,
            stop_data,
            included_city_ids=city_ids,
            stop_id_prefix_by_provider={
                provider_id: _source_prefix(source, "staticStopIDPrefix")
            },
            indexed_ownership_lookup=True,
            catalog_only_city_ids={
                str(city["id"])
                for city in cities
                if city.get("catalogOnly") is True
            },
            city_scoped_prefixes=city_prefixes,
        )
        connection.execute("DROP TABLE city_stops")
        connection.execute("DROP TABLE active_services")
        connection.commit()
    finally:
        connection.close()


def _build_temporal_database(
    database_path: Path,
    *,
    structural_database_path: Path,
    dates: list[date],
) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            CREATE TABLE active_services (
                service_id TEXT NOT NULL,
                service_date TEXT NOT NULL,
                PRIMARY KEY(service_id, service_date)
            ) WITHOUT ROWID
            """
        )
        connection.execute(
            f"ATTACH DATABASE ? AS structural",
            (str(structural_database_path),),
        )
        day_columns = (
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        )
        for service_date in dates:
            compact_date = service_date.strftime("%Y%m%d")
            day_column = day_columns[service_date.weekday()]
            connection.execute(
                f"""
                INSERT OR IGNORE INTO active_services(service_id, service_date)
                SELECT service_id, ?
                FROM structural.calendar
                WHERE start_date <= ? AND end_date >= ? AND {day_column}=1
                  AND NOT EXISTS (
                      SELECT 1 FROM structural.calendar_dates overrides
                      WHERE overrides.service_id=structural.calendar.service_id
                        AND overrides.service_date=?
                        AND overrides.exception_type=2
                  )
                """,
                (compact_date, compact_date, compact_date, compact_date),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO active_services(service_id, service_date)
                SELECT service_id, service_date
                FROM structural.calendar_dates
                WHERE service_date=? AND exception_type=1
                """,
                (compact_date,),
            )
        connection.commit()
        connection.execute("DETACH DATABASE structural")
    finally:
        connection.close()


def _structural_manifest(
    *,
    provider_id: str,
    artifact_key: str,
    normalized_semantic_key: str,
    projection_config_fingerprint: str,
    stop_data_fingerprint: str,
    builder_fingerprint: str,
    database_path: Path,
) -> dict[str, object]:
    digest, size = _manifest_provenance(database_path)
    row_counts = _validate_database(
        database_path,
        required_tables=STRUCTURAL_TABLES,
        required_indexes=STRUCTURAL_INDEXES,
        expected_columns=STRUCTURAL_COLUMNS,
    )
    return {
        "artifactType": "structural",
        "structuralSchemaVersion": STRUCTURAL_SCHEMA_VERSION,
        "providerID": provider_id,
        "artifactKey": artifact_key,
        "normalizedArtifactSemanticKey": normalized_semantic_key,
        "dependencies": {
            "providerProjectionConfigFingerprint": projection_config_fingerprint,
            "stopDataFingerprint": stop_data_fingerprint,
            "staticImporterFingerprint": builder_fingerprint,
            "structuralSchemaFingerprint": _structural_schema_fingerprint(),
        },
        "status": "complete",
        "rowCounts": row_counts,
        "sqlite": {"path": "provider.sqlite", "sha256": digest, "size": size},
    }


def _temporal_manifest(
    *,
    provider_id: str,
    artifact_key: str,
    structural_key: str,
    timezone: str,
    valid_from: date,
    valid_through: date,
    calendar_fingerprints: Mapping[str, object],
    builder_fingerprint: str,
    database_path: Path,
) -> dict[str, object]:
    digest, size = _manifest_provenance(database_path)
    row_counts = _validate_database(
        database_path,
        required_tables=TEMPORAL_TABLES,
        expected_columns=TEMPORAL_COLUMNS,
    )
    return {
        "artifactType": "temporal",
        "temporalSchemaVersion": TEMPORAL_SCHEMA_VERSION,
        "providerID": provider_id,
        "artifactKey": artifact_key,
        "structuralArtifactKey": structural_key,
        "dependencies": {
            "timezone": timezone,
            "validFrom": valid_from.isoformat(),
            "validThrough": valid_through.isoformat(),
            "calendarSemantics": _canonical_value(calendar_fingerprints),
            "temporalBuilderFingerprint": builder_fingerprint,
            "temporalSchemaFingerprint": _temporal_schema_fingerprint(),
        },
        "status": "complete",
        "rowCounts": row_counts,
        "sqlite": {"path": "provider.sqlite", "sha256": digest, "size": size},
    }


def _publish_directory(temporary: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise StaticProviderArtifactError(
            f"Artifact destination appeared during publication: {destination}"
        )
    os.replace(temporary, destination)


def _load_or_build_one(
    *,
    artifact_key: str,
    artifact_directory: Path,
    expected_provider_id: str,
    artifact_type: str,
    required_tables: tuple[str, ...],
    required_indexes: tuple[str, ...],
    expected_schema_version: int,
    expected_dependencies: Mapping[str, object],
    log_stage: str,
    expected_fields: Mapping[str, object] | None = None,
    manifest_builder,
    database_builder,
) -> StaticProviderArtifactUse:
    started = time.monotonic()
    database_path = artifact_directory / "provider.sqlite"
    if artifact_directory.exists():
        try:
            manifest = _validate_manifest(
                artifact_directory,
                expected_artifact_key=artifact_key,
                expected_provider_id=expected_provider_id,
                expected_type=artifact_type,
                required_tables=required_tables,
                required_indexes=required_indexes,
                expected_schema_version=expected_schema_version,
                expected_dependencies=expected_dependencies,
                expected_fields=expected_fields,
            )
        except StaticProviderArtifactError as error:
            print(
                f"[StaticDepartures] source={expected_provider_id} "
                f"stage={log_stage} status=INVALID "
                f"reason={type(error).__name__}:{error} duration={time.monotonic() - started:.4f}s "
                f"rssBytes={_peak_rss_bytes()}",
                flush=True,
            )
            raise
        size = int(manifest["sqlite"]["size"])
        print(
            f"[StaticDepartures] source={expected_provider_id} "
            f"stage={log_stage} status=HIT reason=validated "
            f"duration={time.monotonic() - started:.4f}s artifact_key={artifact_key[:12]} "
            f"size={size} bytesWritten=0 rssBytes={_peak_rss_bytes()}",
            flush=True,
        )
        return StaticProviderArtifactUse(
            status="HIT",
            reason="validated immutable artifact",
            artifact_key=artifact_key,
            artifact_directory=artifact_directory,
            database_path=database_path,
            manifest=manifest,
            size=size,
            bytes_written=0,
            duration_seconds=time.monotonic() - started,
        )

    temporary = artifact_directory.parent / f".{artifact_directory.name}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        database_builder(temporary / "provider.sqlite")
        manifest = manifest_builder(temporary / "provider.sqlite")
        _write_json_atomic(temporary / "manifest.json", manifest)
        size = int(manifest["sqlite"]["size"])
        bytes_written = sum(
            path.stat().st_size
            for path in temporary.rglob("*")
            if path.is_file()
        )
        _publish_directory(temporary, artifact_directory)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(
        f"[StaticDepartures] source={expected_provider_id} "
        f"stage={log_stage} status=MISS reason=cold-build "
        f"duration={time.monotonic() - started:.4f}s artifact_key={artifact_key[:12]} "
        f"size={size} bytesWritten={bytes_written} rssBytes={_peak_rss_bytes()}",
        flush=True,
    )
    return StaticProviderArtifactUse(
        status="MISS",
        reason="cold-build",
        artifact_key=artifact_key,
        artifact_directory=artifact_directory,
        database_path=artifact_directory / "provider.sqlite",
        manifest=manifest,
        size=size,
        bytes_written=bytes_written,
        duration_seconds=time.monotonic() - started,
    )


def load_or_build_static_provider_artifacts(
    *,
    normalized_context,
    normalized_artifact,
    repository_root: Path,
    provider_id: str,
    source: Mapping[str, object],
    cities: list[dict[str, object]],
    stop_data: Path,
    dates: list[date],
    environ: Mapping[str, str] | None = None,
) -> StaticProviderArtifacts:
    if not provider_capability(repository_root, provider_id, STATIC_PROVIDER):
        raise StaticProviderArtifactError(
            f"Static provider artifacts are not enabled for {provider_id}"
        )
    if not dates:
        raise StaticProviderArtifactError("Temporal provider artifact requires dates")
    normalized_key = str(normalized_artifact.semantic_key)
    city_ids = {str(city["id"]) for city in cities}
    projection_fingerprint = _projection_config_fingerprint(source, cities)
    stop_data_fingerprint = _stop_data_fingerprint(stop_data, city_ids)
    structural_builder = _builder_fingerprint(repository_root)
    structural_schema = _structural_schema_fingerprint()
    structural_dependencies = {
        "providerProjectionConfigFingerprint": projection_fingerprint,
        "stopDataFingerprint": stop_data_fingerprint,
        "staticImporterFingerprint": structural_builder,
        "structuralSchemaFingerprint": structural_schema,
    }
    structural_key = _structural_key(
        provider_id=provider_id,
        normalized_semantic_key=normalized_key,
        projection_config_fingerprint=projection_fingerprint,
        stop_data_fingerprint=stop_data_fingerprint,
        builder_fingerprint=structural_builder,
        structural_schema_fingerprint=structural_schema,
    )
    root = _artifact_root_for(provider_id, repository_root, environ)
    structural_directory = root / structural_key
    structural = _load_or_build_one(
        artifact_key=structural_key,
        artifact_directory=structural_directory,
        expected_provider_id=provider_id,
        artifact_type="structural",
        required_tables=STRUCTURAL_TABLES,
        required_indexes=STRUCTURAL_INDEXES,
        expected_schema_version=STRUCTURAL_SCHEMA_VERSION,
        expected_dependencies=structural_dependencies,
        log_stage="static-provider-shard",
        expected_fields={"normalizedArtifactSemanticKey": normalized_key},
        manifest_builder=lambda database_path: _structural_manifest(
            provider_id=provider_id,
            artifact_key=structural_key,
            normalized_semantic_key=normalized_key,
            projection_config_fingerprint=projection_fingerprint,
            stop_data_fingerprint=stop_data_fingerprint,
            builder_fingerprint=structural_builder,
            database_path=database_path,
        ),
        database_builder=lambda database_path: _build_structural_database(
            database_path,
            repository_root=repository_root,
            normalized_context=normalized_context,
            source=source,
            cities=cities,
            stop_data=stop_data,
            provider_id=provider_id,
            city_ids=city_ids,
        ),
    )

    calendar_fingerprints = {
        name: normalized_artifact.manifest.get("fileFingerprints", {}).get(name)
        for name in ("calendar.txt", "calendar_dates.txt")
    }
    timezone = str(source["timezone"])
    temporal_builder = _builder_fingerprint(repository_root, temporal=True)
    temporal_schema = _temporal_schema_fingerprint()
    temporal_dependencies = {
        "timezone": timezone,
        "validFrom": min(dates).isoformat(),
        "validThrough": max(dates).isoformat(),
        "calendarSemantics": _canonical_value(calendar_fingerprints),
        "temporalBuilderFingerprint": temporal_builder,
        "temporalSchemaFingerprint": temporal_schema,
    }
    temporal_key = _temporal_key(
        provider_id=provider_id,
        structural_key=structural_key,
        timezone=timezone,
        valid_from=min(dates),
        valid_through=max(dates),
        calendar_fingerprints=calendar_fingerprints,
        builder_fingerprint=temporal_builder,
        temporal_schema_fingerprint=temporal_schema,
    )
    temporal_directory = structural_directory / "temporal" / temporal_key
    temporal = _load_or_build_one(
        artifact_key=temporal_key,
        artifact_directory=temporal_directory,
        expected_provider_id=provider_id,
        artifact_type="temporal",
        required_tables=TEMPORAL_TABLES,
        required_indexes=(),
        expected_schema_version=TEMPORAL_SCHEMA_VERSION,
        expected_dependencies=temporal_dependencies,
        log_stage="static-provider-temporal",
        expected_fields={"structuralArtifactKey": structural_key},
        manifest_builder=lambda database_path: _temporal_manifest(
            provider_id=provider_id,
            artifact_key=temporal_key,
            structural_key=structural_key,
            timezone=timezone,
            valid_from=min(dates),
            valid_through=max(dates),
            calendar_fingerprints=calendar_fingerprints,
            builder_fingerprint=temporal_builder,
            database_path=database_path,
        ),
        database_builder=lambda database_path: _build_temporal_database(
            database_path,
            structural_database_path=structural.database_path,
            dates=dates,
        ),
    )
    return StaticProviderArtifacts(structural=structural, temporal=temporal)


def validate_artifacts(
    artifacts: StaticProviderArtifacts,
    *,
    provider_id: str,
) -> None:
    _validate_manifest(
        artifacts.structural.artifact_directory,
        expected_artifact_key=artifacts.structural.artifact_key,
        expected_provider_id=provider_id,
        expected_type="structural",
        required_tables=STRUCTURAL_TABLES,
        required_indexes=STRUCTURAL_INDEXES,
        expected_schema_version=STRUCTURAL_SCHEMA_VERSION,
        expected_dependencies=artifacts.structural.manifest["dependencies"],
        expected_fields={
            "normalizedArtifactSemanticKey": artifacts.structural.manifest.get(
                "normalizedArtifactSemanticKey"
            )
        },
    )
    _validate_manifest(
        artifacts.temporal.artifact_directory,
        expected_artifact_key=artifacts.temporal.artifact_key,
        expected_provider_id=provider_id,
        expected_type="temporal",
        required_tables=TEMPORAL_TABLES,
        required_indexes=(),
        expected_schema_version=TEMPORAL_SCHEMA_VERSION,
        expected_dependencies=artifacts.temporal.manifest["dependencies"],
        expected_fields={
            "structuralArtifactKey": artifacts.temporal.manifest.get(
                "structuralArtifactKey"
            )
        },
    )
