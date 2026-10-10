"""Read-only coverage contract for every published city before activation.

Created by Anton on 2026-10-10.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.artifact_provenance import artifact_provenance
from scripts.build_stop_packages import nl_city_ids
from scripts.external_gtfs import load_external_cities, load_external_gtfs_sources
from scripts.static_departures_fallback import FALLBACK_PROFILE, excluded_fallback_providers
from services.static_departures_runtime import load_release_manifest
from services.vbb_overlay_provider import VBBOverlayProviderAdapter


class CityCoverageError(ValueError):
    """A release loses a supported city or cannot route its published stops."""


def read_object(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CityCoverageError(f"invalid JSON object: {path}")
    return payload


def stop_data_root(release: Path) -> Path:
    payload = read_object(release / "release.json")
    entry = payload.get("stopData", {})
    relative = entry.get("path", "stop-data") if isinstance(entry, dict) else entry
    return (release / str(relative)).resolve(strict=True)


def city_manifest(root: Path) -> dict[str, dict]:
    rows = read_object(root / "manifest.json").get("cities")
    if not isinstance(rows, list) or not rows:
        raise CityCoverageError("supported city manifest is empty")
    cities = {str(row["id"]): row for row in rows if isinstance(row, dict) and row.get("id")}
    if len(cities) != len(rows):
        raise CityCoverageError("supported city manifest contains duplicate or invalid IDs")
    return cities


def _package_cities(repository: Path) -> set[str]:
    sources = load_external_gtfs_sources(repository / "config/external-gtfs-sources.json")
    cities = {str(city["id"]) for source in sources
              if source.get("importIntoStaticDepartures") is not True
              for city in load_external_cities(source, repository)}
    cities.update(str(city["id"]) for city in json.loads((repository / "config/swiss-cities.json").read_text()))
    cities.update(nl_city_ids(json.loads((repository / "config/cities.json").read_text())))
    return cities


def package_generation(payload: dict) -> str:
    generated = str(payload.get("generatedAt", ""))
    if len(generated) == 8 and generated.isdigit():
        return datetime.strptime(generated, "%Y%m%d").date().isoformat()
    try:
        instant = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        if instant.tzinfo is not None:
            instant = instant.astimezone(ZoneInfo(str(payload.get("timezone") or "Europe/Berlin")))
        return instant.date().isoformat()
    except ValueError:
        return ""


def scheduled_stop(connection, city: str, prefixes: tuple[str, ...], *, catalog: str = "city_stops", temporal: str = "", today: date, exact: bool = False) -> str | None:
    """Find one scheduled public stop with indexed lookups, including namespaces."""
    stop_column = "stop_id" if exact else "canonical_stop_id"
    raw_matches = " OR ".join(f"rs.{stop_column}=?||cs.stop_id" for _ in prefixes)
    parameters = (city, *prefixes, today.strftime("%Y%m%d"))
    row = connection.execute(
        f"""SELECT cs.stop_id FROM {catalog} AS cs WHERE cs.city_id=? AND EXISTS (
            SELECT 1 FROM raw_stops AS rs WHERE ({raw_matches}) AND EXISTS (
                SELECT 1 FROM stop_times AS st JOIN trips AS t ON t.trip_id=st.trip_id
                WHERE st.raw_stop_id=rs.stop_id AND EXISTS (
                    SELECT 1 FROM {temporal}active_services AS a WHERE a.service_id=t.service_id AND a.service_date>=?
                )
            )
        ) LIMIT 1""", parameters,
    ).fetchone()
    return str(row[0]) if row else None


def validate_city_coverage(release: Path, runtime: tuple[str, ...], repository: Path, *, reference: Path | None = None, validation_date: date | None = None) -> dict:
    release = release.resolve(strict=True)
    today = validation_date or datetime.now(ZoneInfo("Europe/Berlin")).date()
    stop_root = stop_data_root(release)
    payload = read_object(release / "release.json")
    stop_manifest = read_object(stop_root / "manifest.json")
    cities = city_manifest(stop_root)
    if stop_manifest.get("releaseID") != payload.get("releaseID"):
        raise CityCoverageError("candidate stop-data generation does not match releaseID")
    if reference is not None:
        lost = set(city_manifest(stop_data_root(reference.resolve(strict=True)))) - set(cities)
        if lost:
            raise CityCoverageError("candidate removed supported cities: " + ", ".join(sorted(lost)))
    database = release / "departures.sqlite"
    if not database.is_file():
        raise CityCoverageError("complete fallback database is missing")
    digest, size = artifact_provenance(database)
    fallback_reference = payload.get("fallbackDatabase")
    if reference is not None and not isinstance(fallback_reference, dict):
        raise CityCoverageError("candidate has no pinned fallback database reference")
    if fallback_reference is not None and (
        fallback_reference.get("path") != "departures.sqlite"
        or fallback_reference.get("sha256") != digest
        or fallback_reference.get("size") != size
    ):
        raise CityCoverageError("fallback database hash/size does not match candidate")
    package_cities = _package_cities(repository)
    manifest = load_release_manifest(release, provider_ids=runtime)
    checks = []
    package_checks = []
    package_count = 0
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as legacy, \
         closing(sqlite3.connect(manifest.common_database_path.resolve().as_uri() + "?mode=ro", uri=True)) as common:
        metadata = dict(legacy.execute("SELECT key,value FROM metadata"))
        excluded = set()
        if metadata.get("fallbackProfile") == FALLBACK_PROFILE:
            try:
                shard_ids = json.loads(metadata.get("shardProviderIDs", "[]"))
                excluded_ids = json.loads(metadata.get("excludedProviderIDs", "[]"))
                expected = set(excluded_fallback_providers(repository, runtime))
                if not isinstance(shard_ids, list) or set(shard_ids) != set(runtime) or not isinstance(excluded_ids, list) or set(excluded_ids) != expected:
                    raise ValueError("provider set mismatch")
                excluded = expected
            except (ValueError, TypeError) as error:
                raise CityCoverageError("compact fallback provider contract mismatch") from error
        if metadata.get("releaseID") != payload.get("releaseID") or metadata.get("stopDataReleaseID") != stop_manifest.get("releaseID") or metadata.get("stopDataManifestVersion") != stop_manifest.get("version"):
            raise CityCoverageError("fallback database generation does not match candidate")
        start, end = metadata.get("validFrom", ""), metadata.get("validThrough", "")
        if not start or not end or not (date.fromisoformat(start) <= today and date.fromisoformat(end) >= today + timedelta(days=1)):
            raise CityCoverageError("fallback schedule does not cover today and tomorrow")
        required_modes = {(str(source["id"]), str(city["id"]))
                          for source in load_external_gtfs_sources(repository / "config/external-gtfs-sources.json")
                          if source.get("importIntoStaticDepartures") is True and str(source["id"]) not in excluded
                          for city in load_external_cities(source, repository) if str(city["id"]) in cities}
        actual_modes = {(str(row[0]), str(row[1])) for row in legacy.execute("SELECT provider_id,city_id FROM provider_city_modes")}
        if not required_modes <= actual_modes:
            raise CityCoverageError("fallback database lost configured city/provider mappings: " + repr(sorted(required_modes - actual_modes)))
        for city, entry in sorted(cities.items()):
            path = (stop_root / str(entry.get("url", ""))).resolve(strict=True)
            if not path.is_relative_to(stop_root):
                raise CityCoverageError(f"stop package escapes candidate: {city}")
            stops = json.loads(path.read_text(encoding="utf-8"))
            ids = {str(item["id"]) for item in stops if isinstance(item, dict) and item.get("id")} if isinstance(stops, list) else set()
            if not ids or len(ids) != len(stops) or entry.get("stopCount", len(stops)) != len(stops):
                raise CityCoverageError(f"empty or incomplete candidate stop package: {city}")
            providers = {str(row[0]) for row in common.execute("SELECT provider_id FROM provider_city_modes WHERE city_id=?", (city,))}
            shard = bool(providers) and providers <= set(runtime)
            if city in package_cities and not shard:
                departures = read_object(stop_root / "departures" / f"{city}.json")
                if not isinstance(departures.get("stops"), dict) or not any(
                    isinstance(rows, list) and rows for rows in departures["stops"].values()
                ):
                    raise CityCoverageError(f"package-only city has no schedule: {city}")
                if package_generation(departures) != stop_manifest.get("version"):
                    raise CityCoverageError(f"package-only city schedule belongs to a different generation: {city}")
                package_checks.append({"cityID": city, "stopCount": len(departures["stops"])})
                package_count += 1
                continue
            if VBBOverlayProviderAdapter.handles_city(city):
                adapter = VBBOverlayProviderAdapter(stop_root)
                instant = datetime.now(ZoneInfo("Europe/Berlin"))
                if validation_date is not None:
                    instant = datetime.combine(today, datetime.min.time(), ZoneInfo("Europe/Berlin"))
                until = instant + timedelta(days=1)
                try:
                    # Prefer the known public/native mapping, then inspect other public stops.
                    ordered = (["476861"] if "476861" in ids else []) + sorted(ids - {"476861"})
                    probe = next((stop for stop in ordered if adapter.board(city, stop, 1, instant, until)), None)
                    if not probe:
                        raise CityCoverageError("Berlin overlay has no scheduled public stop")
                    if not adapter.board(city, probe, 1, until, until + timedelta(hours=6)):
                        raise CityCoverageError("Berlin overlay has no scheduled departures tomorrow")
                finally:
                    adapter.close()
                checks.append({"cityID": city, "stopID": probe, "hasSchedule": True, "backend": "vbb-overlay", "from": instant.isoformat(), "to": until.isoformat()})
                continue
            connection = common if shard else legacy
            table = "provider_city_stops" if shard else "city_stops"
            routed = {str(row[0]) for row in connection.execute(f"SELECT stop_id FROM {table} WHERE city_id=?", (city,))}
            if not ids <= routed:
                raise CityCoverageError(f"candidate cannot route {len(ids - routed)} published stops: {city}")
            probe = None
            timezone = "Europe/Berlin"
            if shard:
                for provider in sorted(providers):
                    refs = manifest.providers[provider]
                    if not refs.temporal.valid_from or not refs.temporal.valid_through or not (
                        date.fromisoformat(refs.temporal.valid_from) <= today
                        and date.fromisoformat(refs.temporal.valid_through) >= today + timedelta(days=1)
                    ):
                        raise CityCoverageError(f"provider schedule does not cover today and tomorrow: {provider}")
                    with closing(sqlite3.connect(refs.structural.database_path.resolve().as_uri() + "?mode=ro", uri=True)) as structural:
                        structural.execute("ATTACH DATABASE ? AS calendar", (refs.temporal.database_path.resolve().as_uri() + "?mode=ro",))
                        structural.execute("ATTACH DATABASE ? AS catalog", (manifest.common_database_path.resolve().as_uri() + "?mode=ro",))
                        mode = common.execute("SELECT timezone,stop_id_prefix,mode FROM provider_city_modes WHERE city_id=? AND provider_id=?", (city, provider)).fetchone()
                        timezone = str(mode[0])
                        probe = scheduled_stop(structural, city, tuple(dict.fromkeys(("", str(mode[1])))), catalog="catalog.provider_city_stops", temporal="calendar.", today=today, exact=mode[2] == "exact-stop-with-parent-fallback")
                        if probe:
                            break
            else:
                modes = legacy.execute("SELECT timezone,stop_id_prefix FROM provider_city_modes WHERE city_id=?", (city,)).fetchall()
                timezone = str(modes[0][0]) if modes else timezone
                mode = legacy.execute("SELECT mode FROM city_departure_modes WHERE city_id=?", (city,)).fetchone()
                probe = scheduled_stop(legacy, city, tuple(dict.fromkeys(("", *(str(row[1]) for row in modes)))), today=today, exact=bool(mode and mode[0] == "exact-stop-with-parent-fallback"))
            checks.append({"cityID": city, "stopID": probe or sorted(ids)[0], "hasSchedule": bool(probe), "timezone": timezone, "backend": "shard" if shard else "fallback", "from": today.isoformat() + "T00:00:00", "to": end + "T23:59:59"})
    return {"status": "PASS", "releaseID": payload["releaseID"], "supportedCities": len(cities), "apiCities": len(checks), "packageCities": package_count, "checks": checks, "packageChecks": package_checks, "fallbackDatabaseVersion": metadata["databaseVersion"], "fallbackSHA256": digest}


def require_preserved_schedules(candidate: dict, rollback: dict) -> None:
    available = {row["cityID"] for row in candidate["checks"] if row["hasSchedule"]}
    lost = {row["cityID"] for row in rollback["checks"] if row["hasSchedule"]} - available
    if lost:
        raise CityCoverageError("candidate lost scheduled cities: " + ", ".join(sorted(lost)))
