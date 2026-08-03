#!/usr/bin/env python3
"""Shared declarative registry for Austrian MVO GTFS sources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "config" / "austrian-sources.json"


def load_austrian_sources(path: Path = DEFAULT_REGISTRY) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("Austrian source registry must be a non-empty JSON array.")

    sources: list[dict[str, object]] = []
    source_ids: set[str] = set()
    prefixes: set[str] = set()
    dataset_ids: set[int] = set()
    city_sources: dict[str, list[str]] = {}
    for source in payload:
        if not isinstance(source, dict):
            raise ValueError("Each Austrian source must be an object.")
        source_id = str(source.get("id", "")).strip()
        prefix = str(source.get("identifierPrefix", "")).strip()
        dataset_id = source.get("datasetId")
        cities = source.get("cities")
        if not source_id or source_id in source_ids:
            raise ValueError(f"Duplicate or empty Austrian source id: {source_id!r}")
        if not prefix or prefix in prefixes:
            raise ValueError(f"Duplicate or empty Austrian identifier prefix: {prefix!r}")
        if not isinstance(dataset_id, int) or dataset_id <= 0 or dataset_id in dataset_ids:
            raise ValueError(f"Invalid or duplicate MVO dataset id for {source_id}")
        if not isinstance(cities, list) or not cities or not all(isinstance(city, str) and city for city in cities):
            raise ValueError(f"Austrian source {source_id} must declare cities")
        if source.get("preserveStopIDs", False) and source_id != "vor":
            raise ValueError("Only the VOR source may preserve legacy stop IDs")
        if source.get("linzPriority") is not None and not isinstance(source["linzPriority"], int):
            raise ValueError(f"Invalid Linz priority for {source_id}")
        for city in cities:
            city_sources.setdefault(city, []).append(source_id)
        source_ids.add(source_id)
        prefixes.add(prefix)
        dataset_ids.add(dataset_id)
        sources.append(source)

    if set(city_sources) != {"wien", "graz", "linz", "salzburg", "innsbruck", "klagenfurt", "st-poelten", "bregenz"}:
        raise ValueError("Austrian registry must cover exactly the eight configured cities")
    if city_sources["linz"] != ["ooevv", "linz-ag"]:
        raise ValueError("Linz sources must be ordered as ooevv, linz-ag")
    if any(len(ids) != 1 and city != "linz" for city, ids in city_sources.items()):
        raise ValueError("Only Linz may have multiple Austrian source feeds")
    return sources


def sources_by_id(path: Path = DEFAULT_REGISTRY) -> dict[str, dict[str, object]]:
    return {str(source["id"]): source for source in load_austrian_sources(path)}


def sources_for_city(city_id: str, path: Path = DEFAULT_REGISTRY) -> list[dict[str, object]]:
    return [source for source in load_austrian_sources(path) if city_id in source["cities"]]


def public_stop_id(source: dict[str, object], raw_stop_id: str) -> str:
    if source.get("preserveStopIDs", False):
        return raw_stop_id
    return f"{source['identifierPrefix']}{raw_stop_id}"


def internal_prefix(source: dict[str, object]) -> str:
    return str(source["identifierPrefix"])


def all_source_ids(sources: Iterable[dict[str, object]]) -> set[str]:
    return {str(source["id"]) for source in sources}
