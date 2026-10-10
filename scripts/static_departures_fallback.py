"""Select fallback inputs without duplicating authoritative provider shards.

Created by Anton on 2026-10-10.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from scripts.external_gtfs import load_external_cities, load_external_gtfs_sources
from scripts.provider_artifact_capabilities import HYBRID_RUNTIME, SHARD_RUNTIME, provider_capability


FALLBACK_PROFILE = "hybrid-only"


def excluded_fallback_providers(repository: Path, shard_providers: tuple[str, ...], *, sources_path: Path | None = None) -> tuple[str, ...]:
    """Keep complete feeds for mixed cities that still require legacy routing."""
    eligible = set(shard_providers)
    unsupported = {provider for provider in eligible if not all(
        provider_capability(repository, provider, capability)
        for capability in (SHARD_RUNTIME, HYBRID_RUNTIME)
    )}
    if unsupported:
        raise ValueError("unsupported fallback shard providers: " + ", ".join(sorted(unsupported)))
    sources = load_external_gtfs_sources(sources_path or repository / "config/external-gtfs-sources.json")
    providers_by_city: dict[str, set[str]] = defaultdict(set)
    cities_by_provider: dict[str, set[str]] = defaultdict(set)
    for source in sources:
        if source.get("importIntoStaticDepartures") is not True and str(source["id"]) not in eligible:
            continue
        provider = str(source["id"])
        for city in load_external_cities(source, repository):
            identifier = str(city["id"])
            providers_by_city[identifier].add(provider)
            cities_by_provider[provider].add(identifier)
    excluded = {provider for provider in eligible if cities_by_provider[provider] and all(
        providers_by_city[city] <= eligible for city in cities_by_provider[provider]
    )}
    # German memberships exclude the external city catalogs in both builders.
    if "germany" in eligible:
        excluded.add("germany")
    return tuple(sorted(excluded))
