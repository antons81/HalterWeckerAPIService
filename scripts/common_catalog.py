#!/usr/bin/env python3
"""Build the small release-wide catalog used by the provider shard runtime."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


COMMON_SCHEMA_VERSION = 1
COMMON_BUILDER_VERSION = "phase5b-common-catalog-v1"


@dataclass(frozen=True)
class CommonCatalogBuild:
    database_path: Path
    input_fingerprint: str
    row_counts: Mapping[str, int]
    size: int
    build_seconds: float


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonical(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _fingerprint(value: object) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _provider_row(provider_id: str, value: Mapping[str, object]) -> tuple[object, ...]:
    structural = value.get("structural")
    temporal = value.get("temporal")
    if not isinstance(structural, Mapping) or not isinstance(temporal, Mapping):
        raise ValueError(f"provider={provider_id} needs structural and temporal manifest data")
    return (
        provider_id,
        str(value.get("status", "active")),
        int(value.get("providerOrder", 0)),
        str(value.get("mergeGroup", "")),
        str(value.get("releaseID", "")),
        str(structural.get("artifactKey", "")),
        int(structural.get("schemaVersion", structural.get("structuralSchemaVersion", 0))),
        str(temporal.get("artifactKey", "")),
        int(temporal.get("schemaVersion", temporal.get("temporalSchemaVersion", 0))),
        str(value.get("validFrom", "")),
        str(value.get("validThrough", "")),
    )


def _normalized_alias_rows(aliases: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Normalize aliases while rejecting ambiguous canonical mappings."""
    canonical_targets: dict[str, set[str]] = {}
    for alias, canonical in aliases:
        alias_id = str(alias)
        canonical_id = str(canonical)
        canonical_targets.setdefault(alias_id, set()).add(canonical_id)

    rows: list[tuple[str, str]] = []
    for alias_id in sorted(canonical_targets):
        targets = sorted(canonical_targets[alias_id])
        if len(targets) != 1:
            raise ValueError(
                "conflicting city alias mapping: "
                f"alias_city_id={alias_id!r} canonical_city_ids={targets!r}"
            )
        rows.append((alias_id, targets[0]))
    return rows


def build_common_catalog(
    output_path: Path | str,
    *,
    release_id: str,
    providers: Mapping[str, Mapping[str, object]],
    aliases: Iterable[tuple[str, str]] = (),
    city_stops: Iterable[tuple[str, str]] = (),
    provider_city_stops: Iterable[tuple[str, str, str]] = (),
    provider_modes: Iterable[Mapping[str, object]] = (),
) -> CommonCatalogBuild:
    """Build a deterministic catalog from manifests and routing-only inputs.

    The function intentionally accepts already extracted config/stop-data rows.
    It never reads provider-owned GTFS rows and therefore cannot turn the common
    catalog into a second monolithic database.
    """
    output = Path(output_path)
    started = time.perf_counter()
    mode_rows = [
        (
            str(row["providerID"]),
            str(row["cityID"]),
            str(row.get("mode", "canonical")),
            str(row.get("timezone", "UTC")),
            str(row.get("stopIDPrefix", "")),
            str(row.get("identifierPrefix", "")),
        )
        for row in provider_modes
    ]
    provider_rows = [_provider_row(provider_id, providers[provider_id]) for provider_id in sorted(providers)]
    alias_rows = _normalized_alias_rows(aliases)
    city_stop_rows = sorted((str(city), str(stop)) for city, stop in city_stops)
    provider_city_stop_rows = sorted(
        (str(provider), str(city), str(stop))
        for provider, city, stop in provider_city_stops
    )
    mode_rows.sort(key=lambda row: (row[1], next((item[2] for item in provider_rows if item[0] == row[0]), 0), row[0]))
    inputs = {
        "schemaVersion": COMMON_SCHEMA_VERSION,
        "builderVersion": COMMON_BUILDER_VERSION,
        "releaseID": release_id,
        "providers": providers,
        "aliases": alias_rows,
        "cityStops": city_stop_rows,
        "providerCityStops": provider_city_stop_rows,
        "providerModes": mode_rows,
    }
    input_fingerprint = _fingerprint(inputs)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA auto_vacuum=NONE;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE provider_registry (
                provider_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                provider_order INTEGER NOT NULL,
                merge_group TEXT NOT NULL,
                release_id TEXT NOT NULL,
                structural_artifact_key TEXT NOT NULL,
                structural_schema_version INTEGER NOT NULL,
                temporal_artifact_key TEXT NOT NULL,
                temporal_schema_version INTEGER NOT NULL,
                valid_from TEXT NOT NULL,
                valid_through TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE city_aliases (
                alias_city_id TEXT PRIMARY KEY,
                canonical_city_id TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE city_stops (
                city_id TEXT NOT NULL,
                stop_id TEXT NOT NULL,
                PRIMARY KEY(city_id, stop_id)
            ) WITHOUT ROWID;
            CREATE TABLE provider_city_stops (
                provider_id TEXT NOT NULL,
                city_id TEXT NOT NULL,
                stop_id TEXT NOT NULL,
                PRIMARY KEY(provider_id, city_id, stop_id)
            ) WITHOUT ROWID;
            CREATE TABLE provider_city_modes (
                provider_id TEXT NOT NULL,
                city_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                timezone TEXT NOT NULL,
                stop_id_prefix TEXT NOT NULL,
                identifier_prefix TEXT NOT NULL,
                PRIMARY KEY(provider_id, city_id)
            ) WITHOUT ROWID;
            CREATE INDEX provider_city_modes_by_city
                ON provider_city_modes(city_id, provider_id);
            CREATE INDEX provider_city_stops_by_city_stop
                ON provider_city_stops(city_id, stop_id, provider_id);
            """
        )
        metadata = {
            "releaseID": release_id,
            "commonSchemaVersion": str(COMMON_SCHEMA_VERSION),
            "builderVersion": COMMON_BUILDER_VERSION,
            "inputFingerprint": input_fingerprint,
            "providerCount": str(len(provider_rows)),
        }
        connection.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", sorted(metadata.items()))
        connection.executemany(
            "INSERT INTO provider_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            provider_rows,
        )
        connection.executemany("INSERT INTO city_aliases VALUES (?, ?)", alias_rows)
        connection.executemany("INSERT INTO city_stops VALUES (?, ?)", city_stop_rows)
        connection.executemany("INSERT INTO provider_city_stops VALUES (?, ?, ?)", provider_city_stop_rows)
        connection.executemany("INSERT INTO provider_city_modes VALUES (?, ?, ?, ?, ?, ?)", mode_rows)
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    temporary.replace(output)
    row_counts = {}
    with sqlite3.connect(output) as check:
        for table in ("metadata", "provider_registry", "city_aliases", "city_stops", "provider_city_stops", "provider_city_modes"):
            row_counts[table] = int(check.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return CommonCatalogBuild(
        database_path=output,
        input_fingerprint=input_fingerprint,
        row_counts=row_counts,
        size=output.stat().st_size,
        build_seconds=time.perf_counter() - started,
    )


__all__ = [
    "COMMON_BUILDER_VERSION",
    "COMMON_SCHEMA_VERSION",
    "CommonCatalogBuild",
    "build_common_catalog",
]
