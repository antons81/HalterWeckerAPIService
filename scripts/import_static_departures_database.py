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
    populate_active_services, populate_gtfs, resolve_canonical_stops, service_window,
    update_terminal_stops,
)
from build_stop_packages import load_cities, load_gtfs_archive, nl_city_ids
from austrian_sources import DEFAULT_REGISTRY, load_austrian_sources, public_stop_id
from external_gtfs import (
    authenticated_external_request,
    external_city_ids,
    load_external_cities,
    load_external_gtfs_sources,
    parse_external_gtfs_url_args,
    validate_external_gtfs_source,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def timed_stage(source: str, stage: str, callback):
    started = time.monotonic()
    result = callback()
    print(f"[StaticDepartures] source={source} stage={stage} duration={time.monotonic() - started:.2f}s")
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
        stop_ids: set[str] = set()
        for stop in package:
            if not isinstance(stop, dict) or not stop.get("id"):
                continue
            stop_ids.add(str(stop["id"]))
            stop_ids.update(str(alias) for alias in stop.get("sourceStopIDs", []) if alias)
        connection.executemany(
            "INSERT OR IGNORE INTO city_stops(city_id, stop_id) VALUES (?, ?)",
            ((city_id, stop_id) for stop_id in stop_ids),
        )
        city_ids.add(city_id)
    connection.commit()
    return city_ids


def populate_german_city_memberships(
    connection: sqlite3.Connection,
    stop_data: Path,
    excluded_city_ids: set[str]
) -> set[str]:
    return populate_city_memberships(
        connection,
        stop_data,
        excluded_city_ids=excluded_city_ids,
    )


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
                ),
            )
        imported_city_ids.update(str(city) for city in source["cities"])
    if imported_city_ids != expected_city_ids:
        raise ValueError(
            "Austrian registry/city configuration mismatch: "
            f"registry={sorted(imported_city_ids)} configured={sorted(expected_city_ids)}"
        )
    return populate_city_memberships(
        connection,
        stop_data,
        included_city_ids=expected_city_ids,
    )


def validate(connection: sqlite3.Connection) -> None:
    required = ("raw_stops", "city_stops", "city_aliases", "routes", "trips", "stop_times", "active_services")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(required).issubset(tables):
        raise ValueError("Static departures database is incomplete.")
    if connection.execute("SELECT COUNT(*) FROM city_stops").fetchone()[0] == 0:
        raise ValueError("Static departures database contains no city stop memberships.")
    if connection.execute("SELECT COUNT(*) FROM active_services").fetchone()[0] == 0:
        raise ValueError("Static departures database contains no active services.")


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
    for source_id in sorted(url_by_provider):
        source = sources_by_id[source_id]
        validate_external_gtfs_source(source, repository_root)
        cities = load_external_cities(source, repository_root)
        request_url, headers = authenticated_external_request(
            source_id,
            url_by_provider[source_id],
            environ=os.environ,
        )
        with load_gtfs_archive(request_url, headers=headers) as archive:
            populate_gtfs(
                connection,
                archive,
                identifier_prefix=str(source["identifierPrefix"]),
                stop_id_prefix=str(source["identifierPrefix"]),
            )
        imported_city_ids.update(str(city["id"]) for city in cities)

    if not imported_city_ids:
        return imported_city_ids

    resolve_canonical_stops(connection)
    populate_city_memberships(
        connection,
        stop_data,
        included_city_ids=imported_city_ids,
    )
    populate_active_services(connection, dates)
    update_terminal_stops(connection)

    if "stop_id_prefix" not in {
        row[1] for row in connection.execute("PRAGMA table_info(city_departure_modes)")
    }:
        connection.execute(
            "ALTER TABLE city_departure_modes ADD COLUMN stop_id_prefix TEXT NOT NULL DEFAULT ''"
        )
    source_by_city: dict[str, str] = {}
    for source_id in url_by_provider:
        source = sources_by_id[source_id]
        for city in load_external_cities(source, repository_root):
            source_by_city[str(city["id"])] = source_id
    connection.executemany(
        "INSERT OR IGNORE INTO city_departure_modes(city_id, mode, timezone, stop_id_prefix) VALUES (?, 'canonical', ?, ?)",
        (
            (
                city_id,
                str(sources_by_id[source_by_city[city_id]]["timezone"]),
                str(sources_by_id[source_by_city[city_id]]["identifierPrefix"]),
            )
            for city_id in sorted(imported_city_ids)
        ),
    )
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
                        help="External feed providerID=URL (repeatable, used with --add-external).")
    parser.add_argument("--timezone", default="",
                        help="Service-window timezone for active services (default: first external source timezone, else Europe/Berlin).")
    parser.add_argument("--external-sources", default=str(REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"))
    args = parser.parse_args()
    next_path = Path(args.next)
    if not args.add_external:
        next_path.parent.mkdir(parents=True, exist_ok=True)
        next_path.unlink(missing_ok=True)
    dates = service_window(DEFAULT_TIMEZONE, args.days)

    if args.add_external:
        url_by_provider = parse_external_gtfs_url_args(args.external_gtfs_url)
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
                    timed_stage("austria:vor", "populate_gtfs", lambda: populate_gtfs(connection, austrian_archive, identifier_prefix="vor:"))
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
                    stop_id_prefix TEXT NOT NULL DEFAULT ''
                ) WITHOUT ROWID;
            """)
            connection.executemany(
                "INSERT INTO city_departure_modes(city_id, mode, timezone) VALUES (?, 'exact-stop-with-parent-fallback', 'Europe/Vienna')",
                ((city_id,) for city_id in sorted(austrian_city_ids)),
            )
            populate_city_aliases(connection, load_city_aliases(Path(args.city_id_aliases)), city_ids)
            version = str(uuid.uuid4())
            connection.executemany("INSERT INTO metadata VALUES (?, ?)", (
                ("schemaVersion", "1"), ("databaseVersion", version),
                ("releaseID", args.release_id.strip()),
                ("generatedAt", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
                ("validFrom", dates[0].isoformat()), ("validThrough", dates[-1].isoformat()),
                ("timezone", DEFAULT_TIMEZONE),
            ))
            timed_stage("all", "validation", lambda: validate(connection))
            connection.commit()
        finally:
            connection.close()
    print(json.dumps({"databaseVersion": version, "cityCount": len(city_ids)}, separators=(",", ":")))


if __name__ == "__main__":
    main()
