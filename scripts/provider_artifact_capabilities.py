"""Configuration-backed eligibility for persistent provider artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


CAPABILITIES_FIELD = "artifactCapabilities"
PERSISTENT_NORMALIZED = "persistentNormalizedArtifactEligible"
STATIC_PROVIDER = "staticProviderArtifactEligible"
SHARD_RUNTIME = "shardRuntimeEligible"
SUPPORTED_CAPABILITIES = (PERSISTENT_NORMALIZED, STATIC_PROVIDER, SHARD_RUNTIME)


def _sources_path(repository_root: Path) -> Path:
    return repository_root / "config" / "external-gtfs-sources.json"


def _source_capabilities(repository_root: Path, provider_id: str) -> Mapping[str, object]:
    try:
        payload = json.loads(_sources_path(repository_root).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("External GTFS source registry is unavailable") from error
    if not isinstance(payload, list):
        raise ValueError("External GTFS source registry must be a list")
    source = next(
        (
            item
            for item in payload
            if isinstance(item, dict) and item.get("id") == provider_id
        ),
        None,
    )
    if source is None:
        return {}
    capabilities = source.get(CAPABILITIES_FIELD, {})
    if not isinstance(capabilities, dict):
        raise ValueError(f"Provider {provider_id} artifact capabilities must be an object")
    unknown = set(capabilities) - set(SUPPORTED_CAPABILITIES)
    if unknown:
        raise ValueError(
            f"Provider {provider_id} has unknown artifact capabilities: {sorted(unknown)}"
        )
    if any(not isinstance(value, bool) for value in capabilities.values()):
        raise ValueError(f"Provider {provider_id} artifact capabilities must be boolean")
    return capabilities


def provider_capability(
    repository_root: Path,
    provider_id: str,
    capability: str,
) -> bool:
    """Return an explicit capability; absent or unknown providers fail closed."""
    if capability not in SUPPORTED_CAPABILITIES:
        raise ValueError(f"Unknown provider artifact capability: {capability}")
    return _source_capabilities(repository_root, provider_id).get(capability) is True


def provider_artifact_eligible(repository_root: Path, provider_id: str) -> bool:
    """Return whether all persistent artifact layers are explicitly enabled."""
    return all(
        provider_capability(repository_root, provider_id, capability)
        for capability in SUPPORTED_CAPABILITIES
    )
