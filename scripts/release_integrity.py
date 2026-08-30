"""Reusable candidate-release integrity checks."""

from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from pathlib import Path
from typing import Iterable, Mapping

from artifact_provenance import artifact_provenance


VALID_CLASSIFICATIONS = {"required", "optional", "conditional"}


def _classification(source: Mapping[str, object]) -> str:
    value = str(source.get("classification", "required")).strip().lower()
    if value not in VALID_CLASSIFICATIONS:
        raise ValueError(
            f"External GTFS source {source.get('id', '<unknown>')} has invalid "
            f"classification {value!r}."
        )
    return value


def _is_active(source: Mapping[str, object], environ: Mapping[str, str]) -> bool:
    classification = _classification(source)
    if classification == "required":
        return True
    if classification == "conditional":
        activation_env = str(source.get("activationEnv", "")).strip()
        return bool(activation_env and environ.get(activation_env, "").strip())
    return False


def validate_artifact_entry(
    source_id: str,
    entry: Mapping[str, object],
    *,
    base_dir: Path | None = None,
) -> tuple[str, int, Path]:
    digest = entry.get("sha256")
    size = entry.get("size")
    raw_path = entry.get("path")
    if not isinstance(digest, str) or not digest:
        raise ValueError(f"Artifact provenance is missing for {source_id}")
    if not isinstance(size, int) or size <= 0:
        raise ValueError(f"Artifact size provenance is missing for {source_id}")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError(f"Artifact path is missing for {source_id}")
    path = Path(raw_path)
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    if not path.exists():
        raise ValueError(f"Artifact path is missing for {source_id}: {path}")
    actual_digest, actual_size = artifact_provenance(path)
    if actual_digest != digest or actual_size != size:
        raise ValueError(f"Artifact checksum/path mismatch for {source_id}")
    return digest, size, path


def validate_candidate_sources(
    registry: Iterable[Mapping[str, object]],
    candidate_external: Mapping[str, object],
    manifest_city_ids: set[str],
    repository_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    environment = environ if environ is not None else os.environ
    seen: set[str] = set()
    for source in registry:
        source_id = str(source.get("id", ""))
        if not source_id:
            raise ValueError("External GTFS source id is empty")
        if source_id in seen:
            raise ValueError(f"Duplicate external GTFS source id: {source_id}")
        seen.add(source_id)
        entry = candidate_external.get(source_id)
        has_path = isinstance(entry, Mapping) and bool(entry.get("path"))
        if _is_active(source, environment) and not has_path:
            raise ValueError(
                f"Expected external source is missing from candidate: {source_id}"
            )
        if source.get("importIntoStaticDepartures") is True and not has_path:
            raise ValueError(
                f"Static-enabled source is missing from import plan: {source_id}"
            )
        if source_id != "511-bay-area":
            continue
        cities_path = repository_root / str(source["cities"])
        expected_cities = {
            str(city["id"])
            for city in json.loads(cities_path.read_text(encoding="utf-8"))
        }
        missing = sorted(expected_cities - manifest_city_ids)
        if missing:
            raise ValueError(
                "511 candidate city coverage is incomplete: " + ", ".join(missing)
            )


def validate_previous_release_sources(
    old_source_ids: Iterable[str],
    candidate_source_ids: set[str],
    *,
    allowlisted: Iterable[str] = (),
) -> None:
    allowed = set(allowlisted)
    missing = sorted(
        source_id
        for source_id in set(old_source_ids)
        if source_id not in candidate_source_ids and source_id not in allowed
    )
    if missing:
        raise ValueError("Candidate lost sources from active release: " + ", ".join(missing))


def validate_previous_release_cities(
    old_city_ids: Iterable[str],
    candidate_city_ids: set[str],
) -> None:
    missing = sorted(
        city_id
        for city_id in set(old_city_ids)
        if city_id not in candidate_city_ids
    )
    if missing:
        raise ValueError("Candidate lost cities from active release: " + ", ".join(missing))


def _artifact_entries(payload: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    entries: dict[str, Mapping[str, object]] = {}
    for group in ("sources", "external", "supplemental"):
        values = payload.get(group)
        if not isinstance(values, Mapping):
            continue
        for source_id, entry in values.items():
            if isinstance(entry, Mapping) and entry.get("path"):
                entries.setdefault(str(source_id), entry)
    return entries


def _artifact_signature(entry: Mapping[str, object]) -> tuple[str, int] | None:
    digest = entry.get("sha256")
    size = entry.get("size")
    if (
        not isinstance(digest, str)
        or not digest
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
    ):
        return None
    return digest, size


def _source_city_ids(
    registry: Iterable[Mapping[str, object]],
    repository_root: Path,
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for source in registry:
        source_id = str(source.get("id", "")).strip()
        cities_path = source.get("cities")
        if not source_id or not isinstance(cities_path, str):
            continue
        path = repository_root / cities_path
        try:
            cities = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(cities, list):
            continue
        for city in cities:
            if isinstance(city, Mapping) and city.get("id"):
                result.setdefault(str(city["id"]), set()).add(source_id)
    return result


def _city_source_ids(
    city: Mapping[str, object],
    *,
    registry_city_ids: Mapping[str, set[str]],
) -> set[str]:
    explicit = city.get("sourceIDs")
    if isinstance(explicit, list):
        source_ids = {str(value).strip() for value in explicit if str(value).strip()}
        if source_ids:
            return source_ids
    explicit_source = str(city.get("sourceID", "")).strip()
    if explicit_source:
        return {explicit_source}

    city_id = str(city.get("id", "")).strip()
    configured = set(registry_city_ids.get(city_id, set()))
    if configured:
        return configured

    # Legacy manifests did not persist provider ownership. These country
    # fallbacks cover the built-in branches while remaining fail-closed for
    # unknown ownership.
    country = str(city.get("country", "")).strip().upper()
    fallback_source = {
        "DE": "germany",
        "CH": "swiss",
        "NL": "netherlands",
    }.get(country)
    return {fallback_source} if fallback_source else set()


def _safe_release_file(root: Path, relative_path: object) -> Path | None:
    if not isinstance(relative_path, str) or not relative_path.strip():
        return None
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _load_city_stops(
    stop_data_root: Path,
    city: Mapping[str, object],
) -> list[Mapping[str, object]]:
    path = _safe_release_file(stop_data_root, city.get("url"))
    if path is None or not path.is_file():
        raise ValueError(
            f"Cannot prove active stops for lost city {city.get('id', '<unknown>')}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Cannot parse active stops for lost city {city.get('id', '<unknown>')}"
        ) from error
    if not isinstance(payload, list):
        raise ValueError(
            f"Active stops package is invalid for lost city {city.get('id', '<unknown>')}"
        )
    stops = [
        stop
        for stop in payload
        if isinstance(stop, Mapping) and str(stop.get("id", "")).strip()
    ]
    if not stops:
        raise ValueError(
            f"Active stops package is empty for lost city {city.get('id', '<unknown>')}"
        )
    return stops


def _json_count(path: Path, *, departure_payload: bool = False) -> int:
    if not path.is_file():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot parse active service package: {path}") from error
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, Mapping):
        return 0
    if departure_payload:
        stops = payload.get("stops")
        if isinstance(stops, Mapping):
            return sum(len(items) for items in stops.values() if isinstance(items, list))
        return sum(len(items) for items in payload.values() if isinstance(items, list))
    return len(payload)


def _city_service_counts(stop_data_root: Path, city: Mapping[str, object]) -> dict[str, int]:
    city_id = str(city.get("id", "")).strip()
    stop_stem = Path(str(city.get("url", ""))).stem
    stems = [stem for stem in (city_id, stop_stem) if stem]

    def first_count(directory: str, *, departure_payload: bool = False) -> int:
        for stem in dict.fromkeys(stems):
            path = stop_data_root / directory / f"{stem}.json"
            if path.is_file():
                return _json_count(path, departure_payload=departure_payload)
        return 0

    return {
        "routes": first_count("routes"),
        "trips": first_count("trips"),
        "departures": first_count("departures", departure_payload=True),
    }


def _gtfs_stops_by_id(path: Path) -> dict[str, list[dict[str, str]]]:
    required_files = {"stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}
    if path.is_dir():
        missing = sorted(name for name in required_files if not (path / name).is_file())
        if missing:
            raise ValueError(
                "GTFS source import evidence is missing required files: "
                + ", ".join(missing)
            )
        stops_path = path / "stops.txt"
        stream = stops_path.open("r", encoding="utf-8-sig", newline="")
        close_stream = True
        archive = None
    else:
        archive: zipfile.ZipFile | None = None
        try:
            archive = zipfile.ZipFile(path)
            names = set(archive.namelist())
            missing = required_files - names
            if missing:
                raise ValueError(
                    "GTFS source import evidence is missing required files: "
                    + ", ".join(sorted(missing))
                )
            if archive.testzip() is not None:
                raise ValueError(f"GTFS source import evidence has a corrupt member: {path}")
            member = "stops.txt"
        except (OSError, zipfile.BadZipFile) as error:
            raise ValueError(f"GTFS source import evidence is unreadable: {path}") from error
        except ValueError:
            if archive is not None:
                archive.close()
            raise
        stream = io.TextIOWrapper(archive.open(member), encoding="utf-8-sig", newline="")
        close_stream = True

    try:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or "stop_id" not in reader.fieldnames:
            raise ValueError(f"GTFS source stops.txt has no stop_id column: {path}")
        stops_by_id: dict[str, list[dict[str, str]]] = {}
        for row in reader:
            stop_id = str(row.get("stop_id", "")).strip()
            if stop_id:
                stops_by_id.setdefault(stop_id, []).append(dict(row))
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError(f"GTFS source stops.txt is invalid: {path}") from error
    finally:
        if close_stream:
            stream.close()
        if archive is not None:
            archive.close()
    if not stops_by_id:
        raise ValueError(f"GTFS source stops.txt has no data rows: {path}")
    return stops_by_id


def _source_stop_id_variants(
    stop_id: str,
    source: Mapping[str, object],
) -> set[str]:
    variants = {stop_id}
    for prefix_key in ("identifierPrefix", "namespace"):
        prefix = str(source.get(prefix_key, ""))
        if prefix and stop_id.startswith(prefix):
            variants.add(stop_id[len(prefix):])
    return variants


def _stop_identity_matches(
    active_stop: Mapping[str, object],
    candidate_stop: Mapping[str, object],
) -> bool:
    active_name = " ".join(str(active_stop.get("name", "")).casefold().split())
    candidate_name = " ".join(
        str(candidate_stop.get("stop_name", "")).casefold().split()
    )
    if active_name and candidate_name and active_name == candidate_name:
        return True

    try:
        active_lat = float(active_stop["latitude"])
        active_lon = float(active_stop["longitude"])
        candidate_lat = float(candidate_stop["stop_lat"])
        candidate_lon = float(candidate_stop["stop_lon"])
    except (KeyError, TypeError, ValueError):
        return True
    if abs(active_lat - candidate_lat) <= 0.0001 and abs(active_lon - candidate_lon) <= 0.0001:
        return True

    # A reused source ID with both a different name and distant coordinates is
    # a different physical stop, so the active stop was legitimately removed.
    return not (active_name and candidate_name and active_name != candidate_name)


def _source_config_by_id(registry: Iterable[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    return {
        str(source.get("id")): source
        for source in registry
        if str(source.get("id", "")).strip()
    }


def validate_previous_release_city_retirements(
    *,
    old_manifest: Mapping[str, object],
    candidate_manifest: Mapping[str, object],
    active_stop_data: Path,
    candidate_stop_data: Path,
    active_artifacts: Mapping[str, object],
    candidate_artifacts: Mapping[str, object],
    registry: Iterable[Mapping[str, object]],
    repository_root: Path,
    candidate_artifacts_root: Path | None = None,
) -> list[dict[str, object]]:
    registry = list(registry)
    old_cities = {
        str(city.get("id")): city
        for city in old_manifest.get("cities", [])
        if isinstance(city, Mapping) and city.get("id")
    }
    candidate_city_ids = {
        str(city.get("id"))
        for city in candidate_manifest.get("cities", [])
        if isinstance(city, Mapping) and city.get("id")
    }
    missing = sorted(set(old_cities) - candidate_city_ids)
    if not missing:
        return []

    candidate_cities = [
        city for city in candidate_manifest.get("cities", []) if isinstance(city, Mapping)
    ]
    registry_city_ids = _source_city_ids(registry, repository_root)
    source_config = _source_config_by_id(registry)
    old_entries = _artifact_entries(active_artifacts)
    candidate_entries = _artifact_entries(candidate_artifacts)
    failures: list[str] = []
    retirements: list[dict[str, object]] = []

    for city_id in missing:
        city = old_cities[city_id]
        source_ids = _city_source_ids(city, registry_city_ids=registry_city_ids)
        if len(source_ids) != 1:
            failures.append(f"{city_id}: source ownership is not provable")
            continue
        source_id = next(iter(source_ids))
        active_entry = old_entries.get(source_id)
        candidate_entry = candidate_entries.get(source_id)
        if not active_entry or not candidate_entry:
            failures.append(f"{city_id}: source {source_id} is missing from release metadata")
            continue
        active_signature = _artifact_signature(active_entry)
        candidate_signature = _artifact_signature(candidate_entry)
        if active_signature is None or candidate_signature is None:
            failures.append(f"{city_id}: source artifact provenance is incomplete")
            continue
        if active_signature == candidate_signature:
            failures.append(f"{city_id}: source artifact did not change")
            continue
        candidate_status = str(candidate_entry.get("status", "")).strip().lower()
        if candidate_status not in {"updated", "unchanged", "local"}:
            failures.append(
                f"{city_id}: source import status is {candidate_entry.get('status')}"
            )
            continue
        try:
            _, _, candidate_path = validate_artifact_entry(
                source_id,
                candidate_entry,
                base_dir=candidate_artifacts_root,
            )
            candidate_stops_by_id = _gtfs_stops_by_id(candidate_path)
        except ValueError as error:
            failures.append(f"{city_id}: source import is not valid ({error})")
            continue

        owned_candidate_cities = [
            candidate_city
            for candidate_city in candidate_cities
            if source_id in _city_source_ids(
                candidate_city,
                registry_city_ids=registry_city_ids,
            )
        ]
        if not owned_candidate_cities:
            failures.append(f"{city_id}: candidate has no imported city for source {source_id}")
            continue
        missing_candidate_packages: list[str] = []
        for candidate_city in owned_candidate_cities:
            candidate_package = _safe_release_file(
                candidate_stop_data, candidate_city.get("url")
            )
            if candidate_package is None or not candidate_package.is_file():
                missing_candidate_packages.append(
                    str(candidate_city.get("id", "<unknown>"))
                )
        if missing_candidate_packages:
            failures.append(
                f"{city_id}: candidate source packages are incomplete "
                f"({', '.join(missing_candidate_packages)})"
            )
            continue

        active_stops = _load_city_stops(active_stop_data, city)
        source_config_entry = source_config.get(source_id, {})
        matching_active_stops = [
            stop
            for stop in active_stops
            if any(
                _stop_identity_matches(stop, candidate_stop)
                for raw_stop_id in _source_stop_id_variants(
                    str(stop.get("id", "")).strip(), source_config_entry
                )
                for candidate_stop in candidate_stops_by_id.get(raw_stop_id, [])
            )
        ]
        if matching_active_stops:
            matching_ids = ", ".join(
                str(stop.get("id", "<unknown>")) for stop in matching_active_stops
            )
            failures.append(
                f"{city_id}: active stop still exists in candidate source "
                f"({matching_ids})"
            )
            continue

        service_counts = _city_service_counts(active_stop_data, city)
        if any(service_counts.values()):
            failures.append(
                f"{city_id}: active city has service "
                f"routes={service_counts['routes']} trips={service_counts['trips']} "
                f"departures={service_counts['departures']}"
            )
            continue

        retirements.append({
            "cityID": city_id,
            "sourceID": source_id,
            "reason": "upstream-stop-removal",
            "activeStopCount": len(active_stops),
            "serviceCounts": service_counts,
        })

    if failures:
        raise ValueError(
            "Candidate lost cities from active release: "
            + ", ".join(missing)
            + "; legitimate retirement proof failed: "
            + "; ".join(failures)
        )
    return retirements
