"""Fail-safe cache for immutable external GTFS build artifacts."""

from __future__ import annotations

import errno
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


CACHE_SCHEMA_VERSION = 3
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
TRANSFORMED_CACHE_PROVIDER_IDS = frozenset(
    {
        *CACHEABLE_PROVIDER_CITY_IDS,
        "finland-hsl",
        "poland-warsaw",
        "wmata-bus",
        "mbta-boston",
        "sweden",
        "mta-ny-nyct-bus",
        "ttc-surface",
        "ttc-subway",
        "israel-mot",
        "australia-transport-nsw",
    }
)
TRANSFORMED_CACHE_LAYER = "external-transformed-provider-v1"
TRANSFORMED_FEATURE_GATE = "HALTEWECKER_EXTERNAL_TRANSFORMED_BUILD_CACHE"
BUILDER_FAMILY = "external-standard-immutable-v1"
# These are the source fields that can change the immutable stop/route/trip/
# line projection stored by ExternalBuildCache. Departure-window settings are
# deliberately excluded: departures are rebuilt separately because they are
# date-dependent.
IMMUTABLE_OUTPUT_CONFIG_KEYS = (
    "agencyID",
    "buildRoutes",
    "buildStops",
    "buildTripIndex",
    "exclusiveCityPartition",
    "filterCitiesByProvider",
    "mergeGroup",
    "namespace",
    "publishPassengerStopIDs",
    "stopIDMode",
    "supplementalStopCatalog",
)
BUILDER_INPUTS = (
    "scripts/external_gtfs.py",
    "scripts/external_staging.py",
    "scripts/gtfs_csv.py",
    "scripts/build_stop_packages.py",
)
LEGACY_BUILDER_INPUTS = (*BUILDER_INPUTS, "scripts/external_build_cache.py")


def expected_artifacts(
    city_id: str,
    *,
    city_ids: tuple[str, ...] | None = None,
    include_trip_index: bool = True,
) -> tuple[tuple[str, str], ...]:
    normalized_city_ids = city_ids or (city_id,)
    if not normalized_city_ids or any(
        not value or Path(value).name != value for value in normalized_city_ids
    ):
        raise ValueError(f"invalid cache city ids: {normalized_city_ids!r}")
    artifacts: list[tuple[str, str]] = []
    single_city = len(normalized_city_ids) == 1
    for normalized_city_id in normalized_city_ids:
        artifacts.extend(
            [
                (
                    "stops" if single_city else f"stops:{normalized_city_id}",
                    f"stops/{normalized_city_id}.json",
                ),
                (
                    "routes" if single_city else f"routes:{normalized_city_id}",
                    f"routes/{normalized_city_id}.json",
                ),
            ]
        )
        if include_trip_index:
            artifacts.append(
                (
                    "tripIndexBase"
                    if single_city
                    else f"tripIndexBase:{normalized_city_id}",
                    f"trip-index-base/{normalized_city_id}.json",
                )
            )
    artifacts.append(("lineMembership", "line-membership.json"))
    return tuple(artifacts)


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
    projection_fingerprint: str = ""
    city_ids: tuple[str, ...] = ()
    legacy_value: str = ""
    legacy_provider_config_fingerprint: str = ""
    legacy_builder_fingerprint: str = ""


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
    package_stops_by_city_id: dict[str, list[dict[str, object]]] | None = None


DEPARTURE_CACHE_SCHEMA_VERSION = 1
DEPARTURE_OUTPUT_SCHEMA_VERSION = 2
DEPARTURE_CACHE_LAYER = "external-departure-partition-v1"
DEPARTURE_CACHE_FEATURE_GATE = "HALTEWECKER_EXTERNAL_DEPARTURE_CACHE"
DEPARTURE_CACHE_PROVIDER_ALLOWLIST = "HALTEWECKER_EXTERNAL_DEPARTURE_CACHE_PROVIDERS"


@dataclass(frozen=True)
class DeparturePartitionKey:
    value: str
    provider_id: str
    city_id: str
    service_date: str
    raw_sha256: str
    structural_input_key: str
    calendar_fingerprint: str
    builder_fingerprint: str
    config_fingerprint: str


@dataclass(frozen=True)
class DeparturePartitionLookup:
    status: str
    reason: str
    key: DeparturePartitionKey
    directory: Path | None = None


def departure_cache_enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = environ if environ is not None else os.environ
    return values.get(DEPARTURE_CACHE_FEATURE_GATE, "0").strip() == "1"


def departure_provider_allowed(
    provider_id: str,
    environ: Mapping[str, str] | None = None,
) -> bool:
    values = environ if environ is not None else os.environ
    configured = values.get(DEPARTURE_CACHE_PROVIDER_ALLOWLIST)
    if configured is None:
        return False
    return provider_id in {item.strip() for item in configured.split(",") if item.strip()}


def departure_config_fingerprint(source: Mapping[str, object]) -> str:
    keys = (
        "departurePackageDays",
        "namespace",
        "timezone",
        "stopIDMode",
        "publishPassengerStopIDs",
        "exclusiveCityPartition",
        "supplementalStopCatalog",
    )
    payload = {
        key: _safe_config_value(key, source.get(key))
        for key in keys
        if key in source
    }
    return _sha256_json(payload)


def departure_builder_fingerprint(repository_root: Path) -> str:
    return _file_fingerprint(
        repository_root,
        (*BUILDER_INPUTS, "scripts/external_build_cache.py"),
    )


def departure_partition_key(
    *,
    repository_root: Path,
    provider_id: str,
    city_id: str,
    service_date: str,
    raw_sha256: str,
    structural_input_key: str,
    calendar_fingerprint: str,
    source: Mapping[str, object],
) -> DeparturePartitionKey:
    if not provider_id or not city_id or not service_date:
        raise CacheKeyUnavailable("departure partition identity is incomplete")
    if len(raw_sha256) != 64 or not calendar_fingerprint:
        raise CacheKeyUnavailable("departure partition provenance is incomplete")
    builder = departure_builder_fingerprint(repository_root)
    config = departure_config_fingerprint(source)
    payload = {
        "cacheSchemaVersion": DEPARTURE_CACHE_SCHEMA_VERSION,
        "outputSchemaVersion": DEPARTURE_OUTPUT_SCHEMA_VERSION,
        "cacheLayer": DEPARTURE_CACHE_LAYER,
        "providerID": provider_id,
        "cityID": city_id,
        "serviceDate": service_date,
        "rawGTFSsha256": raw_sha256,
        "structuralInputKey": structural_input_key,
        "calendarFingerprint": calendar_fingerprint,
        "builderFingerprint": builder,
        "departureConfigFingerprint": config,
    }
    return DeparturePartitionKey(
        value=_sha256_json(payload),
        provider_id=provider_id,
        city_id=city_id,
        service_date=service_date,
        raw_sha256=raw_sha256,
        structural_input_key=structural_input_key,
        calendar_fingerprint=calendar_fingerprint,
        builder_fingerprint=builder,
        config_fingerprint=config,
    )


def _materialize_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    try:
        try:
            os.link(source, temporary)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


class DeparturePartitionCache:
    """Atomic cache for one v2 departure service-date partition."""

    def __init__(self, root: Path | str, provider_id: str) -> None:
        self.root = Path(root) / provider_id
        self.provider_id = provider_id

    def _directory(self, key: DeparturePartitionKey) -> Path:
        return self.root / key.city_id / key.service_date / key.value

    def lookup(self, key: DeparturePartitionKey) -> DeparturePartitionLookup:
        directory = self._directory(key)
        if not directory.is_dir():
            return DeparturePartitionLookup("MISS", "cache key not found", key)
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            partition = directory / "partition.json"
            digest, size = artifact_provenance(partition)
        except (OSError, ValueError, TypeError):
            shutil.rmtree(directory, ignore_errors=True)
            return DeparturePartitionLookup("INVALID", "partition cache is unreadable", key)
        expected = {
            "cacheSchemaVersion": DEPARTURE_CACHE_SCHEMA_VERSION,
            "outputSchemaVersion": DEPARTURE_OUTPUT_SCHEMA_VERSION,
            "cacheLayer": DEPARTURE_CACHE_LAYER,
            "providerID": key.provider_id,
            "cityID": key.city_id,
            "serviceDate": key.service_date,
            "key": key.value,
            "rawGTFSsha256": key.raw_sha256,
            "structuralInputKey": key.structural_input_key,
            "calendarFingerprint": key.calendar_fingerprint,
            "builderFingerprint": key.builder_fingerprint,
            "departureConfigFingerprint": key.config_fingerprint,
        }
        if not isinstance(manifest, dict) or any(manifest.get(name) != value for name, value in expected.items()):
            shutil.rmtree(directory, ignore_errors=True)
            return DeparturePartitionLookup("INVALID", "partition manifest mismatch", key)
        if manifest.get("status") != "complete" or manifest.get("sha256") != digest or manifest.get("size") != size:
            shutil.rmtree(directory, ignore_errors=True)
            return DeparturePartitionLookup("INVALID", "partition provenance mismatch", key)
        return DeparturePartitionLookup("HIT", "validated partition", key, directory)

    def restore(self, lookup: DeparturePartitionLookup, destination: Path) -> None:
        if lookup.status != "HIT" or lookup.directory is None:
            raise ValueError("only a validated departure partition HIT can be restored")
        _materialize_file(lookup.directory / "partition.json", destination)

    def persist(self, key: DeparturePartitionKey, source: Path) -> None:
        digest, size = artifact_provenance(source)
        directory = self._directory(key)
        if directory.exists():
            return
        directory.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{key.value}.", dir=directory.parent))
        try:
            _materialize_file(source, temporary / "partition.json")
            manifest = {
                "cacheSchemaVersion": DEPARTURE_CACHE_SCHEMA_VERSION,
                "outputSchemaVersion": DEPARTURE_OUTPUT_SCHEMA_VERSION,
                "cacheLayer": DEPARTURE_CACHE_LAYER,
                "providerID": key.provider_id,
                "cityID": key.city_id,
                "serviceDate": key.service_date,
                "key": key.value,
                "rawGTFSsha256": key.raw_sha256,
                "structuralInputKey": key.structural_input_key,
                "calendarFingerprint": key.calendar_fingerprint,
                "builderFingerprint": key.builder_fingerprint,
                "departureConfigFingerprint": key.config_fingerprint,
                "status": "complete",
                "sha256": digest,
                "size": size,
            }
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            try:
                os.replace(temporary, directory)
                temporary = None
            except FileExistsError:
                pass
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)


def cache_enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = environ if environ is not None else os.environ
    return values.get(FEATURE_GATE, "0").strip() == "1"


def transformed_cache_enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = environ if environ is not None else os.environ
    return values.get(TRANSFORMED_FEATURE_GATE, "0").strip() == "1"


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


def projection_fingerprint(
    source: Mapping[str, object],
    city_ids: tuple[str, ...],
    merge_group_members: tuple[str, ...] = (),
) -> str:
    """Hash the source-to-city transformation semantics explicitly."""
    payload = {
        "namespace": source.get("namespace", ""),
        "mergeGroup": source.get("mergeGroup"),
        "filterCitiesByProvider": source.get("filterCitiesByProvider", False),
        "exclusiveCityPartition": source.get("exclusiveCityPartition", False),
        "agencyID": source.get("agencyID"),
        "stopIDMode": source.get("stopIDMode", "exact"),
        "publishPassengerStopIDs": source.get("publishPassengerStopIDs", False),
        "cityIDs": list(city_ids),
        "mergeGroupMembers": list(merge_group_members),
    }
    return _sha256_json(payload)


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
    """Fingerprint only configuration consumed by immutable output builders."""
    payload = {
        key: _safe_config_value(key, source.get(key))
        for key in IMMUTABLE_OUTPUT_CONFIG_KEYS
        if key in source
    }
    return _sha256_json(payload)


def legacy_provider_config_fingerprint(source: Mapping[str, object]) -> str:
    """Return the pre-boundary config fingerprint for cache migration."""
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


def legacy_builder_fingerprint(repository_root: Path) -> str:
    """Return the pre-boundary builder fingerprint for cache migration."""
    return _file_fingerprint(repository_root, LEGACY_BUILDER_INPUTS)


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
    city_ids: tuple[str, ...] | None = None,
    merge_group_members: tuple[str, ...] = (),
    supplemental_input_digests: Mapping[str, str] | None = None,
) -> CacheKey:
    if not raw_sha256 or len(raw_sha256) != 64:
        raise CacheKeyUnavailable("raw GTFS SHA256 is unavailable")
    provider_fingerprint = provider_config_fingerprint(source)
    legacy_provider_fingerprint = legacy_provider_config_fingerprint(source)
    cities_fingerprint = city_config_fingerprint(repository_root, source)
    build_fingerprint = builder_fingerprint(repository_root)
    legacy_build_fingerprint = legacy_builder_fingerprint(repository_root)
    normalized_city_ids = tuple(city_ids or ((city_id,) if city_id else ()))
    if not normalized_city_ids or any(
        not value or Path(value).name != value for value in normalized_city_ids
    ):
        raise CacheKeyUnavailable("cache city IDs are unavailable")
    projection = projection_fingerprint(
        source,
        normalized_city_ids,
        merge_group_members,
    )
    supplemental_fingerprint = _supplemental_inputs_fingerprint(
        supplemental_input_digests
    )
    def payload_for(
        provider_config: str,
        builder: str,
    ) -> dict[str, object]:
        return {
            "cacheSchemaVersion": CACHE_SCHEMA_VERSION,
            "builderFamily": BUILDER_FAMILY,
            "cacheLayer": TRANSFORMED_CACHE_LAYER,
            "providerID": provider_id,
            "cityID": city_id,
            "cityIDs": list(normalized_city_ids),
            "rawGTFSsha256": raw_sha256,
            "providerConfigFingerprint": provider_config,
            "cityConfigFingerprint": cities_fingerprint,
            "builderFingerprint": builder,
            "projectionFingerprint": projection,
            "supplementalInputsFingerprint": supplemental_fingerprint,
        }

    return CacheKey(
        value=_sha256_json(payload_for(provider_fingerprint, build_fingerprint)),
        raw_sha256=raw_sha256,
        provider_config_fingerprint=provider_fingerprint,
        city_config_fingerprint=cities_fingerprint,
        builder_fingerprint=build_fingerprint,
        supplemental_inputs_fingerprint=supplemental_fingerprint,
        provider_id=provider_id,
        city_id=city_id,
        projection_fingerprint=projection,
        city_ids=normalized_city_ids,
        legacy_value=_sha256_json(
            payload_for(legacy_provider_fingerprint, legacy_build_fingerprint)
        ),
        legacy_provider_config_fingerprint=legacy_provider_fingerprint,
        legacy_builder_fingerprint=legacy_build_fingerprint,
    )


def _safe_relative_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return None
    path = Path(value)
    if ".." in path.parts:
        return None
    return path


def _immutable_trip_index_base_is_valid(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    for trip_id, entry in payload.items():
        if (
            not isinstance(trip_id, str)
            or not isinstance(entry, dict)
            or set(entry) != {"r"}
            or not isinstance(entry["r"], str)
        ):
            return False
    return True


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
    if manifest.get("cacheLayer") != TRANSFORMED_CACHE_LAYER:
        return False, "cache layer mismatch"
    if manifest.get("builderFamily") != BUILDER_FAMILY:
        return False, "builder family mismatch"
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
    if manifest.get("projectionFingerprint") != expected.projection_fingerprint:
        return False, "projection fingerprint mismatch"
    expected_city_ids = expected.city_ids or (
        (expected.city_id,) if expected.city_id else ()
    )
    if manifest.get("cityIDs") != list(expected_city_ids):
        return False, "cache city set mismatch"
    manifest_supplemental = manifest.get("supplementalInputsFingerprint")
    if manifest_supplemental is not None:
        if manifest_supplemental != expected.supplemental_inputs_fingerprint:
            return False, "supplemental input fingerprint mismatch"
    elif expected.supplemental_inputs_fingerprint != _supplemental_inputs_fingerprint(
        {}
    ):
        return False, "supplemental input fingerprint is missing"
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
    if len(entries) != len(cached_outputs):
        return False, "cached output names are duplicated"
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
        if (
            name.startswith("tripIndexBase")
            and not _immutable_trip_index_base_is_valid(artifact)
        ):
            return False, f"immutable trip index base is invalid for {name}"
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
        city_ids: tuple[str, ...] | None = None,
        include_trip_index: bool = True,
    ) -> None:
        self.root = Path(root) / provider_id
        self.provider_id = provider_id
        self.city_id = city_id
        self.city_ids = city_ids or (city_id,)
        self.artifacts = expected_artifacts(
            city_id,
            city_ids=self.city_ids,
            include_trip_index=include_trip_index,
        )

    def _directory(self, key: CacheKey) -> Path:
        return self.root / key.value

    def lookup(self, key: CacheKey) -> CacheLookup:
        directory = self._directory(key)
        validation_key = key
        migration = False
        if not directory.exists() and key.legacy_value:
            legacy_key = CacheKey(
                value=key.legacy_value,
                raw_sha256=key.raw_sha256,
                provider_config_fingerprint=key.legacy_provider_config_fingerprint,
                city_config_fingerprint=key.city_config_fingerprint,
                builder_fingerprint=key.legacy_builder_fingerprint,
                supplemental_inputs_fingerprint=key.supplemental_inputs_fingerprint,
                provider_id=key.provider_id,
                city_id=key.city_id,
                projection_fingerprint=key.projection_fingerprint,
                city_ids=key.city_ids,
                legacy_builder_fingerprint=key.legacy_builder_fingerprint,
            )
            legacy_directory = self._directory(legacy_key)
            if legacy_directory.exists():
                directory = legacy_directory
                validation_key = legacy_key
                migration = True
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
            validation_key,
            directory,
            self.provider_id,
            self.artifacts,
        )
        if not valid:
            shutil.rmtree(directory, ignore_errors=True)
            return CacheLookup("INVALID", reason, key)
        if migration:
            reason = "validated legacy manifest after fingerprint boundary change"
        return CacheLookup("HIT", reason, validation_key, directory, manifest)

    def restore(self, lookup: CacheLookup, output: Path) -> CacheRestore:
        if lookup.status != "HIT" or lookup.directory is None:
            raise ValueError("only a validated cache HIT can be restored")
        source_directory = lookup.directory
        package_stops: dict[str, list[dict[str, object]]] = {}
        lines_by_stop_id: dict[str, dict[str, dict[str, object]]] | None = None
        for name, relative_value in self.artifacts:
            source = source_directory / relative_value
            if name == "lineMembership":
                payload = json.loads(source.read_text(encoding="utf-8"))
                if (
                    not isinstance(payload, dict)
                    or payload.get("schemaVersion") != CACHE_SCHEMA_VERSION
                ):
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
                try:
                    os.link(source, temporary)
                except OSError as error:
                    if error.errno != errno.EXDEV:
                        raise
                    shutil.copyfile(source, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            if Path(relative_value).parts[0] == "stops":
                payload = json.loads(destination.read_text(encoding="utf-8"))
                if not isinstance(payload, list) or not all(
                    isinstance(item, dict) for item in payload
                ):
                    raise ValueError("cached stops payload is invalid")
                package_stops[Path(relative_value).stem] = payload
            elif Path(relative_value).parts[0] in {"routes", "trips"}:
                payload = json.loads(destination.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or not all(
                    isinstance(key, str) and isinstance(value, dict)
                    for key, value in payload.items()
                ):
                    raise ValueError(
                        f"cached {Path(relative_value).parts[0]} payload is invalid"
                    )
        if set(package_stops) != set(self.city_ids) or lines_by_stop_id is None:
            raise ValueError("cache restore is incomplete")
        return CacheRestore(
            package_stops[self.city_ids[0]],
            lines_by_stop_id,
            package_stops_by_city_id=package_stops,
        )

    def persist(
        self,
        key: CacheKey,
        output: Path,
        lines_by_stop_id: Mapping[str, Mapping[str, Mapping[str, object]]],
    ) -> None:
        expected_city_ids = key.city_ids or ((key.city_id,) if key.city_id else ())
        if tuple(expected_city_ids) != self.city_ids:
            raise ValueError("cache key city set does not match cache storage")
        self.root.mkdir(parents=True, exist_ok=True)
        final_directory = self._directory(key)
        temporary_directory = Path(
            tempfile.mkdtemp(prefix=f".{key.value}.tmp-", dir=self.root)
        )
        try:
            for _name, relative_value in self.artifacts:
                if _name == "lineMembership":
                    continue
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
                "cacheLayer": TRANSFORMED_CACHE_LAYER,
                "builderFamily": BUILDER_FAMILY,
                "providerID": self.provider_id,
                "key": key.value,
                "rawGTFSsha256": key.raw_sha256,
                "providerConfigFingerprint": key.provider_config_fingerprint,
                "cityConfigFingerprint": key.city_config_fingerprint,
                "builderFingerprint": key.builder_fingerprint,
                "projectionFingerprint": key.projection_fingerprint,
                "cityIDs": list(key.city_ids or self.city_ids),
                "supplementalInputsFingerprint": key.supplemental_inputs_fingerprint,
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
            published = False
            try:
                os.replace(temporary_directory, final_directory)
                published = True
            except OSError as error:
                # Another writer published the same validated key first.
                if (
                    error.errno not in {errno.EEXIST, errno.ENOTEMPTY}
                    or not final_directory.is_dir()
                ):
                    raise
            if published:
                temporary_directory = Path()
        finally:
            if temporary_directory != Path():
                shutil.rmtree(temporary_directory, ignore_errors=True)
