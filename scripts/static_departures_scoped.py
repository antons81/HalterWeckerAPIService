#!/usr/bin/env python3
"""Manual provider-scoped static departures rebuild implementation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from build_german_departure_index import (
    DEFAULT_TIMEZONE,
    populate_gtfs,
    resolve_canonical_stops,
    service_window,
    update_terminal_stops,
)
from build_stop_packages import load_gtfs_archive, transit_radar_manifest
from external_gtfs import (
    authenticated_external_request,
    load_external_gtfs_sources,
)
from gtfs_source_cache import DEFAULT_CACHE_ROOT, GTFSArtifactCache
from import_static_departures_database import (
    add_external_gtfs,
    configured_external_city_ids,
    populate_german_city_memberships,
    populate_provider_city_memberships,
    validate,
)
from static_departures_ownership import (
    delete_provider_data,
    has_ownership_schema,
    provider_city_ids,
    rebuild_city_departure_modes,
    rebuild_city_stops,
    register_city_mode,
)
from austrian_sources import load_austrian_sources


STATIC_CONTAINER_DEFAULT = "static-departures-api"


@dataclass(frozen=True)
class StaticProvider:
    provider_id: str
    country: str
    kind: str
    source: dict[str, object] | None = None


def timed_stage(stage: str, callback):
    started = time.monotonic()
    try:
        return callback()
    finally:
        print(
            f"[ScopedDepartures] stage={stage} duration={time.monotonic() - started:.2f}s",
            flush=True,
        )


def _io_snapshot() -> dict[str, int]:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/self/io").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            if key in {"rchar", "wchar"}:
                values[key] = int(value.strip())
        return values
    except (FileNotFoundError, OSError, ValueError):
        return {"rchar": 0, "wchar": 0}


def _timed_import_substage(
    connection: sqlite3.Connection,
    stage_name: str,
    callback,
):
    started = time.monotonic()
    changes_before = connection.total_changes
    io_before = _io_snapshot()
    try:
        result = callback()
    except BaseException:
        io_after = _io_snapshot()
        print(
            "[ScopedDepartures] stage=import-external "
            f"substage={stage_name} status=error "
            f"duration={time.monotonic() - started:.2f}s "
            f"rchar={io_after.get('rchar', 0) - io_before.get('rchar', 0)} "
            f"wchar={io_after.get('wchar', 0) - io_before.get('wchar', 0)}",
            flush=True,
        )
        raise
    io_after = _io_snapshot()
    row_count = (
        result
        if isinstance(result, int) and not isinstance(result, bool)
        else connection.total_changes - changes_before
    )
    print(
        "[ScopedDepartures] stage=import-external "
        f"substage={stage_name} duration={time.monotonic() - started:.2f}s "
        f"rows={row_count} "
        f"rchar={io_after.get('rchar', 0) - io_before.get('rchar', 0)} "
        f"wchar={io_after.get('wchar', 0) - io_before.get('wchar', 0)}",
        flush=True,
    )
    return result


def load_static_providers(repository_root: Path) -> list[StaticProvider]:
    """Build the registry from the existing source registries."""
    providers = [StaticProvider("germany", "DE", "germany")]
    providers.extend(
        StaticProvider(str(source["id"]), "AT", "austrian", source)
        for source in load_austrian_sources(
            repository_root / "config" / "austrian-sources.json"
        )
    )
    for source in load_external_gtfs_sources(
        repository_root / "config" / "external-gtfs-sources.json"
    ):
        if source.get("importIntoStaticDepartures") is True:
            country = str(source.get("country", "")).strip().upper()
            if len(country) != 2:
                raise ValueError(
                    f"Static external source {source.get('id', '<unknown>')} "
                    "has no valid country code."
                )
            providers.append(
                StaticProvider(str(source["id"]), country, "external", source)
            )
    return providers


def resolve_scope(
    repository_root: Path,
    *,
    provider_id: str = "",
    country: str = "",
) -> tuple[str, list[StaticProvider]]:
    if bool(provider_id.strip()) == bool(country.strip()):
        raise ValueError("Specify exactly one of --provider or --country.")

    providers = load_static_providers(repository_root)
    by_id = {provider.provider_id: provider for provider in providers}
    if provider_id.strip():
        requested = provider_id.strip()
        provider = by_id.get(requested)
        if provider is None:
            raise ValueError(
                f"Unknown static-departures provider {requested!r}. "
                f"Known providers: {', '.join(by_id)}"
            )
        return f"provider {requested}", [provider]

    requested_country = country.strip().upper()
    selected = [
        provider for provider in providers if provider.country == requested_country
    ]
    if not selected:
        raise ValueError(
            f"Unknown static-departures country {requested_country!r}; "
            "no provider is registered for this country."
        )
    return f"country {requested_country}", selected


def current_release_path(data_root: Path) -> Path | None:
    pointer = data_root / "current-release"
    if pointer.exists() or pointer.is_symlink():
        return pointer.resolve()
    return None


def current_database_path(data_root: Path) -> Path:
    release = current_release_path(data_root)
    if release is not None:
        candidate = release / "departures.sqlite"
        if candidate.is_file():
            return candidate
    compatibility = data_root / "departures-current.sqlite"
    if compatibility.is_file():
        return compatibility.resolve()
    raise ValueError(
        f"No active static departures database found below {data_root}."
    )


def current_stop_data_path(data_root: Path) -> Path:
    release = current_release_path(data_root)
    candidates = []
    if release is not None:
        candidates.append(release / "stop-data")
    candidates.append(data_root / "current")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise ValueError(f"No active stop-data directory found below {data_root}.")


def clone_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()


def update_release_metadata(
    connection: sqlite3.Connection,
    release_id: str,
    database_version: str,
) -> None:
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    connection.executemany(
        """
        INSERT INTO metadata(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (
            ("databaseVersion", database_version),
            ("releaseID", release_id),
            ("generatedAt", generated_at),
        ),
    )


def resolve_gtfs_artifact(
    provider_id: str,
    url: str,
    environment: dict[str, str],
) -> Path:
    request_url, headers = authenticated_external_request(
        provider_id,
        url,
        environ=environment,
    )
    cache_root = environment.get("GTFS_CACHE_ROOT", str(DEFAULT_CACHE_ROOT))
    artifact = GTFSArtifactCache(cache_root).resolve(
        provider_id,
        request_url,
        headers=headers,
        allow_stale=True,
        state_url=url,
    )
    return artifact.path


def resolve_external_artifact(
    provider: StaticProvider,
    environment: dict[str, str],
) -> Path:
    assert provider.source is not None
    configured_url = str(
        provider.source.get("url")
        or provider.source.get("scopedURL")
        or ""
    ).strip()
    local_path = str(provider.source.get("localPath") or "").strip()
    if local_path:
        path = Path(local_path)
        if path.is_dir():
            return path
        if not path.is_file():
            raise ValueError(
                f"Local external source {provider.provider_id} is missing: {path}"
            )
        return path
    if not configured_url:
        raise ValueError(
            f"Static provider {provider.provider_id} has no configured GTFS URL."
        )
    return resolve_gtfs_artifact(provider.provider_id, configured_url, environment)


def resolve_austrian_archive(
    provider: StaticProvider,
    data_root: Path,
    environment: dict[str, str],
) -> Path:
    source_id = provider.provider_id
    explicit = environment.get("AUSTRIAN_GTFS_PATH", "").strip()
    if explicit and source_id == "vor":
        path = Path(explicit)
        if path.is_file():
            return path
    directory = Path(
        environment.get("AUSTRIAN_GTFS_DIR", str(data_root / "austria"))
    )
    candidates = sorted(directory.glob(f"{source_id}-*.zip"))
    if not candidates:
        raise ValueError(
            f"No Austrian GTFS archive found for provider {source_id} in {directory}."
        )
    return candidates[-1]


def import_germany(
    connection: sqlite3.Connection,
    provider: StaticProvider,
    stop_data: Path,
    repository_root: Path,
    environment: dict[str, str],
    days: int,
) -> None:
    url = environment.get("GTFS_URL", "").strip()
    if not url:
        raise ValueError("GTFS_URL is required for the germany provider.")
    archive_path = resolve_gtfs_artifact("germany", url, environment)
    with load_gtfs_archive(str(archive_path)) as archive:
        populate_gtfs(connection, archive, provider_id=provider.provider_id)
    excluded = configured_external_city_ids(
        repository_root / "config" / "cities.json",
        repository_root / "config" / "swiss-cities.json",
    )
    manifest = json.loads((stop_data / "manifest.json").read_text(encoding="utf-8"))
    included_city_ids = {
        str(city["id"])
        for city in manifest.get("cities", [])
        if isinstance(city, dict) and isinstance(city.get("id"), str)
    }
    populate_german_city_memberships(
        connection,
        stop_data,
        excluded,
        provider_id=provider.provider_id,
        included_city_ids=included_city_ids,
    )
    from build_german_departure_index import populate_active_services

    populate_active_services(
        connection,
        service_window(DEFAULT_TIMEZONE, days),
        provider_ids=[provider.provider_id],
    )


def import_austrian(
    connection: sqlite3.Connection,
    providers: list[StaticProvider],
    stop_data: Path,
    repository_root: Path,
    data_root: Path,
    environment: dict[str, str],
    days: int,
) -> None:
    registry_by_id = {
        str(source["id"]): source
        for source in load_austrian_sources(
            repository_root / "config" / "austrian-sources.json"
        )
    }
    for provider in providers:
        source = registry_by_id[provider.provider_id]
        archive_path = resolve_austrian_archive(provider, data_root, environment)
        with load_gtfs_archive(str(archive_path)) as archive:
            stop_prefix = (
                "" if source.get("preserveStopIDs", False)
                else str(source["identifierPrefix"])
            )
            populate_gtfs(
                connection,
                archive,
                identifier_prefix=str(source["identifierPrefix"]),
                stop_id_prefix=stop_prefix,
                provider_id=provider.provider_id,
            )
        for city_id in source["cities"]:
            register_city_mode(
                connection,
                provider.provider_id,
                str(city_id),
                "exact-stop-with-parent-fallback",
                "Europe/Vienna",
            )
    selected_city_ids = {
        str(city_id)
        for provider in providers
        for city_id in registry_by_id[provider.provider_id]["cities"]
    }
    populate_provider_city_memberships(connection, stop_data, selected_city_ids)
    from build_german_departure_index import populate_active_services

    populate_active_services(
        connection,
        service_window("Europe/Vienna", days),
        provider_ids=[provider.provider_id for provider in providers],
    )


def import_external(
    connection: sqlite3.Connection,
    providers: list[StaticProvider],
    stop_data: Path,
    repository_root: Path,
    environment: dict[str, str],
    days: int,
    artifacts: dict[str, Path] | None = None,
) -> None:
    sources_path = repository_root / "config" / "external-gtfs-sources.json"
    imported_city_ids: set[str] = set()
    for provider in providers:
        archive_path = (artifacts or {}).get(provider.provider_id)
        if archive_path is None:
            archive_path = resolve_external_artifact(provider, environment)
        source = provider.source or {}
        timezone_name = str(source.get("timezone") or DEFAULT_TIMEZONE)
        imported_city_ids.update(add_external_gtfs(
            connection,
            stop_data,
            {provider.provider_id: str(archive_path)},
            repository_root=repository_root,
            sources_path=sources_path,
            dates=service_window(timezone_name, days),
            populate_memberships=False,
            environ=environment,
            scoped=True,
            stage_runner=lambda stage, callback: _timed_import_substage(
                connection, stage, callback
            ),
        ))
    if imported_city_ids:
        stop_id_prefix_by_provider = {
            provider.provider_id: str(
                (provider.source or {}).get(
                    "staticStopIDPrefix",
                    str((provider.source or {}).get("namespace", "")).strip()
                    or str((provider.source or {}).get("identifierPrefix", "")),
                )
            )
            for provider in providers
        }
        _timed_import_substage(
            connection,
            "memberships",
            lambda: populate_provider_city_memberships(
                connection,
                stop_data,
                included_city_ids=imported_city_ids,
                stop_id_prefix_by_provider=stop_id_prefix_by_provider,
                indexed_ownership_lookup=True,
            ),
        )


def build_external_static_assets(
    release_dir: Path,
    providers: list[StaticProvider],
    repository_root: Path,
    artifacts: dict[str, Path],
) -> None:
    """Build selected external JSON assets inside the staged release."""
    if not providers:
        return
    from external_gtfs import process_external_gtfs_sources

    stop_data = release_dir / "stop-data"
    selected_urls = {
        provider.provider_id: str(artifacts[provider.provider_id])
        for provider in providers
    }
    manifest_entries, external_cities, _package_stops, _lines = process_external_gtfs_sources(
        repository_root=repository_root,
        sources_path=repository_root / "config" / "external-gtfs-sources.json",
        url_by_provider=selected_urls,
        output=stop_data,
        load_gtfs_archive=load_gtfs_archive,
        occupied_city_ids=set(),
    )

    manifest_path = stop_data / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("Staged stop-data is missing manifest.json for external assets.")
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = manifest_payload.get("cities")
    if not isinstance(manifest, list):
        raise ValueError("Staged stop-data manifest has no cities array.")
    indexes = {
        str(entry.get("id")): index
        for index, entry in enumerate(manifest)
        if isinstance(entry, dict) and entry.get("id")
    }
    for raw_entry in manifest_entries:
        entry = dict(raw_entry)
        entry.pop("_source", None)
        city_id = str(entry.get("id", ""))
        if city_id in indexes:
            manifest[indexes[city_id]] = entry
        else:
            indexes[city_id] = len(manifest)
            manifest.append(entry)
    manifest.sort(key=lambda entry: str(entry.get("id", "")))
    manifest_payload["cities"] = manifest
    manifest_payload["version"] = datetime.now(timezone.utc).date().isoformat()
    manifest_payload["releaseID"] = release_dir.name
    manifest_path.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (stop_data / "cities.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    radar_path = stop_data / "transit-radar-cities.json"
    if radar_path.is_file():
        radar_payload = json.loads(radar_path.read_text(encoding="utf-8"))
        radar_cities = radar_payload.get("cities")
        if not isinstance(radar_cities, list):
            raise ValueError("Staged transit radar manifest has no cities array.")
        radar_indexes = {
            str(city.get("appCityID")): index
            for index, city in enumerate(radar_cities)
            if isinstance(city, dict) and city.get("appCityID")
        }
        for city in transit_radar_manifest(external_cities)["cities"]:
            app_city_id = str(city["appCityID"])
            if app_city_id in radar_indexes:
                radar_cities[radar_indexes[app_city_id]] = city
            else:
                radar_indexes[app_city_id] = len(radar_cities)
                radar_cities.append(city)
        radar_payload["cities"] = sorted(
            radar_cities,
            key=lambda city: str(city.get("appCityID", "")),
        )
        radar_path.write_text(
            json.dumps(radar_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    attributions_path = stop_data / "attributions.json"
    if attributions_path.is_file():
        attributions = json.loads(attributions_path.read_text(encoding="utf-8"))
        if isinstance(attributions, list) and not any(
            isinstance(item, dict) and item.get("name") == "511 SF Bay"
            for item in attributions
        ):
            attributions.append({
                "name": "511 SF Bay",
                "license": "511 Open Data terms",
                "url": "https://511.org/open-data",
            })
            attributions_path.write_text(
                json.dumps(attributions, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


def validate_scoped_database(
    connection: sqlite3.Connection,
    provider_ids: list[str],
) -> None:
    if not has_ownership_schema(connection):
        raise ValueError("Staged database is missing provider ownership metadata.")
    validate(connection)
    placeholders = ",".join("?" for _ in provider_ids)
    for entity_type, table, column in (
        ("raw_stops", "raw_stops", "stop_id"),
        ("routes", "routes", "route_id"),
        ("trips", "trips", "trip_id"),
        ("calendar", "calendar", "service_id"),
    ):
        orphan_count = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM provider_entities owned
            LEFT JOIN {table} actual ON actual.{column} = owned.key_1
            WHERE owned.entity_type = ?
              AND owned.provider_id IN ({placeholders})
              AND actual.{column} IS NULL
            """,
            (entity_type, *provider_ids),
        ).fetchone()[0]
        if orphan_count:
            raise ValueError(
                f"Provider ownership contains {orphan_count} orphaned {entity_type} rows."
            )
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "transfers" in tables:
        transfer_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(transfers)")
        }
        required_transfer_columns = {
            "from_stop_id",
            "to_stop_id",
            "from_trip_id",
            "to_trip_id",
            "from_route_id",
            "to_route_id",
        }
        if required_transfer_columns.issubset(transfer_columns):
            transfer_identity = (
                "actual.from_stop_id || char(31) || actual.to_stop_id || char(31) || "
                "actual.from_trip_id || char(31) || actual.to_trip_id || char(31) || "
                "actual.from_route_id || char(31) || actual.to_route_id"
            )
            orphan_count = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM provider_entities owned
                WHERE owned.entity_type = 'transfers'
                  AND owned.provider_id IN ({placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM transfers actual
                      WHERE owned.key_1 = {transfer_identity}
                  )
                """,
                tuple(provider_ids),
            ).fetchone()[0]
            if orphan_count:
                raise ValueError(
                    f"Provider ownership contains {orphan_count} orphaned transfers rows."
                )


def prepare_staging_release(
    data_root: Path,
    source_database: Path,
    source_stop_data: Path,
    release_id: str,
) -> Path:
    release_dir = data_root / "releases" / release_id
    release_dir.mkdir(parents=True, exist_ok=False)
    clone_database(source_database, release_dir / "departures.sqlite")
    shutil.copytree(source_stop_data, release_dir / "stop-data", symlinks=True)
    source_metadata = source_database.parent / "release-metadata.json"
    if source_metadata.is_file():
        metadata = json.loads(source_metadata.read_text(encoding="utf-8"))
    else:
        metadata = {}
    metadata["releaseID"] = release_id
    (release_dir / "release-metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return release_dir


def replace_link(link: Path, target: str) -> None:
    temporary = link.with_name(f".{link.name}.scoped-next-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def link_target(path: Path) -> str | None:
    return os.readlink(path) if path.is_symlink() else None


def activate_release(data_root: Path, release_dir: Path) -> dict[Path, str | None]:
    links = {
        data_root / "current-release": link_target(data_root / "current-release"),
        data_root / "current": link_target(data_root / "current"),
        data_root / "departures-current.sqlite": link_target(
            data_root / "departures-current.sqlite"
        ),
    }
    target = os.path.relpath(release_dir, data_root)
    try:
        replace_link(data_root / "current-release", target)
        replace_link(data_root / "current", f"{target}/stop-data")
        replace_link(
            data_root / "departures-current.sqlite",
            f"{target}/departures.sqlite",
        )
    except Exception:
        restore_links(data_root, links)
        raise
    return links


def restore_links(data_root: Path, links: dict[Path, str | None]) -> None:
    for path, target in links.items():
        if target is None:
            path.unlink(missing_ok=True)
        else:
            replace_link(path, target)


def run_readiness(
    repository_root: Path,
    environment: dict[str, str],
    expected_release_id: str,
) -> None:
    compose_file = repository_root / "deploy" / "static-departures.compose.yml"
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d", "--build"],
        cwd=repository_root,
        env=environment,
        check=True,
    )
    container = environment.get(
        "STATIC_DEPARTURES_CONTAINER_NAME", STATIC_CONTAINER_DEFAULT
    )
    timeout = int(environment.get("HEALTH_TIMEOUT_SECONDS", "45"))
    interval = float(environment.get("HEALTH_INTERVAL_SECONDS", "2"))
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "python3",
                    "-c",
                    "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/static-departures/health', timeout=5).read().decode())",
                ],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            if payload.get("status") not in (None, "ok"):
                raise ValueError("static departures health returned a non-ok status")
            actual_release_id = str(
                payload.get("database", {}).get("releaseID", "")
            )
            if actual_release_id != expected_release_id:
                raise ValueError(
                    f"runtime release mismatch: expected {expected_release_id}, "
                    f"got {actual_release_id or '<missing>'}"
                )
            return
        except (subprocess.CalledProcessError, OSError, ValueError, json.JSONDecodeError) as error:
            last_error = error
            time.sleep(interval)
    raise RuntimeError(f"Static departures readiness timed out: {last_error}")


def print_dry_run(
    scope_label: str,
    providers: list[StaticProvider],
    data_root: Path,
) -> None:
    release = current_release_path(data_root)
    print("Mode: scoped dry-run")
    print(f"Requested scope: {scope_label}")
    print("\nResolved providers:")
    for provider in providers:
        print(f"  - {provider.provider_id}")
    print(f"\nCurrent release: {release or '<missing>'}")
    print("\nProduction switch:")
    print("  NO")


def scoped_rebuild(
    repository_root: Path,
    data_root: Path,
    providers: list[StaticProvider],
    environment: dict[str, str],
) -> Path:
    source_database = current_database_path(data_root)
    source_stop_data = current_stop_data_path(data_root)
    release_id = f"scoped-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    release_dir = timed_stage(
        "prepare-staging",
        lambda: prepare_staging_release(
            data_root,
            source_database,
            source_stop_data,
            release_id,
        ),
    )
    staged_database = release_dir / "departures.sqlite"
    provider_ids = [provider.provider_id for provider in providers]
    try:
        connection = sqlite3.connect(staged_database)
        try:
            if not has_ownership_schema(connection):
                raise ValueError(
                    "Active static departures database has no provider ownership metadata; "
                    "run the canonical full pipeline once before using scoped rebuild."
                )
            timed_stage(
                "delete-provider-data",
                lambda: delete_provider_data(connection, provider_ids),
            )
            austrian = [provider for provider in providers if provider.kind == "austrian"]
            if austrian:
                timed_stage(
                    "import-austrian",
                    lambda: import_austrian(
                        connection,
                        austrian,
                        source_stop_data,
                        repository_root,
                        data_root,
                        environment,
                        int(environment.get("STATIC_DEPARTURES_DAYS", "15")),
                    ),
                )
            if any(provider.kind == "germany" for provider in providers):
                timed_stage(
                    "import-germany",
                    lambda: import_germany(
                        connection,
                        next(provider for provider in providers if provider.kind == "germany"),
                        source_stop_data,
                        repository_root,
                        environment,
                        int(environment.get("STATIC_DEPARTURES_DAYS", "15")),
                    ),
                )
            external = [provider for provider in providers if provider.kind == "external"]
            if external:
                external_artifacts = timed_stage(
                    "resolve-external-artifacts",
                    lambda: {
                        provider.provider_id: resolve_external_artifact(provider, environment)
                        for provider in external
                    },
                )
                timed_stage(
                    "import-external",
                    lambda: import_external(
                        connection,
                        external,
                        source_stop_data,
                        repository_root,
                        environment,
                        int(environment.get("STATIC_DEPARTURES_DAYS", "15")),
                        artifacts=external_artifacts,
                    ),
                )
                timed_stage(
                    "build-external-static-assets",
                    lambda: build_external_static_assets(
                        release_dir,
                        external,
                        repository_root,
                        external_artifacts,
                    ),
                )
            timed_stage(
                "canonical-stops",
                lambda: resolve_canonical_stops(connection, provider_ids=provider_ids),
            )
            timed_stage(
                "terminal-stops",
                lambda: update_terminal_stops(connection, provider_ids=provider_ids),
            )
            scoped_city_ids = provider_city_ids(connection, provider_ids)
            timed_stage(
                "city-stops",
                lambda: rebuild_city_stops(connection, scoped_city_ids),
            )
            timed_stage(
                "city-departure-modes",
                lambda: rebuild_city_departure_modes(connection, scoped_city_ids),
            )
            version = str(uuid.uuid4())

            def finalize_database() -> None:
                update_release_metadata(connection, release_id, version)
                validate_scoped_database(connection, provider_ids)
                connection.commit()

            timed_stage(
                "metadata-validation",
                finalize_database,
            )
        finally:
            connection.close()

        links = timed_stage("activate-release", lambda: activate_release(data_root, release_dir))
        try:
            timed_stage(
                "readiness",
                lambda: run_readiness(repository_root, environment, release_id),
            )
        except Exception:
            restore_links(data_root, links)
            raise
        return release_dir
    except Exception:
        shutil.rmtree(release_dir, ignore_errors=True)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manually rebuild selected static-departures providers."
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--provider", default="", help="Canonical provider/source ID")
    scope.add_argument("--country", default="", help="ISO country code")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and print the scope without downloading, importing, or publishing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repository_root = Path(
        os.environ.get("REPO", str(Path(__file__).resolve().parents[1]))
    ).resolve()
    data_root = Path(
        os.environ.get("DATA_ROOT", "/srv/haltewecker/data")
    ).resolve()
    scope_label, providers = resolve_scope(
        repository_root,
        provider_id=args.provider,
        country=args.country,
    )
    if args.dry_run:
        print_dry_run(scope_label, providers, data_root)
        return 0

    print("Mode: SCOPED PIPELINE")
    print(f"Requested scope: {scope_label}")
    print("Selected providers: " + ", ".join(provider.provider_id for provider in providers))
    release = scoped_rebuild(repository_root, data_root, providers, dict(os.environ))
    print(f"Published scoped release: {release}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        print(f"[SCOPED PIPELINE] ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
