#!/usr/bin/env python3
"""Import German GTFS into an isolated, query-oriented SQLite snapshot."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from build_german_departure_index import (
    DEFAULT_TIMEZONE, connect, load_city_aliases,
    ImportStageRunner, populate_active_services, populate_gtfs, resolve_canonical_stops,
    service_window, update_terminal_stops,
)
from build_stop_packages import load_cities, load_gtfs_archive, nl_city_ids
from austrian_sources import DEFAULT_REGISTRY, load_austrian_sources, public_stop_id
from gtfs_agency import agency_scoped_archive

from external_gtfs import (
    authenticated_external_request,
    external_city_ids,
    load_external_cities,
    load_external_gtfs_sources,
    parse_external_gtfs_url_args,
    validate_external_gtfs_source,
)
from static_departures_ownership import (
    has_ownership_schema,
    rebuild_city_departure_modes,
    rebuild_city_stops,
    register_city_mode,
    register_city_stops,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def timed_stage(source: str, stage: str, callback):
    started = time.monotonic()
    print(
        f"[StaticDepartures] source={source} stage={stage} status=started",
        flush=True,
    )
    try:
        result = callback()
    except Exception:
        print(
            f"[StaticDepartures] source={source} stage={stage} status=failed "
            f"duration={time.monotonic() - started:.2f}s",
            flush=True,
        )
        raise
    print(
        f"[StaticDepartures] source={source} stage={stage} status=completed "
        f"duration={time.monotonic() - started:.2f}s",
        flush=True,
    )
    return result


def _external_window_timezone(
    sources_path: Path,
    url_by_provider: dict[str, str],
) -> str:
    """Pick the service-window timezone for an external import.

    Prefers the configured timezone of the sole external provider; falls back
    to the default Europe/Berlin window timezone otherwise.
    """
    sources = load_external_gtfs_sources(sources_path)
    if len(url_by_provider) == 1:
        provider_id = next(iter(url_by_provider))
        for source in sources:
            if str(source.get("id")) == provider_id:
                timezone_name = source.get("timezone")
                if isinstance(timezone_name, str) and timezone_name.strip():
                    return timezone_name.strip()
    return DEFAULT_TIMEZONE


def configured_external_city_ids(cities_path: Path, swiss_cities_path: Path) -> set[str]:
    cities = load_cities(cities_path)
    excluded = nl_city_ids(cities)
    excluded.update(
        str(city["id"])
        for city in cities
        if city.get("packageMode") in {"austrian", "external"}
    )
    if swiss_cities_path.exists():
        excluded.update(str(city["id"]) for city in load_cities(swiss_cities_path))
    external_sources_path = REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
    if external_sources_path.exists():
        excluded.update(
            external_city_ids(
                load_external_gtfs_sources(external_sources_path),
                REPOSITORY_ROOT,
            )
        )
    return excluded


def populate_city_memberships(
    connection: sqlite3.Connection,
    stop_data: Path,
    included_city_ids: set[str] | None = None,
    excluded_city_ids: set[str] | None = None,
    provider_id: str | None = None,
) -> set[str]:
    manifest_path = stop_data / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cities = manifest.get("cities")
    if not isinstance(cities, list):
        raise ValueError("Stop manifest must contain a cities array.")

    city_ids: set[str] = set()
    for city in cities:
        if not isinstance(city, dict) or not isinstance(city.get("id"), str):
            raise ValueError("Stop manifest contains an invalid city entry.")
        city_id = city["id"]
        if included_city_ids is not None and city_id not in included_city_ids:
            continue
        if excluded_city_ids and city_id in excluded_city_ids:
            continue
        if city_id in city_ids:
            raise ValueError(f"Conflicting canonical city ID in stop manifest: {city_id}")
        package_path = stop_data / str(city.get("url", ""))
        if not package_path.is_file():
            raise ValueError(f"Missing stop package for {city_id}: {package_path}")
        package = json.loads(package_path.read_text(encoding="utf-8"))
        if not isinstance(package, list):
            raise ValueError(f"Invalid stop package for {city_id}")
        memberships: list[tuple[str, str]] = []
        stop_ids: set[str] = set()
        for stop in package:
            if not isinstance(stop, dict) or not stop.get("id"):
                continue
            stop_ids.add(str(stop["id"]))
            stop_ids.update(str(alias) for alias in stop.get("sourceStopIDs", []) if alias)
        memberships.extend((city_id, stop_id) for stop_id in sorted(stop_ids))
        connection.executemany(
            "INSERT OR IGNORE INTO city_stops(city_id, stop_id) VALUES (?, ?)",
            memberships,
        )
        if provider_id is not None:
            register_city_stops(connection, provider_id, memberships)
        city_ids.add(city_id)
    connection.commit()
    return city_ids


def populate_german_city_memberships(
    connection: sqlite3.Connection,
    stop_data: Path,
    excluded_city_ids: set[str],
    provider_id: str = "germany",
    included_city_ids: set[str] | None = None,
) -> set[str]:
    return populate_city_memberships(
        connection,
        stop_data,
        included_city_ids=included_city_ids,
        excluded_city_ids=excluded_city_ids,
        provider_id=provider_id,
    )


def _supplemental_stop_provider(
    connection: sqlite3.Connection,
    city_id: str,
    stop: dict[str, object],
    prefix_by_provider: dict[str, str],
) -> str | None:
    """Resolve explicit ownership for an official catalog-only stop."""
    provider_id = str(stop.get("staticDepartureProviderID", "") or "").strip()
    if not provider_id:
        return None
    configured = provider_id in prefix_by_provider or connection.execute(
        """
        SELECT 1
        FROM provider_city_modes
        WHERE provider_id=? AND city_id=?
        """,
        (provider_id, city_id),
    ).fetchone() is not None
    if not configured:
        raise ValueError(
            f"Supplemental stop {city_id}/{stop.get('id')} "
            f"references unconfigured provider {provider_id}"
        )
    return provider_id


def populate_provider_city_memberships(
    connection: sqlite3.Connection,
    stop_data: Path,
    included_city_ids: set[str],
    stop_id_prefix_by_provider: dict[str, str] | None = None,
    indexed_ownership_lookup: bool = False,
    catalog_only_city_ids: set[str] | None = None,
) -> set[str]:
    """Map package memberships to the providers that own their GTFS stop IDs."""
    manifest = json.loads((stop_data / "manifest.json").read_text(encoding="utf-8"))
    cities = manifest.get("cities")
    if not isinstance(cities, list):
        raise ValueError("Stop manifest must contain a cities array.")
    if not has_ownership_schema(connection):
        return populate_city_memberships(
            connection,
            stop_data,
            included_city_ids=included_city_ids,
        )
    if indexed_ownership_lookup:
        return _populate_provider_city_memberships_indexed(
            connection,
            stop_data,
            included_city_ids,
            stop_id_prefix_by_provider,
            catalog_only_city_ids or set(),
        )

    city_ids: set[str] = set()
    for city in cities:
        if not isinstance(city, dict) or not isinstance(city.get("id"), str):
            raise ValueError("Stop manifest contains an invalid city entry.")
        city_id = str(city["id"])
        if city_id not in included_city_ids:
            continue
        package_path = stop_data / str(city.get("url", ""))
        package = json.loads(package_path.read_text(encoding="utf-8"))
        if not isinstance(package, list):
            raise ValueError(f"Invalid stop package for {city_id}")
        prefix_by_provider = dict(stop_id_prefix_by_provider or {})
        for provider_id, prefix in connection.execute(
            "SELECT provider_id, stop_id_prefix FROM provider_city_modes WHERE city_id=?",
            (city_id,),
        ):
            prefix_by_provider.setdefault(str(provider_id), str(prefix))
        package_stop_ids: set[str] = set()
        owned_ids: dict[str, set[str]] = {}
        for stop in package:
            if not isinstance(stop, dict) or not stop.get("id"):
                continue
            stop_ids = {str(stop["id"])}
            stop_ids.update(str(alias) for alias in stop.get("sourceStopIDs", []) if alias)
            package_stop_ids.update(stop_ids)
            candidate_stop_ids = set(stop_ids)
            for prefix in prefix_by_provider.values():
                if prefix:
                    candidate_stop_ids.update(f"{prefix}{stop_id}" for stop_id in stop_ids)
            placeholders = ",".join("?" for _ in candidate_stop_ids)
            rows = connection.execute(
                """
                SELECT provider_id, key_1
                FROM provider_entities
                WHERE entity_type = 'raw_stops'
                  AND key_1 IN (%s)
                """ % placeholders,
                tuple(sorted(candidate_stop_ids)),
            )
            owners = [(str(provider), str(stop_id)) for provider, stop_id in rows]
            preferred_owners = [
                (provider, stop_id)
                for provider, stop_id in owners
                if prefix_by_provider.get(provider)
                and stop_id in {
                    f"{prefix_by_provider[provider]}{public_stop_id}"
                    for public_stop_id in stop_ids
                }
            ]
            if preferred_owners:
                owners = preferred_owners
            if not owners:
                catalog_provider = _supplemental_stop_provider(
                    connection,
                    city_id,
                    stop,
                    prefix_by_provider,
                )
                if catalog_provider:
                    owned_ids.setdefault(catalog_provider, set()).add(
                        str(stop["id"])
                    )
                    continue
                raise ValueError(
                    f"Could not resolve provider ownership for package stop "
                    f"{city_id}/{stop.get('id')}"
                )
            for provider, stop_id in owners:
                owned_ids.setdefault(provider, set()).add(stop_id)
            unresolved_public_id = str(stop["id"]) not in {stop_id for _, stop_id in owners}
            if unresolved_public_id:
                for provider, _ in owners:
                    owned_ids.setdefault(provider, set()).add(str(stop["id"]))

        memberships = [(city_id, stop_id) for stop_id in sorted(package_stop_ids)]
        connection.executemany(
            "INSERT OR IGNORE INTO city_stops(city_id, stop_id) VALUES (?, ?)",
            memberships,
        )
        for provider, stop_ids in owned_ids.items():
            register_city_stops(
                connection,
                provider,
                ((city_id, stop_id) for stop_id in sorted(stop_ids)),
            )
        city_ids.add(city_id)
    connection.commit()
    return city_ids


def _populate_provider_city_memberships_indexed(
    connection: sqlite3.Connection,
    stop_data: Path,
    included_city_ids: set[str],
    stop_id_prefix_by_provider: dict[str, str] | None,
    catalog_only_city_ids: set[str],
) -> set[str]:
    """Resolve scoped package ownership through one indexed TEMP set."""
    manifest = json.loads((stop_data / "manifest.json").read_text(encoding="utf-8"))
    cities = manifest.get("cities")
    if not isinstance(cities, list):
        raise ValueError("Stop manifest must contain a cities array.")

    city_packages: list[tuple[str, list[dict[str, object]], dict[str, str], bool]] = []
    candidate_stop_ids: set[str] = set()
    for city in cities:
        if not isinstance(city, dict) or not isinstance(city.get("id"), str):
            raise ValueError("Stop manifest contains an invalid city entry.")
        city_id = str(city["id"])
        if city_id not in included_city_ids:
            continue
        package_path = stop_data / str(city.get("url", ""))
        package = json.loads(package_path.read_text(encoding="utf-8"))
        if not isinstance(package, list):
            raise ValueError(f"Invalid stop package for {city_id}")
        prefix_by_provider = dict(stop_id_prefix_by_provider or {})
        for provider_id, prefix in connection.execute(
            "SELECT provider_id, stop_id_prefix FROM provider_city_modes WHERE city_id=?",
            (city_id,),
        ):
            prefix_by_provider.setdefault(str(provider_id), str(prefix))
        typed_package = [stop for stop in package if isinstance(stop, dict)]
        catalog_only = city_id in catalog_only_city_ids or city.get("catalogOnly") is True
        city_packages.append((city_id, typed_package, prefix_by_provider, catalog_only))
        for stop in typed_package:
            if not stop.get("id"):
                continue
            stop_ids = {str(stop["id"])}
            stop_ids.update(str(alias) for alias in stop.get("sourceStopIDs", []) if alias)
            candidate_stop_ids.update(stop_ids)
            for prefix in prefix_by_provider.values():
                if prefix:
                    candidate_stop_ids.update(f"{prefix}{stop_id}" for stop_id in stop_ids)

    connection.execute(
        "DROP TABLE IF EXISTS temp.scoped_membership_candidate_stop_ids"
    )
    connection.execute(
        "DROP TABLE IF EXISTS temp.scoped_membership_stop_owners"
    )
    try:
        connection.executescript(
            """
            CREATE TEMP TABLE scoped_membership_candidate_stop_ids(
                stop_id TEXT PRIMARY KEY
            ) WITHOUT ROWID;
            CREATE TEMP TABLE scoped_membership_stop_owners(
                stop_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                PRIMARY KEY(stop_id, provider_id)
            ) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO scoped_membership_candidate_stop_ids(stop_id) VALUES (?)",
            ((stop_id,) for stop_id in sorted(candidate_stop_ids)),
        )
        connection.execute(
            """
            INSERT INTO scoped_membership_stop_owners(stop_id, provider_id)
            SELECT entities.key_1, entities.provider_id
            FROM provider_entities AS entities
            JOIN scoped_membership_candidate_stop_ids AS candidates
              ON candidates.stop_id = entities.key_1
            WHERE entities.entity_type = 'raw_stops'
            """
        )
        owners_by_stop: dict[str, list[tuple[str, str]]] = {}
        for stop_id, provider_id in connection.execute(
            "SELECT stop_id, provider_id FROM scoped_membership_stop_owners"
        ):
            owners_by_stop.setdefault(str(stop_id), []).append(
                (str(provider_id), str(stop_id))
            )
    finally:
        connection.execute(
            "DROP TABLE IF EXISTS temp.scoped_membership_stop_owners"
        )
        connection.execute(
            "DROP TABLE IF EXISTS temp.scoped_membership_candidate_stop_ids"
        )

    city_ids: set[str] = set()
    for city_id, package, prefix_by_provider, catalog_only in city_packages:
        package_stop_ids: set[str] = set()
        owned_ids: dict[str, set[str]] = {}
        for stop in package:
            if not stop.get("id"):
                continue
            stop_ids = {str(stop["id"])}
            stop_ids.update(str(alias) for alias in stop.get("sourceStopIDs", []) if alias)
            package_stop_ids.update(stop_ids)
            candidate_ids = set(stop_ids)
            for prefix in prefix_by_provider.values():
                if prefix:
                    candidate_ids.update(f"{prefix}{stop_id}" for stop_id in stop_ids)
            owners = [
                owner
                for candidate_id in sorted(candidate_ids)
                for owner in owners_by_stop.get(candidate_id, ())
            ]
            preferred_owners = [
                (provider, stop_id)
                for provider, stop_id in owners
                if prefix_by_provider.get(provider)
                and stop_id in {
                    f"{prefix_by_provider[provider]}{public_stop_id}"
                    for public_stop_id in stop_ids
                }
            ]
            if preferred_owners:
                owners = preferred_owners
            if not owners:
                if catalog_only:
                    continue
                catalog_provider = _supplemental_stop_provider(
                    connection,
                    city_id,
                    stop,
                    prefix_by_provider,
                )
                if catalog_provider:
                    owned_ids.setdefault(catalog_provider, set()).add(
                        str(stop["id"])
                    )
                    continue
                raise ValueError(
                    f"Could not resolve provider ownership for package stop "
                    f"{city_id}/{stop.get('id')}"
                )
            for provider, stop_id in owners:
                owned_ids.setdefault(provider, set()).add(stop_id)
            unresolved_public_id = str(stop["id"]) not in {
                stop_id for _, stop_id in owners
            }
            if unresolved_public_id:
                for provider, _ in owners:
                    owned_ids.setdefault(provider, set()).add(str(stop["id"]))

        memberships = [(city_id, stop_id) for stop_id in sorted(package_stop_ids)]
        connection.executemany(
            "INSERT OR IGNORE INTO city_stops(city_id, stop_id) VALUES (?, ?)",
            memberships,
        )
        for provider, stop_ids in owned_ids.items():
            register_city_stops(
                connection,
                provider,
                ((city_id, stop_id) for stop_id in sorted(stop_ids)),
            )
        city_ids.add(city_id)
    connection.commit()
    return city_ids


def configured_austrian_static_city_ids(cities_path: Path) -> set[str]:
    return {
        str(city["id"])
        for city in load_cities(cities_path)
        if city.get("packageMode") == "austrian"
        and city.get("staticDepartures") is True
    }


def import_austrian_gtfs(
    connection: sqlite3.Connection,
    stop_data: Path,
    cities_path: Path,
    gtfs_dir: Path,
    registry_path: Path,
) -> set[str]:
    registry = load_austrian_sources(registry_path)
    expected_city_ids = configured_austrian_static_city_ids(cities_path)
    imported_city_ids: set[str] = set()
    for source in registry:
        source_id = str(source["id"])
        candidates = sorted(gtfs_dir.glob(f"{source_id}-*.zip"))
        if not candidates:
            raise ValueError(f"Missing Austrian GTFS archive for source {source_id}")
        with load_gtfs_archive(str(candidates[-1])) as archive:
            stop_prefix = "" if source.get("preserveStopIDs", False) else str(source["identifierPrefix"])
            timed_stage(
                f"austria:{source_id}",
                "populate_gtfs",
                lambda: populate_gtfs(
                    connection,
                    archive,
                    identifier_prefix=str(source["identifierPrefix"]),
                    stop_id_prefix=stop_prefix,
                    provider_id=source_id,
                ),
            )
        imported_city_ids.update(str(city) for city in source["cities"])
    if imported_city_ids != expected_city_ids:
        raise ValueError(
            "Austrian registry/city configuration mismatch: "
            f"registry={sorted(imported_city_ids)} configured={sorted(expected_city_ids)}"
        )
    imported_city_ids = timed_stage(
        "austria",
        "provider-city-memberships",
        lambda: populate_provider_city_memberships(
            connection,
            stop_data,
            included_city_ids=expected_city_ids,
            indexed_ownership_lookup=True,
        ),
    )
    for source in registry:
        source_id = str(source["id"])
        for city_id in source["cities"]:
            if city_id in expected_city_ids:
                register_city_mode(
                    connection,
                    source_id,
                    str(city_id),
                    "exact-stop-with-parent-fallback",
                    "Europe/Vienna",
                )
    return imported_city_ids


def validate(connection: sqlite3.Connection) -> None:
    required = ("raw_stops", "city_stops", "city_aliases", "routes", "trips", "stop_times", "active_services")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(required).issubset(tables):
        raise ValueError("Static departures database is incomplete.")
    if connection.execute("SELECT COUNT(*) FROM city_stops").fetchone()[0] == 0:
        raise ValueError("Static departures database contains no city stop memberships.")
    if connection.execute("SELECT COUNT(*) FROM active_services").fetchone()[0] == 0:
        raise ValueError("Static departures database contains no active services.")


def stop_data_metadata(stop_data: Path) -> tuple[str, str]:
    manifest = json.loads((stop_data / "manifest.json").read_text(encoding="utf-8"))
    release_id = str(manifest.get("releaseID", "")).strip()
    manifest_version = str(manifest.get("version", "")).strip()
    return release_id, manifest_version


def populate_city_aliases(
    connection: sqlite3.Connection,
    aliases: dict[str, str],
    city_ids: set[str]
) -> None:
    for alias, target in aliases.items():
        if alias in city_ids and alias != target:
            raise ValueError(f"City ID alias conflicts with a canonical city ID: {alias}")
        if target not in city_ids:
            raise ValueError(f"City ID alias target is absent from German city memberships: {alias} -> {target}")
    connection.executemany(
        "INSERT INTO city_aliases(alias_city_id, canonical_city_id) VALUES (?, ?)",
        sorted(aliases.items())
    )
    connection.commit()


def add_external_gtfs(
    connection: sqlite3.Connection,
    stop_data: Path,
    url_by_provider: dict[str, str],
    *,
    repository_root: Path,
    sources_path: Path,
    dates: list[date],
    populate_memberships: bool = True,
    environ: dict[str, str] | None = None,
    scoped: bool = False,
    stage_runner: ImportStageRunner | None = None,
) -> set[str]:
    """Augment an existing static departures database with external GTFS feeds.

    External cities (e.g. stockholm) are exposed as canonical stop IDs, so the
    board/lines queries resolve package parent stops to their child platforms
    via raw_stops.canonical_stop_id. Per-city modes are written to
    city_departure_modes with the source timezone. Returns the imported city IDs.
    """
    sources = load_external_gtfs_sources(sources_path)
    sources_by_id = {str(source["id"]): source for source in sources}
    unknown = sorted(set(url_by_provider) - set(sources_by_id))
    if unknown:
        raise ValueError(f"Unknown external GTFS sources: {', '.join(unknown)}")

    imported_city_ids: set[str] = set()
    catalog_only_city_ids: set[str] = set()
    source_stop_id_prefixes = {
        source_id: str(source.get("staticStopIDPrefix", (
            str(source.get("namespace", "")).strip()
            or str(source["identifierPrefix"])
        )))
        for source_id, source in sources_by_id.items()
        if source_id in url_by_provider
    }
    for source_id in sorted(url_by_provider):
        source = sources_by_id[source_id]
        validate_external_gtfs_source(source, repository_root)
        cities = load_external_cities(source, repository_root)
        catalog_only_city_ids.update(
            str(city["id"])
            for city in cities
            if city.get("catalogOnly") is True
        )
        request_url, headers = authenticated_external_request(
            source_id,
            url_by_provider[source_id],
            environ=environ if environ is not None else os.environ,
        )
        raw_archive = load_gtfs_archive(request_url, headers=headers)
        archive = agency_scoped_archive(raw_archive, source.get("agencyID"))
        try:
            provider_stage_runner = None
            if stage_runner is not None:
                provider_stage_runner = lambda stage, callback, source_id=source_id: stage_runner(
                    f"{source_id}:{stage}", callback
                )
            timed_stage(
                f"external:{source_id}",
                "populate_gtfs",
                lambda: populate_gtfs(
                    connection,
                    archive,
                    identifier_prefix=str(source["identifierPrefix"]),
                    stop_id_prefix=(
                        str(source.get("staticStopIDPrefix", (
                            str(source.get("namespace", "")).strip()
                            or str(source["identifierPrefix"])
                        )))
                    ),
                    provider_id=source_id,
                    stage_runner=provider_stage_runner,
                ),
            )
        finally:
            archive.close()
        imported_city_ids.update(str(city["id"]) for city in cities)

    if not imported_city_ids:
        return imported_city_ids

    provider_scope = (source_id for source_id in url_by_provider) if scoped else None
    if stage_runner is None:
        resolve_canonical_stops(connection, provider_ids=provider_scope)
    else:
        stage_runner(
            "canonical-stops",
            lambda: resolve_canonical_stops(connection, provider_ids=provider_scope),
        )
    if populate_memberships:
        if stage_runner is None:
            timed_stage(
                "external",
                "provider-city-memberships",
                lambda: populate_provider_city_memberships(
                    connection,
                    stop_data,
                    included_city_ids=imported_city_ids,
                    stop_id_prefix_by_provider=source_stop_id_prefixes,
                    indexed_ownership_lookup=True,
                    catalog_only_city_ids=catalog_only_city_ids,
                ),
            )
        else:
            stage_runner(
                "provider-city-memberships",
                lambda: populate_provider_city_memberships(
                    connection,
                    stop_data,
                    included_city_ids=imported_city_ids,
                    stop_id_prefix_by_provider=source_stop_id_prefixes,
                    indexed_ownership_lookup=True,
                    catalog_only_city_ids=catalog_only_city_ids,
                ),
            )
    provider_scope = tuple(url_by_provider) if scoped else None
    if stage_runner is None:
        populate_active_services(connection, dates, provider_ids=provider_scope)
        update_terminal_stops(connection, provider_ids=provider_scope)
    else:
        stage_runner(
            "active-services",
            lambda: populate_active_services(connection, dates, provider_ids=provider_scope),
        )
        stage_runner(
            "terminal-stops",
            lambda: update_terminal_stops(connection, provider_ids=provider_scope),
        )

    if "stop_id_prefix" not in {
        row[1] for row in connection.execute("PRAGMA table_info(city_departure_modes)")
    }:
        connection.execute(
            "ALTER TABLE city_departure_modes ADD COLUMN stop_id_prefix TEXT NOT NULL DEFAULT ''"
        )
    if "identifier_prefix" not in {
        row[1] for row in connection.execute("PRAGMA table_info(city_departure_modes)")
    }:
        connection.execute(
            "ALTER TABLE city_departure_modes ADD COLUMN identifier_prefix TEXT NOT NULL DEFAULT ''"
        )
    source_ids_by_city: dict[str, list[str]] = {}
    for source_id in url_by_provider:
        source = sources_by_id[source_id]
        for city in load_external_cities(source, repository_root):
            source_ids_by_city.setdefault(str(city["id"]), []).append(source_id)

    def departure_mode(source: dict[str, object]) -> str:
        mode = str(source.get("staticDepartureMode", "canonical")).strip() or "canonical"
        if mode not in {"canonical", "exact-stop-with-parent-fallback"}:
            raise ValueError(f"Unsupported staticDepartureMode for {source.get('id')}: {mode}")
        return mode

    def register_modes() -> int:
        connection.executemany(
            "INSERT OR IGNORE INTO city_departure_modes(city_id, mode, timezone, stop_id_prefix, identifier_prefix) VALUES (?, ?, ?, ?, ?)",
            (
                (
                    city_id,
                    departure_mode(sources_by_id[source_ids_by_city[city_id][0]]),
                    str(sources_by_id[source_ids_by_city[city_id][0]]["timezone"]),
                    (
                        str(sources_by_id[source_ids_by_city[city_id][0]].get("staticStopIDPrefix", (
                            str(sources_by_id[source_ids_by_city[city_id][0]].get("namespace", "")).strip()
                            or str(sources_by_id[source_ids_by_city[city_id][0]]["identifierPrefix"])
                        )))
                        if len(source_ids_by_city[city_id]) == 1
                        and not str(sources_by_id[source_ids_by_city[city_id][0]].get("namespace", "")).strip()
                        else ""
                    ),
                    (
                        str(sources_by_id[source_ids_by_city[city_id][0]].get("staticIdentifierPrefix", ""))
                        if len(source_ids_by_city[city_id]) == 1
                        and not str(sources_by_id[source_ids_by_city[city_id][0]].get("namespace", "")).strip()
                        else ""
                    ),
                )
                for city_id in sorted(imported_city_ids)
            ),
        )
        for city_id in sorted(imported_city_ids):
            for source_id in source_ids_by_city[city_id]:
                source = sources_by_id[source_id]
                register_city_mode(
                    connection,
                    source_id,
                    city_id,
                    departure_mode(source),
                    str(source["timezone"]),
                    (
                        str(source.get("staticStopIDPrefix", (
                            str(source.get("namespace", "")).strip()
                            or str(source["identifierPrefix"])
                        )))
                        if len(source_ids_by_city[city_id]) == 1
                        and not str(source.get("namespace", "")).strip()
                        else ""
                    ),
                    (
                        str(source.get("staticIdentifierPrefix", ""))
                        if len(source_ids_by_city[city_id]) == 1
                        and not str(source.get("namespace", "")).strip()
                        else ""
                    ),
                )
        return len(imported_city_ids)

    if stage_runner is None:
        register_modes()
    else:
        stage_runner("memberships:modes", register_modes)
    connection.commit()
    return imported_city_ids


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs-url", default="")
    parser.add_argument("--austrian-gtfs", default="")
    parser.add_argument("--austrian-gtfs-dir", default="")
    parser.add_argument("--austrian-sources", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--stop-data", required=True, help="Read-only current stop dataset")
    parser.add_argument("--next", default="", help="Fresh database output path (required unless --add-external).")
    parser.add_argument("--release-id", default="", help="Release identity stored in database metadata.")
    parser.add_argument("--days", type=int, default=15)
    parser.add_argument("--cities", default=str(REPOSITORY_ROOT / "config" / "cities.json"))
    parser.add_argument("--swiss-cities", default=str(REPOSITORY_ROOT / "config" / "swiss-cities.json"))
    parser.add_argument("--city-id-aliases", default=str(REPOSITORY_ROOT / "config" / "city-id-aliases.json"))
    parser.add_argument("--add-external", action="store_true",
                        help="Augment --db with --external-gtfs-url feeds instead of building a fresh database.")
    parser.add_argument("--db", default="",
                        help="Existing database to augment with --add-external.")
    parser.add_argument("--external-gtfs-url", action="append", default=[],
                        help="External feed providerID=URL (repeatable).")
    parser.add_argument("--timezone", default="",
                        help="Service-window timezone for active services (default: first external source timezone, else Europe/Berlin).")
    parser.add_argument("--external-sources", default=str(REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"))
    args = parser.parse_args()
    url_by_provider = parse_external_gtfs_url_args(args.external_gtfs_url)
    next_path = Path(args.next)
    if not args.add_external:
        next_path.parent.mkdir(parents=True, exist_ok=True)
        next_path.unlink(missing_ok=True)
    stop_data_release_id, stop_data_manifest_version = stop_data_metadata(Path(args.stop_data))
    dates = service_window(DEFAULT_TIMEZONE, args.days)

    if args.add_external:
        if not url_by_provider:
            raise ValueError("--add-external requires at least one --external-gtfs-url.")
        database_path = Path(args.db)
        if not database_path.is_file():
            raise ValueError(f"Existing database does not exist: {database_path}")
        timezone_name = args.timezone.strip() or _external_window_timezone(
            Path(args.external_sources), url_by_provider
        )
        dates = service_window(timezone_name, args.days)
        connection = sqlite3.connect(database_path)
        connection.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
        try:
            imported = timed_stage("external", "populate_gtfs", lambda: add_external_gtfs(
                connection,
                Path(args.stop_data),
                url_by_provider,
                repository_root=REPOSITORY_ROOT,
                sources_path=Path(args.external_sources),
                dates=dates,
            ))
            version = str(uuid.uuid4())
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    ("databaseVersion", version),
                    ("releaseID", args.release_id.strip()),
                    ("stopDataReleaseID", stop_data_release_id),
                    ("stopDataManifestVersion", stop_data_manifest_version),
                    ("generatedAt", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
                    ("validFrom", dates[0].isoformat()),
                    ("validThrough", dates[-1].isoformat()),
                    ("timezone", timezone_name),
                ),
            )
            validate(connection)
            connection.commit()
        finally:
            connection.close()
        print(json.dumps({"databaseVersion": version, "externalCityCount": len(imported)}, separators=(",", ":")))
        return

    if not args.gtfs_url.strip():
        raise ValueError("--gtfs-url is required unless --add-external is used.")
    if not args.next.strip():
        raise ValueError("--next is required unless --add-external is used.")
    with load_gtfs_archive(args.gtfs_url) as archive:
        connection = connect(next_path)
        try:
            imported_external_city_ids: set[str] = set()
            timed_stage("germany", "populate_gtfs", lambda: populate_gtfs(connection, archive))
            austrian_city_ids = configured_austrian_static_city_ids(Path(args.cities))
            if args.austrian_gtfs_dir:
                austrian_city_ids = import_austrian_gtfs(
                    connection,
                    Path(args.stop_data),
                    Path(args.cities),
                    Path(args.austrian_gtfs_dir),
                    Path(args.austrian_sources),
                )
            elif args.austrian_gtfs:
                with load_gtfs_archive(args.austrian_gtfs) as austrian_archive:
                    timed_stage(
                        "austria:vor",
                        "populate_gtfs",
                        lambda: populate_gtfs(
                            connection,
                            austrian_archive,
                            identifier_prefix="vor:",
                            provider_id="vor",
                        ),
                    )
            timed_stage("all", "canonical-stops", lambda: resolve_canonical_stops(connection))
            city_ids = timed_stage("all", "city-memberships", lambda: populate_german_city_memberships(
                connection, Path(args.stop_data), configured_external_city_ids(Path(args.cities), Path(args.swiss_cities))
            ))
            if austrian_city_ids:
                if not args.austrian_gtfs and not args.austrian_gtfs_dir:
                    raise ValueError("Austrian static departure cities require an Austrian GTFS source.")
                if not args.austrian_gtfs_dir:
                    city_ids.update(populate_city_memberships(
                        connection,
                        Path(args.stop_data),
                        included_city_ids=austrian_city_ids,
                        provider_id="vor",
                    ))
                else:
                    city_ids.update(austrian_city_ids)
            timed_stage("all", "active-services", lambda: populate_active_services(connection, dates))
            timed_stage("all", "terminal-stops", lambda: update_terminal_stops(connection))
            connection.executescript("""
                CREATE TABLE city_aliases (
                    alias_city_id TEXT PRIMARY KEY,
                    canonical_city_id TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE INDEX raw_stops_by_canonical ON raw_stops(canonical_stop_id, stop_id);
                CREATE INDEX stop_times_by_stop ON stop_times(raw_stop_id, trip_id, departure_seconds);
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
                CREATE TABLE city_departure_modes (
                    city_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    stop_id_prefix TEXT NOT NULL DEFAULT '',
                    identifier_prefix TEXT NOT NULL DEFAULT ''
                ) WITHOUT ROWID;
            """)
            connection.executemany(
                "INSERT INTO city_departure_modes(city_id, mode, timezone) VALUES (?, 'exact-stop-with-parent-fallback', 'Europe/Vienna')",
                ((city_id,) for city_id in sorted(austrian_city_ids)),
            )
            for city_id in sorted(austrian_city_ids):
                register_city_mode(
                    connection,
                    "vor",
                    city_id,
                    "exact-stop-with-parent-fallback",
                    "Europe/Vienna",
                )
            populate_city_aliases(connection, load_city_aliases(Path(args.city_id_aliases)), city_ids)
            version = str(uuid.uuid4())
            connection.executemany("INSERT INTO metadata VALUES (?, ?)", (
                ("schemaVersion", "1"), ("databaseVersion", version),
                ("releaseID", args.release_id.strip()),
                ("stopDataReleaseID", stop_data_release_id),
                ("stopDataManifestVersion", stop_data_manifest_version),
                ("generatedAt", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
                ("validFrom", dates[0].isoformat()), ("validThrough", dates[-1].isoformat()),
                ("timezone", DEFAULT_TIMEZONE),
            ))
            if url_by_provider:
                external_timezone = _external_window_timezone(
                    Path(args.external_sources), url_by_provider
                )
                external_dates = service_window(external_timezone, args.days)
                imported_external_city_ids = timed_stage("external", "populate_gtfs", lambda: add_external_gtfs(
                    connection,
                    Path(args.stop_data),
                    url_by_provider,
                    repository_root=REPOSITORY_ROOT,
                    sources_path=Path(args.external_sources),
                    dates=external_dates,
                ))
                city_ids.update(imported_external_city_ids)
            rebuild_city_stops(connection)
            rebuild_city_departure_modes(connection)
            timed_stage("all", "validation", lambda: validate(connection))
            connection.commit()
        finally:
            connection.close()
    print(json.dumps({"databaseVersion": version, "cityCount": len(city_ids)}, separators=(",", ":")))


if __name__ == "__main__":
    main()
