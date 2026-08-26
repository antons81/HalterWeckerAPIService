"""Fail-safe cache for immutable external GTFS build artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from .artifact_provenance import artifact_provenance
except ImportError:
    from artifact_provenance import artifact_provenance


CACHE_SCHEMA_VERSION = 1
CTA_PROVIDER_ID = "cta-chicago"
FEATURE_GATE = "HALTEWECKER_EXTERNAL_BUILD_CACHE"
PROVIDER_ALLOWLIST = "HALTEWECKER_EXTERNAL_BUILD_CACHE_PROVIDERS"
DEFAULT_PROVIDER_ALLOWLIST = frozenset({CTA_PROVIDER_ID})
CACHEABLE_PROVIDER_CITY_IDS = {
    "cta-chicago": "chicago",
    "translink": "vancouver",
    "king-county-metro": "seattle",
    "stm-montreal": "montreal",
}
BUILDER_FAMILY = "external-standard-immutable-v1"
BUILDER_INPUTS = (
    "scripts/external_gtfs.py",
    "scripts/external_staging.py",
    "scripts/gtfs_csv.py",
    "scripts/build_stop_packages.py",
    "scripts/external_build_cache.py",
)


def expected_artifacts(city_id: str) -> tuple[tuple[str, str], ...]:
    if not city_id or Path(city_id).name != city_id:
        raise ValueError(f"invalid cache city id: {city_id!r}")
    return (
        ("stops", f"stops/{city_id}.json"),
        ("routes", f"routes/{city_id}.json"),
        ("lineMembership", "line-membership.json"),
    )


SENSITIVE_CONFIG_MARKERS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "apikey",
    "api_key",
)


class CacheKeyUnavailable(ValueError):
    """The cache key cannot be computed safely for the current input."""


@dataclass(frozen=True)
class CacheKey:
    value: str
    raw_sha256: str
    provider_config_fingerprint: str
    city_config_fingerprint: str
    builder_fingerprint: str
    supplemental_inputs_fingerprint: str = ""
    provider_id: str = ""
    city_id: str = ""


@dataclass(frozen=True)
class CacheLookup:
    status: str
    reason: str
    key: CacheKey
    directory: Path | None = None
    manifest: dict[str, object] | None = None


@dataclass(frozen=True)
class CacheRestore:
    stops: list[dict[str, object]]
    lines_by_stop_id: dict[str, dict[str, dict[str, object]]]


def cache_enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = environ if environ is not None else os.environ
    return values.get(FEATURE_GATE, "0").strip() == "1"


def cache_provider_allowed(
    provider_id: str,
    environ: Mapping[str, str] | None = None,
) -> bool:
    values = environ if environ is not None else os.environ
    configured = values.get(PROVIDER_ALLOWLIST)
    if configured is None:
        return provider_id in DEFAULT_PROVIDER_ALLOWLIST
    requested = {item.strip() for item in configured.split(",") if item.strip()}
    return provider_id in requested


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _supplemental_inputs_fingerprint(
    supplemental_input_digests: Mapping[str, str] | None,
) -> str:
    values = supplemental_input_digests or {}
    normalized = {
        str(name): str(digest)
        for name, digest in sorted(values.items(), key=lambda item: str(item[0]))
    }
    return _sha256_json(normalized)


def _safe_config_value(key: str, value: object) -> object:
    lowered = key.casefold()
    if any(marker in lowered for marker in SENSITIVE_CONFIG_MARKERS):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(child_key): _safe_config_value(str(child_key), child_value)
            for child_key, child_value in sorted(
                value.items(), key=lambda item: str(item[0])
            )
        }
    if isinstance(value, list):
        return [_safe_config_value(key, item) for item in value]
    return value


def provider_config_fingerprint(source: Mapping[str, object]) -> str:
    payload = {
        str(key): _safe_config_value(str(key), value)
        for key, value in sorted(source.items(), key=lambda item: str(item[0]))
        if str(key) not in {"url", "scopedURL", "localPath"}
    }
    return _sha256_json(payload)


def _file_fingerprint(repository_root: Path, relative_paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in relative_paths:
        path = repository_root / relative
        if not path.is_file():
            raise CacheKeyUnavailable(f"builder input is missing: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def builder_fingerprint(repository_root: Path) -> str:
    return _file_fingerprint(repository_root, BUILDER_INPUTS)


def city_config_fingerprint(repository_root: Path, source: Mapping[str, object]) -> str:
    cities_path_value = source.get("cities")
    if not isinstance(cities_path_value, str) or not cities_path_value.strip():
        raise CacheKeyUnavailable("city configuration path is missing")
    cities_path = (repository_root / cities_path_value).resolve()
    try:
        cities_path.relative_to(repository_root.resolve())
    except ValueError as error:
        raise CacheKeyUnavailable("city configuration is outside repository") from error
    if not cities_path.is_file():
        raise CacheKeyUnavailable(f"city configuration is missing: {cities_path_value}")
    digest = hashlib.sha256()
    digest.update(cities_path_value.encode("utf-8"))
    digest.update(b"\0")
    with cities_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_key(
    *,
    repository_root: Path,
    provider_id: str,
    raw_sha256: str,
    source: Mapping[str, object],
    city_id: str = "",
    supplemental_input_digests: Mapping[str, str] | None = None,
) -> CacheKey:
    if not raw_sha256 or len(raw_sha256) != 64:
        raise CacheKeyUnavailable("raw GTFS SHA256 is unavailable")
    provider_fingerprint = provider_config_fingerprint(source)
    cities_fingerprint = city_config_fingerprint(repository_root, source)
    build_fingerprint = builder_fingerprint(repository_root)
    supplemental_fingerprint = _supplemental_inputs_fingerprint(
        supplemental_input_digests
    )
    payload = {
        "cacheSchemaVersion": CACHE_SCHEMA_VERSION,
        "builderFamily": BUILDER_FAMILY,
        "providerID": provider_id,
        "cityID": city_id,
        "rawGTFSsha256": raw_sha256,
        "providerConfigFingerprint": provider_fingerprint,
        "cityConfigFingerprint": cities_fingerprint,
        "builderFingerprint": build_fingerprint,
        "supplementalInputsFingerprint": supplemental_fingerprint,
    }
    return CacheKey(
        value=_sha256_json(payload),
        raw_sha256=raw_sha256,
        provider_config_fingerprint=provider_fingerprint,
        city_config_fingerprint=cities_fingerprint,
        builder_fingerprint=build_fingerprint,
        supplemental_inputs_fingerprint=supplemental_fingerprint,
        provider_id=provider_id,
        city_id=city_id,
    )


def _safe_relative_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return None
    path = Path(value)
    if ".." in path.parts:
        return None
    return path


def _manifest_matches(
    manifest: object,
    expected: CacheKey,
    directory: Path,
    provider_id: str,
    artifacts: tuple[tuple[str, str], ...],
) -> tuple[bool, str]:
    if not isinstance(manifest, dict):
        return False, "manifest is not an object"
    if manifest.get("cacheSchemaVersion") != CACHE_SCHEMA_VERSION:
        return False, "cache schema mismatch"
    if manifest.get("providerID") != provider_id:
        return False, "provider mismatch"
    if manifest.get("key") != expected.value:
        return False, "cache key mismatch"
    if manifest.get("rawGTFSsha256") != expected.raw_sha256:
        return False, "raw GTFS digest mismatch"
    if (
        manifest.get("providerConfigFingerprint")
        != expected.provider_config_fingerprint
    ):
        return False, "provider configuration fingerprint mismatch"
    if manifest.get("cityConfigFingerprint") != expected.city_config_fingerprint:
        return False, "city configuration fingerprint mismatch"
    if manifest.get("builderFingerprint") != expected.builder_fingerprint:
        return False, "builder fingerprint mismatch"
    if manifest.get("status") != "complete" or manifest.get("complete") is not True:
        return False, "cache is not complete"
    cached_outputs = manifest.get("cachedOutputs")
    if not isinstance(cached_outputs, list):
        return False, "cached outputs are missing"
    entries: dict[str, dict[str, object]] = {}
    for item in cached_outputs:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return False, "cached output entry is invalid"
        entries[str(item["name"])] = item
    if set(entries) != {name for name, _path in artifacts}:
        return False, "cached output set mismatch"
    for name, expected_path in artifacts:
        entry = entries[name]
        if entry.get("path") != expected_path:
            return False, f"cached output path mismatch for {name}"
        relative = _safe_relative_path(entry.get("path"))
        if relative is None:
            return False, f"cached output path is unsafe for {name}"
        artifact = directory / relative
        if not artifact.is_file():
            return False, f"cached artifact is missing for {name}"
        try:
            digest, size = artifact_provenance(artifact)
        except (OSError, ValueError):
            return False, f"cached artifact cannot be read for {name}"
        if digest != entry.get("sha256") or size != entry.get("size"):
            return False, f"cached artifact digest mismatch for {name}"
    return True, "validated manifest and artifacts"


class ExternalBuildCache:
    """Atomic, provider-scoped storage for immutable build artifacts."""

    def __init__(
        self,
        root: Path | str,
        provider_id: str = CTA_PROVIDER_ID,
        city_id: str = "chicago",
    ) -> None:
        self.root = Path(root) / provider_id
        self.provider_id = provider_id
        self.city_id = city_id
        self.artifacts = expected_artifacts(city_id)

    def _directory(self, key: CacheKey) -> Path:
        return self.root / key.value

    def lookup(self, key: CacheKey) -> CacheLookup:
        directory = self._directory(key)
        if not directory.exists():
            return CacheLookup("MISS", "cache key not found", key)
        manifest_path = directory / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            shutil.rmtree(directory, ignore_errors=True)
            return CacheLookup("INVALID", "manifest is unreadable", key)
        valid, reason = _manifest_matches(
            manifest,
            key,
            directory,
            self.provider_id,
            self.artifacts,
        )
        if not valid:
            shutil.rmtree(directory, ignore_errors=True)
            return CacheLookup("INVALID", reason, key)
        return CacheLookup("HIT", reason, key, directory, manifest)

    def restore(self, lookup: CacheLookup, output: Path) -> CacheRestore:
        if lookup.status != "HIT" or lookup.directory is None:
            raise ValueError("only a validated cache HIT can be restored")
        source_directory = lookup.directory
        stops: list[dict[str, object]] | None = None
        lines_by_stop_id: dict[str, dict[str, dict[str, object]]] | None = None
        for name, relative_value in self.artifacts:
            source = source_directory / relative_value
            if name == "lineMembership":
                payload = json.loads(source.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("cached line membership is invalid")
                value = payload.get("linesByStopID")
                if not isinstance(value, dict):
                    raise ValueError("cached line membership payload is invalid")
                for stop_id, route_lines in value.items():
                    if not isinstance(stop_id, str) or not isinstance(
                        route_lines, dict
                    ):
                        raise TypeError("cached line membership entry is invalid")
                    for route_id, line in route_lines.items():
                        if not isinstance(route_id, str) or not isinstance(line, dict):
                            raise TypeError("cached line membership route is invalid")
                        if not isinstance(line.get("routeID"), str):
                            raise TypeError(
                                "cached line membership route ID is invalid"
                            )
                        names = line.get("names")
                        if not isinstance(names, list) or not all(
                            isinstance(name, str) for name in names
                        ):
                            raise ValueError("cached line membership names are invalid")
                lines_by_stop_id = value
                continue
            destination = output / relative_value
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.cache-tmp")
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            if name == "stops":
                payload = json.loads(destination.read_text(encoding="utf-8"))
                if not isinstance(payload, list) or not all(
                    isinstance(item, dict) for item in payload
                ):
                    raise ValueError("cached stops payload is invalid")
                stops = payload
        if stops is None or lines_by_stop_id is None:
            raise ValueError("cache restore is incomplete")
        return CacheRestore(stops, lines_by_stop_id)

    def persist(
        self,
        key: CacheKey,
        output: Path,
        lines_by_stop_id: Mapping[str, Mapping[str, Mapping[str, object]]],
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        final_directory = self._directory(key)
        temporary_directory = Path(
            tempfile.mkdtemp(prefix=f".{key.value}.tmp-", dir=self.root)
        )
        try:
            for _name, relative_value in self.artifacts[:2]:
                source = output / relative_value
                if not source.is_file():
                    raise ValueError(f"cacheable output is missing: {relative_value}")
                destination = temporary_directory / relative_value
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)

            line_path = temporary_directory / "line-membership.json"
            line_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": CACHE_SCHEMA_VERSION,
                        "linesByStopID": lines_by_stop_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )

            cached_outputs: list[dict[str, object]] = []
            for name, relative_value in self.artifacts:
                digest, size = artifact_provenance(temporary_directory / relative_value)
                cached_outputs.append(
                    {
                        "name": name,
                        "path": relative_value,
                        "sha256": digest,
                        "size": size,
                    }
                )
            manifest = {
                "cacheSchemaVersion": CACHE_SCHEMA_VERSION,
                "providerID": self.provider_id,
                "key": key.value,
                "rawGTFSsha256": key.raw_sha256,
                "providerConfigFingerprint": key.provider_config_fingerprint,
                "cityConfigFingerprint": key.city_config_fingerprint,
                "builderFingerprint": key.builder_fingerprint,
                "createdAt": _now(),
                "cachedOutputs": cached_outputs,
                "status": "complete",
                "complete": True,
            }
            manifest_path = temporary_directory / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_directory, final_directory)
            temporary_directory = Path()
        finally:
            if temporary_directory != Path():
                shutil.rmtree(temporary_directory, ignore_errors=True)
