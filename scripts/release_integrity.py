"""Reusable candidate-release integrity checks."""

from __future__ import annotations

import json
import os
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
    *,
    allowlisted: Iterable[str] = (),
) -> None:
    allowed = set(allowlisted)
    missing = sorted(
        city_id
        for city_id in set(old_city_ids)
        if city_id not in candidate_city_ids and city_id not in allowed
    )
    if missing:
        raise ValueError("Candidate lost cities from active release: " + ", ".join(missing))
