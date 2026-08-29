#!/usr/bin/env python3
"""Generic external GTFS source registry and builders (Sweden-first)."""

from __future__ import annotations

import base64
import json
import math
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

try:
    from .gtfs_source_cache import GTFSArtifactCache, REQUIRED_GTFS_FILES
except ImportError:
    from gtfs_source_cache import GTFSArtifactCache, REQUIRED_GTFS_FILES

try:
    from .dynamic_resource_resolver import resolve_gtfs_resource
except ImportError:
    from dynamic_resource_resolver import resolve_gtfs_resource

try:
    from .external_staging import ExternalDepartureStage, ExternalMergeStage, iter_departure_payload, iter_json_array, iter_json_object
except ImportError:
    from external_staging import ExternalDepartureStage, ExternalMergeStage, iter_departure_payload, iter_json_array, iter_json_object

try:
    from .gtfs_agency import agency_scoped_archive
except ImportError:
    from gtfs_agency import agency_scoped_archive

try:
    from .build_stop_packages import (
        build_lines_by_stop_id_noncanonical,
        distance_meters,
        iter_table,
        load_cities,
        load_table,
        normalized,
        write_stop_package,
    )
except ImportError:
    from build_stop_packages import (
        build_lines_by_stop_id_noncanonical,
        distance_meters,
        iter_table,
        load_cities,
        load_table,
        normalized,
        write_stop_package,
    )

try:
    from .kyiv_open_data import KyivResourceCache, _cache_age_text, load_json_resource
except ImportError:
    from kyiv_open_data import KyivResourceCache, _cache_age_text, load_json_resource

try:
    from .artifact_provenance import (
        artifact_provenance,
        canonical_content_provenance,
        provenance_record,
    )
except ImportError:
    from artifact_provenance import (
        artifact_provenance,
        canonical_content_provenance,
        provenance_record,
    )

try:
    from .external_build_cache import (
        CACHEABLE_PROVIDER_CITY_IDS,
        CTA_PROVIDER_ID,
        CacheKey,
        CacheKeyUnavailable,
        ExternalBuildCache,
        cache_enabled,
        cache_provider_allowed,
        cache_key,
    )
except ImportError:
    from external_build_cache import (
        CACHEABLE_PROVIDER_CITY_IDS,
        CTA_PROVIDER_ID,
        CacheKey,
        CacheKeyUnavailable,
        ExternalBuildCache,
        cache_enabled,
        cache_provider_allowed,
        cache_key,
    )


# Auth is resolved at runtime from environment variables. Secrets never appear
# in config JSON. Future sources can register here without touching GTFS parsers.
EXTERNAL_SOURCE_AUTH: dict[str, dict[str, object]] = {
    "sweden": {
        "api_key_env": "SAMTRAFIKEN_STATIC_API_KEY",
        "query_parameter": "key",
        "headers": {
            "Accept-Encoding": "gzip",
            "User-Agent": "HalteWeckerStopPipeline/1.0",
        },
    },
    "511-bay-area": {
        "api_key_env": "API_511_KEY",
        "query_parameter": "api_key",
        "headers": {
            "Accept-Encoding": "gzip",
            "User-Agent": "HalteWeckerStopPipeline/1.0",
        },
    },
    "wmata-bus": {
        "api_key_env": "WMATA_API_KEY",
        "header_name": "api_key",
        "headers": {"Accept-Encoding": "gzip"},
    },
    "wmata-rail": {
        "api_key_env": "WMATA_API_KEY",
        "header_name": "api_key",
        "headers": {"Accept-Encoding": "gzip"},
    },
    "australia-transport-nsw": {
        "api_key_env": "NSW_API_TOKEN",
        "header_name": "Authorization",
        "header_prefix": "apikey ",
        "headers": {
            "Accept-Encoding": "gzip",
            "User-Agent": "HalteWeckerStopPipeline/1.0",
        },
    },
    "australia-transport-canberra": {
        "basic_auth_env": ("CANBERRA_CLIENT_ID", "CANBERRA_CLIENT_SECRET"),
        "headers": {
            "Accept-Encoding": "gzip",
            "User-Agent": "HalteWeckerStopPipeline/1.0",
        },
    },
    "kyiv": {
        "headers": {
            "Accept": "application/zip,application/octet-stream;q=0.9,*/*;q=0.8",
            "Referer": "https://data.kyivcity.gov.ua/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36"
            ),
        },
    },
}

FINLAND_SOURCE_PREFIX = "finland-"

VALID_SOURCE_CLASSIFICATIONS = {"required", "optional", "conditional"}


def source_classification(source: dict[str, object]) -> str:
    value = str(source.get("classification", "required")).strip().lower()
    if value not in VALID_SOURCE_CLASSIFICATIONS:
        raise ValueError(
            f"External GTFS source {source.get('id', '<unknown>')} has invalid "
            f"classification {value!r}."
        )
    return value


def configured_external_url(source: dict[str, object]) -> str:
    """Resolve the canonical feed URL; url wins over scopedURL."""
    for key in ("url", "scopedURL"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def load_external_gtfs_sources(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("External GTFS sources must be a JSON array.")
    sources: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Each external GTFS source must be an object.")
        sources.append(item)
    return sources


def external_gtfs_resilience_policy(
    source: dict[str, object],
) -> dict[str, object] | None:
    raw_policy = source.get("resilientFetch")
    if raw_policy is None:
        return None
    if not isinstance(raw_policy, dict) or raw_policy.get("enabled") is not True:
        raise ValueError(
            f"External GTFS source {source.get('id', '<unknown>')} has invalid resilientFetch."
        )
    allow_stale = raw_policy.get("allowStale")
    retry_attempts = raw_policy.get("retryAttempts")
    metadata_probe = raw_policy.get("metadataProbe", False)
    require_data_rows = raw_policy.get("requireDataRows", True)
    if not isinstance(allow_stale, bool):
        raise ValueError("resilientFetch.allowStale must be a boolean")
    if not isinstance(retry_attempts, int) or not 1 <= retry_attempts <= 5:
        raise ValueError("resilientFetch.retryAttempts must be between 1 and 5")
    if not isinstance(metadata_probe, bool) or not isinstance(require_data_rows, bool):
        raise ValueError("resilientFetch boolean options are invalid")
    return {
        "allowStale": allow_stale,
        "retryAttempts": retry_attempts,
        "metadataProbe": metadata_probe,
        "requireDataRows": require_data_rows,
    }


def validate_kyiv_gtfs_archive(path: Path) -> None:
    """Require the core Kyiv feed tables to contain headers and data rows."""
    try:
        with zipfile.ZipFile(path) as archive:
            for name in REQUIRED_GTFS_FILES:
                with archive.open(name) as stream:
                    rows = [
                        line.decode("utf-8-sig").strip()
                        for line in stream
                        if line.strip()
                    ]
                if len(rows) < 2:
                    raise ValueError(f"Kyiv GTFS {name} has no data rows")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as error:
        raise ValueError("Kyiv GTFS archive is unreadable") from error


def validate_external_gtfs_source(
    source: dict[str, object],
    repository_root: Path,
    *,
    known_source_ids: set[str] | None = None,
    known_prefixes: set[str] | None = None,
    known_namespaces: set[str] | None = None,
) -> None:
    source_id = source.get("id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("External GTFS source id must be a non-empty string.")
    if known_source_ids is not None and source_id in known_source_ids:
        raise ValueError(f"Duplicate external GTFS source id: {source_id}")

    cities_path = source.get("cities")
    if not isinstance(cities_path, str) or not cities_path.strip():
        raise ValueError(f"External GTFS source {source_id} is missing cities path.")
    resolved_cities = (repository_root / cities_path).resolve()
    if not resolved_cities.is_file():
        raise ValueError(
            f"External GTFS source {source_id} city file does not exist: {cities_path}"
        )

    classification = source_classification(source)
    for url_key in ("url", "scopedURL"):
        configured_url = source.get(url_key)
        if configured_url is not None and (
            not isinstance(configured_url, str) or not configured_url.strip()
        ):
            raise ValueError(
                f"External GTFS source {source_id} has an invalid {url_key}."
            )
    local_path = source.get("localPath")
    if local_path is not None and (
        not isinstance(local_path, str) or not local_path.strip()
    ):
        raise ValueError(f"External GTFS source {source_id} has an invalid localPath.")
    preflight = source.get("preflight", "head")
    if preflight not in {"head", "download"}:
        raise ValueError(
            f"External GTFS source {source_id} has invalid preflight {preflight!r}."
        )

    allow_stale = source.get("allowStale", True)
    if not isinstance(allow_stale, bool):
        raise ValueError(f"External GTFS source {source_id} has invalid allowStale.")
    external_gtfs_resilience_policy(source)

    agency_id = source.get("agencyID")
    if agency_id is not None and (
        not isinstance(agency_id, (str, int)) or not str(agency_id).strip()
    ):
        raise ValueError(f"External GTFS source {source_id} has an invalid agencyID.")

    timezone_name = source.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError(f"External GTFS source {source_id} is missing timezone.")
    try:
        ZoneInfo(timezone_name)
    except Exception as error:
        raise ValueError(
            f"External GTFS source {source_id} has invalid timezone {timezone_name!r}."
        ) from error

    preserve_native_ids = source.get("preserveNativeIDs", False)
    if not isinstance(preserve_native_ids, bool):
        raise ValueError(
            f"External GTFS source {source_id} has invalid preserveNativeIDs."
        )
    prefix = source.get("identifierPrefix")
    if not isinstance(prefix, str) or (not prefix.strip() and not preserve_native_ids):
        raise ValueError(
            f"External GTFS source {source_id} requires a non-empty identifierPrefix."
        )
    if prefix.strip() and known_prefixes is not None and prefix in known_prefixes and not str(source.get("mergeGroup", "")).strip():
        raise ValueError(f"Duplicate external GTFS identifierPrefix: {prefix}")

    namespace = source.get("namespace", "")
    if not isinstance(namespace, str):
        raise ValueError(f"External GTFS source {source_id} has an invalid namespace.")
    if namespace and (
        namespace.strip() != namespace
        or any(character.isspace() for character in namespace)
        or not namespace.endswith(":")
    ):
        raise ValueError(
            f"External GTFS source {source_id} namespace must be a whitespace-free "
            "prefix ending with ':'."
        )
    if namespace and known_namespaces is not None and namespace in known_namespaces and not str(source.get("mergeGroup", "")).strip():
        raise ValueError(f"Duplicate external GTFS namespace: {namespace}")

    merge_group = source.get("mergeGroup")
    if merge_group is not None and (
        not isinstance(merge_group, str) or not merge_group.strip()
    ):
        raise ValueError(f"External GTFS source {source_id} has an invalid mergeGroup.")

    country = source.get("country")
    if not isinstance(country, str) or not country.strip():
        raise ValueError(f"External GTFS source {source_id} is missing country.")

    filter_by_provider = source.get("filterCitiesByProvider", False)
    if not isinstance(filter_by_provider, bool):
        raise ValueError(
            f"External GTFS source {source_id} has invalid filterCitiesByProvider."
        )

    exclusive_city_partition = source.get("exclusiveCityPartition", False)
    if not isinstance(exclusive_city_partition, bool):
        raise ValueError(
            f"External GTFS source {source_id} has invalid exclusiveCityPartition."
        )

    stop_id_mode = source.get("stopIDMode", "exact")
    if stop_id_mode != "exact":
        raise ValueError(
            f"External GTFS source {source_id} stopIDMode {stop_id_mode!r} "
            "is not supported (only 'exact')."
        )

    for flag in ("buildStops", "buildRoutes", "buildDepartures", "buildRadarTopology"):
        value = source.get(flag, flag != "buildRadarTopology")
        if not isinstance(value, bool):
            raise ValueError(f"External GTFS source {source_id} has invalid {flag}.")

    import_static = source.get("importIntoStaticDepartures", False)
    if not isinstance(import_static, bool):
        raise ValueError(
            f"External GTFS source {source_id} has invalid importIntoStaticDepartures."
        )

    publish_passenger_stop_ids = source.get("publishPassengerStopIDs", False)
    if not isinstance(publish_passenger_stop_ids, bool):
        raise ValueError(
            f"External GTFS source {source_id} has invalid "
            "publishPassengerStopIDs."
        )

    supplemental_catalog = source.get("supplementalStopCatalog")
    if supplemental_catalog is not None:
        if not isinstance(supplemental_catalog, dict):
            raise ValueError(
                f"External GTFS source {source_id} has an invalid "
                "supplementalStopCatalog."
            )
        download_url = supplemental_catalog.get("downloadURL")
        resource_id = supplemental_catalog.get("resourceID")
        if not isinstance(download_url, str) or not download_url.strip():
            raise ValueError(
                f"External GTFS source {source_id} supplemental stop catalog "
                "is missing downloadURL."
            )
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError(
                f"External GTFS source {source_id} supplemental stop catalog "
                "is missing resourceID."
            )
        parsed_catalog_url = urlsplit(download_url)
        if (
            parsed_catalog_url.scheme != "https"
            or parsed_catalog_url.hostname != "data.kyivcity.gov.ua"
            or not (
                "/data/download" in parsed_catalog_url.path
                or "/download/" in parsed_catalog_url.path
            )
        ):
            raise ValueError(
                f"External GTFS source {source_id} supplemental stop catalog "
                "must use the official Kyiv CKAN download URL."
            )
        for key in ("idField", "nameField", "latitudeField", "longitudeField"):
            value = supplemental_catalog.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"External GTFS source {source_id} supplemental stop catalog "
                    f"is missing {key}."
                )
        id_prefix = supplemental_catalog.get("idPrefix", "")
        if not isinstance(id_prefix, str) or not id_prefix.strip():
            raise ValueError(
                f"External GTFS source {source_id} supplemental stop catalog "
                "requires a non-empty idPrefix."
            )
        static_provider = supplemental_catalog.get("staticDepartureProviderID")
        if not isinstance(static_provider, str) or not static_provider.strip():
            raise ValueError(
                f"External GTFS source {source_id} supplemental stop catalog "
                "requires staticDepartureProviderID."
            )
        if static_provider != source_id:
            raise ValueError(
                f"External GTFS source {source_id} supplemental stop catalog "
                "must use its own provider ID."
            )


def load_external_cities(
    source: dict[str, object],
    repository_root: Path,
) -> list[dict[str, object]]:
    cities_rel = str(source["cities"])
    cities = load_cities(repository_root / cities_rel)
    source_id = str(source["id"])
    filter_by_provider = bool(source.get("filterCitiesByProvider", False))
    if filter_by_provider:
        cities = [
            city
            for city in cities
            if source_id in {
                str(provider).strip()
                for provider in (
                    city.get("externalGTFSProviders")
                    or (
                        [city.get("externalGTFSProvider")]
                        if city.get("externalGTFSProvider")
                        else []
                    )
                )
                if str(provider).strip()
            }
        ]
    if not cities:
        raise ValueError(f"External GTFS source {source_id} has no cities.")
    for city in cities:
        city_id = str(city["id"])
        package_mode = city.get("packageMode", "german")
        if package_mode != "external":
            raise ValueError(
                f"External city {city_id} must set packageMode to 'external'."
            )
        provider = city.get("externalGTFSProvider")
        providers = city.get("externalGTFSProviders")
        if providers is not None and (
            not isinstance(providers, list)
            or not providers
            or not all(isinstance(item, str) and item.strip() for item in providers)
            or source_id not in providers
        ):
            raise ValueError(
                f"External city {city_id} externalGTFSProviders does not include "
                f"source {source_id!r}."
            )
        if providers is None and provider is not None and provider != source_id:
            raise ValueError(
                f"External city {city_id} externalGTFSProvider {provider!r} "
                f"does not match source {source_id!r}."
            )
        if source_id in {"wmata-bus", "wmata-rail"}:
            city["country"] = "US"
            city["timezone"] = str(source.get("timezone", "America/New_York"))
        city["externalGTFSProvider"] = source_id
    return cities


def external_city_ids(
    sources: list[dict[str, object]],
    repository_root: Path,
) -> set[str]:
    ids: set[str] = set()
    for source in sources:
        for city in load_external_cities(source, repository_root):
            ids.add(str(city["id"]))
    return ids


def parse_external_gtfs_url_args(values: list[str] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(
                f"Invalid --external-gtfs-url value {value!r}; expected providerID=URL."
            )
        provider_id, url = value.split("=", 1)
        provider_id = provider_id.strip()
        url = url.strip()
        if not provider_id or not url:
            raise ValueError(
                f"Invalid --external-gtfs-url value {value!r}; expected providerID=URL."
            )
        if provider_id in mapping:
            raise ValueError(f"Duplicate --external-gtfs-url provider id: {provider_id}")
        mapping[provider_id] = url
    return mapping


def authenticated_external_request(
    source_id: str,
    url: str,
    environ: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Return (url, headers) for an external source without logging secrets."""
    env = environ if environ is not None else os.environ
    auth = EXTERNAL_SOURCE_AUTH.get(source_id, {})
    if not auth and source_id.startswith(FINLAND_SOURCE_PREFIX):
        auth = {
            "api_key_env": "DIGITRANSIT_KEY",
            "header_name": "digitransit-subscription-key",
            "headers": {"Accept-Encoding": "gzip"},
        }
    headers = {
        str(key): str(value)
        for key, value in dict(auth.get("headers") or {}).items()
    }
    if "User-Agent" not in headers:
        headers["User-Agent"] = "HalteWeckerStopPipeline/1.0"

    parts = urlsplit(url)
    is_remote = parts.scheme in {"http", "https"}
    api_key_env = auth.get("api_key_env")
    if is_remote and isinstance(api_key_env, str) and api_key_env:
        api_key = env.get(api_key_env, "").strip()
        if not api_key:
            raise ValueError(
                f"Missing required environment variable {api_key_env} "
                f"for external GTFS source {source_id}."
            )
        header_name = str(auth.get("header_name") or "").strip()
        if header_name:
            header_prefix = str(auth.get("header_prefix") or "")
            headers[header_name] = f"{header_prefix}{api_key}"
        else:
            query_parameter = str(auth.get("query_parameter") or "key")
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query[query_parameter] = api_key
            url = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
            )
    basic_auth_env = auth.get("basic_auth_env")
    if is_remote and isinstance(basic_auth_env, (tuple, list)) and len(basic_auth_env) == 2:
        client_id_env, client_secret_env = (str(value) for value in basic_auth_env)
        client_id = env.get(client_id_env, "").strip()
        client_secret = env.get(client_secret_env, "").strip()
        if not client_id or not client_secret:
            missing = client_id_env if not client_id else client_secret_env
            raise ValueError(
                f"Missing required environment variable {missing} "
                f"for external GTFS source {source_id}."
            )
        credentials = f"{client_id}:{client_secret}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
    return url, headers


def _parse_location_type(value: object) -> int:
    """GTFS location_type with safe fallback: empty/unknown values mean 0."""
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        parsed = int(raw)
    except ValueError:
        return 0
    if parsed not in (0, 1, 2, 3, 4):
        return 0
    return parsed


def _feed_stop_rows(
    archive: zipfile.ZipFile,
) -> dict[str, dict[str, str]]:
    return {
        str(row["stop_id"]): row
        for row in iter_table(archive, "stops.txt")
        if row.get("stop_id")
    }


def _published_id(raw_id: str, namespace: str) -> str:
    return f"{namespace}{raw_id}" if namespace else raw_id


def _raw_id(published_id: str, namespace: str) -> str:
    if namespace:
        return published_id[len(namespace):] if published_id.startswith(namespace) else ""
    return published_id


def _stop_resolution_map(
    feed_stops: dict[str, dict[str, str]],
    public_stop_ids: set[str],
) -> dict[str, str | None]:
    """Map every feed stop ID to the public package stop that represents it.

    A public stop represents itself. A child platform rolls up to its nearest
    ancestor (via parent_station) that is present in the public package. Stops
    that do not resolve to any public package stop map to None.
    """
    parent_of = {
        stop_id: str(row.get("parent_station", "") or "").strip()
        for stop_id, row in feed_stops.items()
    }
    memo: dict[str, str | None] = {}

    def resolve(stop_id: str) -> str | None:
        if stop_id in memo:
            return memo[stop_id]
        seen: set[str] = set()
        chain: list[str] = []
        current = stop_id
        result: str | None = None
        while True:
            if current in public_stop_ids:
                result = current
                break
            if current in seen:
                break
            seen.add(current)
            chain.append(current)
            parent = parent_of.get(current, "")
            if not parent:
                break
            current = parent
        for node in chain:
            memo[node] = result
        return result

    return resolve


DUPLICATE_STOP_DISTANCE_METERS = 150.0


def _load_served_platform_ids(archive: zipfile.ZipFile) -> set[str]:
    """Stop ids that receive at least one stop_time in the timetable."""
    served: set[str] = set()
    for row in iter_table(archive, "stop_times.txt"):
        stop_id = str(row.get("stop_id", "") or "").strip()
        if stop_id:
            served.add(stop_id)
    return served


def _consolidate_duplicate_stops(
    public_stops: list[dict[str, object]],
    served_platform_ids: set[str],
    children_of: dict[str, list[str]],
) -> list[dict[str, object]]:
    """Drop unserved stops that share a name and near-identical coordinates
    with a served twin, keeping only the served stop."""
    if not public_stops:
        return public_stops

    served_by_id: dict[str, bool] = {}
    for stop in public_stops:
        stop_id = str(stop["id"])
        served_by_id[stop_id] = stop_id in served_platform_ids or any(
            child_id in served_platform_ids
            for child_id in children_of.get(stop_id, ())
        )

    bins: dict[tuple[object, float, float], list[dict[str, object]]] = {}
    for stop in public_stops:
        key = (
            stop["searchName"],
            round(float(stop["latitude"]), 2),
            round(float(stop["longitude"]), 2),
        )
        bins.setdefault(key, []).append(stop)

    removed_ids: set[str] = set()
    for group in bins.values():
        served = [stop for stop in group if served_by_id[str(stop["id"])]]
        unserved = [stop for stop in group if not served_by_id[str(stop["id"])]]
        if not served or not unserved:
            continue
        for dead in unserved:
            if any(
                distance_meters(
                    float(dead["latitude"]),
                    float(dead["longitude"]),
                    float(alive["latitude"]),
                    float(alive["longitude"]),
                ) <= DUPLICATE_STOP_DISTANCE_METERS
                for alive in served
            ):
                removed_ids.add(str(dead["id"]))

    if not removed_ids:
        return public_stops
    return [stop for stop in public_stops if str(stop["id"]) not in removed_ids]


def _duplicate_bin_count(public_stops: list[dict[str, object]]) -> int:
    """Largest number of public stops sharing a name and coarse location."""
    bins: dict[tuple[object, float, float], int] = {}
    for stop in public_stops:
        key = (
            stop["searchName"],
            round(float(stop["latitude"]), 2),
            round(float(stop["longitude"]), 2),
        )
        bins[key] = bins.get(key, 0) + 1
    return max(bins.values(), default=0)


def _deduplicate_merged_stops(
    records: list[tuple[str, dict[str, object]]],
    city_id: str,
) -> list[dict[str, object]]:
    """Deduplicate equivalent native IDs and reject conflicting collisions."""
    grouped: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for source_id, stop in records:
        grouped.setdefault(str(stop["id"]), []).append((source_id, stop))

    result: list[dict[str, object]] = []
    for stop_id, candidates in grouped.items():
        signatures = {
            json.dumps(
                {
                    key: candidate.get(key)
                    for key in ("id", "name", "latitude", "longitude", "searchName", "stopCode")
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for _, candidate in candidates
        }
        if len(signatures) > 1:
            details = "; ".join(
                f"provider={source_id} name={candidate.get('name')!r} "
                f"lat={candidate.get('latitude')} lon={candidate.get('longitude')}"
                for source_id, candidate in candidates
            )
            raise ValueError(
                f"Conflicting external stop ID collision for {city_id}/{stop_id}: {details}"
            )
        result.append(candidates[0][1])
    return result


def _supplemental_stop_records(payload: object) -> list[dict[str, object]]:
    if (
        isinstance(payload, list)
        and payload
        and all(isinstance(item, dict) for item in payload)
    ):
        return [dict(item) for item in payload]
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        records = payload["records"]
        if records and all(isinstance(item, dict) for item in records):
            return [dict(item) for item in records]
    raise ValueError(
        "Supplemental stop catalog must contain a non-empty array of records."
    )


def validate_kyiv_supplemental_catalog(
    payload: object,
    configuration: dict[str, object],
) -> None:
    records = _supplemental_stop_records(payload)
    fields = {
        key: str(configuration[key])
        for key in ("idField", "nameField", "latitudeField", "longitudeField")
    }
    allowed_types = {
        int(value)
        for value in configuration.get("allowedTypes", [0])
        if isinstance(value, (int, str)) and str(value).strip().lstrip("-").isdigit()
    }
    valid_records = 0
    for record in records:
        if not str(record.get(fields["idField"], "") or "").strip():
            continue
        if not str(record.get(fields["nameField"], "") or "").strip():
            continue
        try:
            latitude = float(record[fields["latitudeField"]])
            longitude = float(record[fields["longitudeField"]])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            continue
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            continue
        type_field = str(configuration.get("typeField", "type"))
        if allowed_types:
            raw_type = record.get(type_field, 0)
            try:
                if int(raw_type) not in allowed_types:
                    continue
            except (TypeError, ValueError):
                continue
        valid_records += 1
    if valid_records == 0:
        raise ValueError("Kyiv supplemental stop catalog has no valid stop records")


def load_kyiv_supplemental_catalog(
    configuration: dict[str, object],
    *,
    cache: KyivResourceCache | None,
) -> object:
    try:
        payload = load_json_resource(configuration)
        validate_kyiv_supplemental_catalog(payload, configuration)
        if cache is not None:
            cache.store(configuration, payload)
        return payload
    except Exception as error:
        if cache is None:
            raise
        cached = cache.load(configuration)
        if cached is None:
            raise
        payload, modified_at = cached
        validate_kyiv_supplemental_catalog(payload, configuration)
        print(
            "[Kyiv] source=supplemental-stop-catalog "
            f"using cached resource age={_cache_age_text(modified_at)}"
        )
        return payload


def _build_supplemental_stop_entries(
    records: list[dict[str, object]],
    city: dict[str, object],
    existing_stops: list[dict[str, object]],
    configuration: dict[str, object],
) -> list[dict[str, object]]:
    """Merge official stop locations that are absent from the GTFS timetable."""
    id_field = str(configuration["idField"])
    name_field = str(configuration["nameField"])
    latitude_field = str(configuration["latitudeField"])
    longitude_field = str(configuration["longitudeField"])
    id_prefix = str(configuration["idPrefix"])
    static_provider_id = str(configuration["staticDepartureProviderID"])
    deduplication_distance = float(
        configuration.get("deduplicationDistanceMeters", DUPLICATE_STOP_DISTANCE_METERS)
    )
    allowed_types = {
        int(value)
        for value in configuration.get("allowedTypes", [0])
        if isinstance(value, (int, str)) and str(value).strip().lstrip("-").isdigit()
    }
    city_latitude = float(city["latitude"])
    city_longitude = float(city["longitude"])
    city_radius = float(city["radiusMeters"])
    seen_ids: set[str] = set()
    entries: list[dict[str, object]] = []

    for record in records:
        raw_id = str(record.get(id_field, "") or "").strip()
        name = str(record.get(name_field, "") or "").strip()
        if not raw_id or not name:
            continue
        raw_type = record.get(str(configuration.get("typeField", "type")), 0)
        try:
            record_type = int(raw_type or 0)
        except (TypeError, ValueError):
            continue
        if allowed_types and record_type not in allowed_types:
            continue
        try:
            latitude = float(record[latitude_field])
            longitude = float(record[longitude_field])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
            or distance_meters(latitude, longitude, city_latitude, city_longitude)
            > city_radius
        ):
            continue
        published_id = f"{id_prefix}{raw_id}"
        if published_id in seen_ids:
            continue
        if any(
            distance_meters(
                latitude,
                longitude,
                float(existing["latitude"]),
                float(existing["longitude"]),
            )
            <= deduplication_distance
            for existing in existing_stops
        ):
            continue
        seen_ids.add(published_id)
        raw_stop_code = record.get(str(configuration.get("stopCodeField", "code")))
        stop_code = str(raw_stop_code).strip() if raw_stop_code else None
        entries.append({
            "id": published_id,
            "name": name,
            "latitude": latitude,
            "longitude": longitude,
            "searchName": normalized(name),
            "stopCode": stop_code,
            "staticDeparturesAvailable": False,
            "staticDepartureProviderID": static_provider_id,
            "dataSource": str(configuration.get("dataSource", "official-stop-catalog")),
        })
    return entries


def build_external_stop_packages(
    archive: zipfile.ZipFile,
    cities: list[dict[str, object]],
    output: Path,
    stop_id_mode: str = "exact",
    namespace: str = "",
    publish_passenger_stop_ids: bool = False,
    supplemental_stop_catalog: object | None = None,
    supplemental_catalog_configuration: dict[str, object] | None = None,
    exclusive_city_partition: bool = False,
) -> tuple[list[dict[str, object]], dict[str, list[dict[str, object]]]]:
    if stop_id_mode != "exact":
        raise ValueError(f"Unsupported stopIDMode: {stop_id_mode!r}")
    if not cities:
        return [], {}

    raw_by_city_id: dict[str, list[dict[str, object]]] = {
        str(city["id"]): [] for city in cities
    }
    for row in iter_table(archive, "stops.txt"):
        stop_id = str(row.get("stop_id", "")).strip()
        name = str(row.get("stop_name", "") or "").strip()
        if not stop_id or not name:
            continue
        try:
            latitude = float(row["stop_lat"])
            longitude = float(row["stop_lon"])
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            continue
        record: dict[str, object] = {
            "id": stop_id,
            "name": name,
            "latitude": latitude,
            "longitude": longitude,
            "searchName": normalized(name),
            "stopCode": str(row.get("stop_code", "") or "").strip(),
            "location_type": _parse_location_type(row.get("location_type")),
            "parent_station": str(row.get("parent_station", "") or "").strip(),
            "platform_code": str(row.get("platform_code", "") or "").strip(),
        }
        if exclusive_city_partition:
            nearest_city = min(
                cities,
                key=lambda city: distance_meters(
                    latitude,
                    longitude,
                    float(city["latitude"]),
                    float(city["longitude"]),
                ),
            )
            if distance_meters(
                latitude,
                longitude,
                float(nearest_city["latitude"]),
                float(nearest_city["longitude"]),
            ) <= float(nearest_city["radiusMeters"]):
                raw_by_city_id[str(nearest_city["id"])].append(record)
        else:
            for city in cities:
                if distance_meters(
                    latitude,
                    longitude,
                    float(city["latitude"]),
                    float(city["longitude"]),
                ) <= float(city["radiusMeters"]):
                    raw_by_city_id[str(city["id"])].append(record)

    packages_directory = output / "stops"
    packages_directory.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    package_stops: dict[str, list[dict[str, object]]] = {}
    city_public: dict[str, list[dict[str, object]]] = {}
    city_children: dict[str, dict[str, list[str]]] = {}
    served_platform_ids: set[str] = _load_served_platform_ids(archive)
    for city in cities:
        city_id = str(city["id"])
        raw_stops = raw_by_city_id[city_id]
        if not raw_stops:
            raise ValueError(f"No stops found for configured external city {city_id}.")
        parent_ids = {
            str(stop["id"])
            for stop in raw_stops
            if stop["location_type"] == 1
        }
        children: dict[str, list[str]] = {}
        for stop in raw_stops:
            parent_station = str(stop["parent_station"])
            if parent_station:
                children.setdefault(parent_station, []).append(str(stop["id"]))
        city_children[city_id] = children
        public_stops: list[dict[str, object]] = []
        for stop in raw_stops:
            location_type = int(stop["location_type"])
            parent_station = str(stop["parent_station"])
            if publish_passenger_stop_ids:
                if location_type == 0:
                    public_stops.append(stop)
                    continue
                if location_type == 1:
                    # Keep stations that are directly referenced or needed to select
                    # served child boarding stops.
                    stop_id_str = str(stop["id"])
                    if stop_id_str in served_platform_ids or any(
                        child_id in served_platform_ids
                        for child_id in children.get(stop_id_str, [])
                    ):
                        public_stops.append(stop)
                continue
            if location_type in (2, 3, 4):
                # entrances, generic nodes, and boarding areas are not public stops
                continue
            if location_type == 1:
                # parent stations are public only when they (or a child) have stop_times
                stop_id_str = str(stop["id"])
                if stop_id_str in served_platform_ids or any(
                    child_id in served_platform_ids
                    for child_id in children.get(stop_id_str, [])
                ):
                    public_stops.append(stop)
                continue
            # location_type == 0: keep only orphans whose parent is unavailable
            if parent_station and parent_station in parent_ids:
                continue
            public_stops.append(stop)
        if not public_stops:
            raise ValueError(
                f"No public stops found for configured external city {city_id}."
            )
        city_public[city_id] = public_stops

    has_potential_duplicates = any(
        _duplicate_bin_count(stops) > 1 for stops in city_public.values()
    )
    if has_potential_duplicates and not publish_passenger_stop_ids:
        for city_id, public_stops in city_public.items():
            city_public[city_id] = _consolidate_duplicate_stops(
                public_stops,
                served_platform_ids,
                city_children[city_id],
            )

    for city in cities:
        city_id = str(city["id"])
        public_entries = [
            {
                "id": _published_id(str(stop["id"]), namespace),
                "name": stop["name"],
                "latitude": stop["latitude"],
                "longitude": stop["longitude"],
                "searchName": stop["searchName"],
                "stopCode": stop["stopCode"] or None,
            }
            for stop in city_public[city_id]
        ]
        if supplemental_stop_catalog is not None:
            catalog_entries = _build_supplemental_stop_entries(
                _supplemental_stop_records(supplemental_stop_catalog),
                city,
                public_entries,
                supplemental_catalog_configuration or {},
            )
            public_entries.extend(catalog_entries)
            if catalog_entries:
                print(
                    f"[StopData] supplemental city={city_id} "
                    f"source={supplemental_catalog_configuration.get('resourceID') if supplemental_catalog_configuration else 'unknown'} "
                    f"stops={len(catalog_entries)}"
                )
        filename = write_stop_package(packages_directory, city_id, public_entries)
        package_stops[city_id] = public_entries
        manifest.append({
            "id": city["id"],
            "name": city["name"],
            "aliases": city.get("aliases", []),
            "stopCount": len(public_entries),
            "url": f"stops/{filename}",
            **({"catalogOnly": True} if city.get("catalogOnly") is True else {}),
        })
    return manifest, package_stops


def validate_external_stop_packages(
    *,
    cities: list[dict[str, object]],
    manifest_entries: list[dict[str, object]],
    package_stops_by_city_id: dict[str, list[dict[str, object]]],
    output: Path,
    center_radius_meters: float = 8_000.0,
) -> dict[str, int]:
    """Validate published external stop packages and report center coverage."""
    cities_by_id = {str(city["id"]): city for city in cities}
    entries_by_id = {str(entry.get("id", "")): entry for entry in manifest_entries}
    center_counts: dict[str, int] = {}

    for city_id, entry in entries_by_id.items():
        city = cities_by_id.get(city_id)
        if city is None:
            raise ValueError(
                f"External manifest city {city_id} has no source configuration."
            )

        stops = package_stops_by_city_id.get(city_id, [])
        if not stops:
            raise ValueError(
                f"External stop package for {city_id} is empty or missing."
            )

        declared_count = entry.get("stopCount")
        if declared_count != len(stops):
            raise ValueError(
                f"External stop count mismatch for {city_id}: "
                f"manifest={declared_count!r}, package={len(stops)}."
            )

        center_latitude = float(city["latitude"])
        center_longitude = float(city["longitude"])
        center_count = 0
        stop_ids: set[str] = set()
        for stop in stops:
            stop_id = str(stop.get("id", "")).strip()
            if not stop_id or stop_id in stop_ids:
                raise ValueError(f"External stop IDs are invalid for {city_id}.")
            stop_ids.add(stop_id)
            try:
                latitude = float(stop["latitude"])
                longitude = float(stop["longitude"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"External stop coordinates are invalid for {city_id}: {stop_id}."
                ) from error
            if (
                not math.isfinite(latitude)
                or not math.isfinite(longitude)
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180
            ):
                raise ValueError(
                    f"External stop coordinates are out of range for {city_id}: {stop_id}."
                )
            if distance_meters(
                latitude,
                longitude,
                center_latitude,
                center_longitude,
            ) <= center_radius_meters:
                center_count += 1

        if center_count == 0:
            raise ValueError(
                f"External stop package for {city_id} has no stop within "
                f"{center_radius_meters:.0f} m of its configured center."
            )

        package_url = entry.get("url")
        if not isinstance(package_url, str) or not package_url.startswith("stops/"):
            raise ValueError(f"External stop package URL is invalid for {city_id}.")
        package_path = output / package_url
        if not package_path.is_file():
            raise ValueError(
                f"External stop package file is missing for {city_id}: {package_path}."
            )

        center_counts[city_id] = center_count
        print(
            f"[StopData] external city={city_id} stops={len(stops)} "
            f"centerStops{int(center_radius_meters / 1000)}km={center_count}"
        )

    return center_counts


def build_external_route_index(
    archive: zipfile.ZipFile,
    cities: list[dict[str, object]],
    output: Path,
    namespace: str = "",
) -> None:
    if not cities:
        return

    routes = {
        str(row["route_id"]): row
        for row in load_table(archive, "routes.txt")
        if row.get("route_id")
    }
    if not routes:
        return

    trip_routes: dict[str, str] = {}
    trip_headsigns: dict[str, tuple[str, str]] = {}
    for trip in iter_table(archive, "trips.txt"):
        route_id = str(trip.get("route_id", ""))
        trip_id = str(trip.get("trip_id", ""))
        if route_id in routes and trip_id:
            trip_routes[trip_id] = route_id
            headsign = str(trip.get("trip_headsign", "") or "")
            direction_id = str(trip.get("direction_id", "0"))
            if headsign:
                trip_headsigns[trip_id] = (direction_id, headsign)

    stop_route_ids: dict[str, set[str]] = {}
    feed_stops = _feed_stop_rows(archive)
    packages_directory = output / "stops"
    routes_directory = output / "routes"
    routes_directory.mkdir(parents=True, exist_ok=True)

    public_stop_ids: set[str] = set()
    city_stop_ids: dict[str, set[str]] = {}
    for city in cities:
        city_id = str(city["id"])
        stop_path = packages_directory / f"{city_id}.json"
        if not stop_path.exists():
            city_stop_ids[city_id] = set()
            continue
        ids = {
            _raw_id(str(stop["id"]), namespace)
            for stop in json.loads(stop_path.read_text(encoding="utf-8"))
        }
        city_stop_ids[city_id] = ids
        public_stop_ids.update(ids)

    resolve = _stop_resolution_map(feed_stops, public_stop_ids)
    for stop_time in iter_table(archive, "stop_times.txt"):
        stop_id = str(stop_time.get("stop_id", ""))
        trip_id = str(stop_time.get("trip_id", ""))
        route_id = trip_routes.get(trip_id)
        if stop_id and route_id:
            public_stop_id = resolve(stop_id)
            if public_stop_id is not None:
                stop_route_ids.setdefault(public_stop_id, set()).add(route_id)

    for city in cities:
        city_id = str(city["id"])
        city_routes: dict[str, dict[str, object]] = {}
        for stop_id in city_stop_ids[city_id]:
            for route_id in stop_route_ids.get(stop_id, set()):
                published_route_id = _published_id(route_id, namespace)
                if published_route_id not in city_routes:
                    route = routes[route_id]
                    city_routes[published_route_id] = {
                        "short_name": str(route.get("route_short_name", "")),
                        "long_name": str(route.get("route_long_name", "")),
                        "type": str(route.get("route_type", "3")),
                        "agency": str(route.get("agency_id", "")),
                        "headsigns": {},
                    }
        for trip_id, (direction_id, headsign) in trip_headsigns.items():
            route_id = trip_routes.get(trip_id)
            published_route_id = _published_id(route_id, namespace) if route_id else ""
            if published_route_id and published_route_id in city_routes:
                headsigns = city_routes[published_route_id].setdefault("headsigns", {})
                if isinstance(headsigns, dict) and direction_id not in headsigns:
                    headsigns[direction_id] = headsign
        ordered = {
            route_id: city_routes[route_id]
            for route_id in sorted(city_routes)
        }
        (routes_directory / f"{city_id}.json").write_text(
            json.dumps(ordered, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )


def build_external_trip_index(
    archive: zipfile.ZipFile,
    cities: list[dict[str, object]],
    output: Path,
    namespace: str = "",
) -> None:
    """Write a compact realtime trip index: tripId -> {"r": routeId, "h": headsign}.

    The Samtrafiken GTFS-RT VehiclePositions feed rarely carries routeId
    (only ~7/486 in a live sample). It carries the GTFS trip namespace of the
    operator, which varies between Swedish datasets. This index lets the iOS
    radar resolve a realtime tripId to a static route/line and (where known) a
    headsign without assuming one operator prefix.

    Only trips with a non-empty id and a route present in routes.txt are
    eligible. Each city index is limited to the routes already published for
    that city, keeping the index compact while preserving every operator
    namespace present in the source dataset. Headsigns are enriched from the
    already-written departures index (active-window trips only).
    """
    if not cities:
        return

    routes = {
        str(row["route_id"]): row
        for row in load_table(archive, "routes.txt")
        if row.get("route_id")
    }

    trip_route: dict[str, str] = {}
    for trip in iter_table(archive, "trips.txt"):
        trip_id = str(trip.get("trip_id", "")).strip()
        route_id = str(trip.get("route_id", "")).strip()
        if trip_id and route_id in routes:
            trip_route[trip_id] = route_id

    if not trip_route:
        return

    trips_directory = output / "trips"
    trips_directory.mkdir(parents=True, exist_ok=True)

    for city in cities:
        city_id = str(city["id"])
        city_routes_path = output / "routes" / f"{city_id}.json"
        if city_routes_path.exists():
            try:
                city_routes_payload = json.loads(
                    city_routes_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                city_routes_payload = None
            city_route_ids = (
                {str(route_id) for route_id in city_routes_payload}
                if isinstance(city_routes_payload, dict)
                else set()
            )
        else:
            # Keep the direct builder useful for callers that do not build routes first.
            city_route_ids = set(routes)

        city_trip_route = {
            trip_id: route_id
            for trip_id, route_id in trip_route.items()
            if _published_id(route_id, namespace) in city_route_ids
        }
        headsigns: dict[str, str] = {}
        departures_path = output / "departures" / f"{city_id}.json"
        if departures_path.exists():
            try:
                departures = json.loads(departures_path.read_text(encoding="utf-8"))
                for deps in departures.get("stops", {}).values():
                    for dep in deps:
                        trip_id = dep.get("t", "")
                        headsign = dep.get("h", "")
                        if trip_id in city_trip_route and headsign:
                            headsigns.setdefault(trip_id, headsign)
            except (OSError, ValueError):
                pass

        index: dict[str, dict[str, str]] = {}
        for trip_id, route_id in city_trip_route.items():
            entry: dict[str, str] = {"r": _published_id(route_id, namespace)}
            if headsign := headsigns.get(trip_id):
                entry["h"] = headsign
            index[_published_id(trip_id, namespace)] = entry

        (trips_directory / f"{city_id}.json").write_text(
            json.dumps(index, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        print(f"[ExternalGTFS] trip index {city_id}: {len(index)} trips")


def _service_calendar(archive: zipfile.ZipFile) -> dict[str, dict[str, object]]:
    calendar: dict[str, dict[str, object]] = {}
    names = set(archive.namelist())
    if "calendar.txt" in names:
        for row in iter_table(archive, "calendar.txt"):
            service_id = row.get("service_id", "").strip()
            if not service_id:
                continue
            calendar[service_id] = {
                "startDate": row.get("start_date", "00000000"),
                "endDate": row.get("end_date", "99999999"),
                "weekdays": [
                    int(row.get(day, "0") or "0")
                    for day in (
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                        "sunday",
                    )
                ],
                "exceptions": {},
            }
    if "calendar_dates.txt" in names:
        for row in iter_table(archive, "calendar_dates.txt"):
            service_id = row.get("service_id", "").strip()
            service_date = row.get("date", "").strip()
            if not service_id or not service_date:
                continue
            entry = calendar.setdefault(
                service_id,
                {
                    "startDate": "00000000",
                    "endDate": "99999999",
                    "weekdays": [0] * 7,
                    "exceptions": {},
                },
            )
            try:
                exception_type = int(row.get("exception_type", "0") or "0")
            except ValueError:
                continue
            exceptions = entry["exceptions"]
            if isinstance(exceptions, dict):
                exceptions[service_date] = exception_type
    return calendar


def _service_active(service: dict[str, object] | None, service_date: str) -> bool:
    if not service:
        return False
    exceptions = service.get("exceptions")
    if isinstance(exceptions, dict) and service_date in exceptions:
        return exceptions[service_date] == 1
    start_date = str(service.get("startDate", "00000000"))
    end_date = str(service.get("endDate", "99999999"))
    if not start_date <= service_date <= end_date:
        return False
    weekdays = service.get("weekdays")
    if not isinstance(weekdays, list) or len(weekdays) != 7:
        return False
    weekday = datetime.strptime(service_date, "%Y%m%d").weekday()
    return bool(weekdays[weekday])


def _line_label(route: dict[str, str], route_id: str) -> str:
    return (
        route.get("route_short_name", "").strip()
        or route.get("route_long_name", "").strip()
        or route_id
    )


def _compact_departure_identity(item: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Deterministic identity covering every compact field of a departure row."""
    return tuple(sorted(item.items()))


def _dedupe_exact_departures(
    items: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Drop exact duplicate compact departure rows, keeping the first occurrence."""
    deduped: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for item in items:
        identity = _compact_departure_identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(item)
    return deduped


def build_external_departure_index(
    archive: zipfile.ZipFile,
    cities: list[dict[str, object]],
    output: Path,
    timezone_name: str,
    namespace: str = "",
    departure_window_days: int = 3,
) -> None:
    if not cities:
        return

    zone = ZoneInfo(timezone_name)
    today = datetime.now(zone).date()
    if departure_window_days < 1:
        raise ValueError("departure_window_days must be positive")
    offsets = (-1, 0, 1) if departure_window_days <= 3 else range(departure_window_days)
    service_dates = [
        (today + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in offsets
    ]
    calendar = _service_calendar(archive)
    active_by_service: dict[str, list[str]] = {}
    for service_id, service in calendar.items():
        active_dates = [
            service_date
            for service_date in service_dates
            if _service_active(service, service_date)
        ]
        if active_dates:
            active_by_service[service_id] = active_dates

    routes = {
        str(row["route_id"]): row
        for row in load_table(archive, "routes.txt")
        if row.get("route_id")
    }
    stop_names = {
        str(row["stop_id"]): row.get("stop_name", "").strip()
        for row in load_table(archive, "stops.txt")
        if row.get("stop_id")
    }

    trip_meta: dict[str, dict[str, str]] = {}
    for trip in iter_table(archive, "trips.txt"):
        trip_id = str(trip.get("trip_id", "")).strip()
        service_id = str(trip.get("service_id", "")).strip()
        if not trip_id or service_id not in active_by_service:
            continue
        route_id = str(trip.get("route_id", "")).strip()
        trip_meta[trip_id] = {
            "route_id": route_id,
            "headsign": str(trip.get("trip_headsign", "") or "").strip(),
            "direction_id": str(trip.get("direction_id", "0") or "0"),
            "service_id": service_id,
        }

    packages_directory = output / "stops"
    city_stop_ids: dict[str, set[str]] = {}
    public_stop_ids: set[str] = set()
    for city in cities:
        city_id = str(city["id"])
        stop_path = packages_directory / f"{city_id}.json"
        if not stop_path.exists():
            city_stop_ids[city_id] = set()
            continue
        stops = json.loads(stop_path.read_text(encoding="utf-8"))
        ids = {str(stop["id"]) for stop in stops}
        city_stop_ids[city_id] = ids
        public_stop_ids.update(
            raw_id for raw_id in (_raw_id(stop_id, namespace) for stop_id in ids)
            if raw_id
        )

    feed_stops = _feed_stop_rows(archive)
    resolve = _stop_resolution_map(feed_stops, public_stop_ids)

    # Stream stop_times once: collect departures for package stops and terminal stops.
    stop_departures: dict[str, list[tuple[str, str, str, str, int]]] = {}
    terminal_by_trip: dict[str, tuple[int, str]] = {}
    for stop_time in iter_table(archive, "stop_times.txt"):
        trip_id = str(stop_time.get("trip_id", "")).strip()
        if trip_id not in trip_meta:
            continue
        stop_id = str(stop_time.get("stop_id", "")).strip()
        departure_time = stop_time.get("departure_time", "").strip()
        try:
            sequence = int(stop_time.get("stop_sequence", "0") or "0")
        except ValueError:
            sequence = 0
        if stop_id and departure_time:
            public_stop_id = resolve(stop_id)
            if public_stop_id is not None:
                platform_code = ""
                if public_stop_id != stop_id:
                    platform_code = str(
                        feed_stops.get(stop_id, {}).get("platform_code", "") or ""
                    ).strip()
                stop_departures.setdefault(public_stop_id, []).append(
                    (departure_time, trip_id, stop_id, platform_code, sequence)
                )
        if stop_id:
            previous = terminal_by_trip.get(trip_id)
            if previous is None or sequence >= previous[0]:
                terminal_by_trip[trip_id] = (sequence, stop_id)

    # Realtime mapping: parent public stop -> child platform stop IDs.
    platforms_by_parent: dict[str, set[str]] = {}
    for stop_row in feed_stops.values():
        if _parse_location_type(stop_row.get("location_type")) != 0:
            continue
        child_id = str(stop_row["stop_id"])
        parent_id = str(stop_row.get("parent_station", "") or "").strip()
        if not parent_id:
            continue
        target = resolve(child_id)
        if target is not None and target != child_id:
            platforms_by_parent.setdefault(target, set()).add(child_id)

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    departures_directory = output / "departures"
    departures_directory.mkdir(parents=True, exist_ok=True)

    for city in cities:
        city_id = str(city["id"])
        departures_by_stop: dict[str, list[dict[str, str]]] = {}
        for published_stop_id in sorted(city_stop_ids.get(city_id, set())):
            stop_id = _raw_id(published_stop_id, namespace)
            if not stop_id:
                continue
            items: list[dict[str, str]] = []
            for departure_time, trip_id, orig_stop_id, platform_code, sequence in sorted(
                stop_departures.get(stop_id, []),
                key=lambda value: (value[0], value[1], value[4]),
            ):
                meta = trip_meta[trip_id]
                route_id = meta["route_id"]
                route = routes.get(route_id, {})
                destination = meta["headsign"]
                if not destination:
                    terminal_stop_id = terminal_by_trip.get(trip_id, (0, ""))[1]
                    destination = stop_names.get(terminal_stop_id, "") or _line_label(
                        route, route_id
                    )
                direction_id = meta["direction_id"]
                item: dict[str, str] = {
                    "t": _published_id(trip_id, namespace),
                    "r": _published_id(route_id, namespace),
                    "h": destination,
                    "d": direction_id,
                    "p": departure_time,
                    "q": str(sequence),
                }
                if orig_stop_id != stop_id:
                    item["s"] = _published_id(orig_stop_id, namespace)
                    if platform_code:
                        item["platform"] = platform_code
                items.append(item)
            if items:
                items.sort(key=lambda item: (item["p"], item["t"], item["r"]))
                departures_by_stop[published_stop_id] = _dedupe_exact_departures(items)

        platforms = {
            _published_id(parent, namespace): sorted(
                _published_id(child_id, namespace) for child_id in child_ids
            )
            for parent, child_ids in platforms_by_parent.items()
            if _published_id(parent, namespace) in city_stop_ids.get(city_id, set())
        }
        payload = {
            "generatedAt": generated_at,
            "timezone": timezone_name,
            "stops": {
                stop_id: departures_by_stop[stop_id]
                for stop_id in sorted(departures_by_stop)
            },
            "platforms": platforms,
        }
        (departures_directory / f"{city_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )


def build_external_departure_index_bounded(
    archive: zipfile.ZipFile,
    cities: list[dict[str, object]],
    output: Path,
    timezone_name: str,
    namespace: str = "",
    departure_window_days: int = 3,
) -> None:
    """Build departure JSON through a disk-backed SQLite staging database."""
    if not cities:
        return
    zone = ZoneInfo(timezone_name)
    today = datetime.now(zone).date()
    if departure_window_days < 1:
        raise ValueError("departure_window_days must be positive")
    offsets = (-1, 0, 1) if departure_window_days <= 3 else range(departure_window_days)
    service_dates = [
        (today + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in offsets
    ]
    active_by_service: dict[str, list[str]] = {}
    for service_id, service in _service_calendar(archive).items():
        active_dates = [
            service_date
            for service_date in service_dates
            if _service_active(service, service_date)
        ]
        if active_dates:
            active_by_service[service_id] = active_dates

    packages_directory = output / "stops"
    city_stop_ids: dict[str, set[str]] = {}
    public_stop_ids: set[str] = set()
    for city in cities:
        city_id = str(city["id"])
        stop_path = packages_directory / f"{city_id}.json"
        ids = {
            str(item["id"])
            for item in iter_json_array(stop_path)
            if isinstance(item, dict) and item.get("id")
        }
        city_stop_ids[city_id] = ids
        public_stop_ids.update(
            raw_id for raw_id in (_raw_id(stop_id, namespace) for stop_id in ids) if raw_id
        )

    stage = ExternalDepartureStage()
    try:
        stage.populate(archive, active_by_service, public_stop_ids)
        stage.write_outputs(output, cities, city_stop_ids, namespace, timezone_name)
    finally:
        stage.close()


build_external_departure_index = build_external_departure_index_bounded


def build_external_lines(
    archive: zipfile.ZipFile,
    package_stops_by_city_id: dict[str, list[dict[str, object]]],
    namespace: str = "",
) -> dict[str, dict[str, dict[str, object]]]:
    included_stop_ids = {
        raw_id
        for stops in package_stops_by_city_id.values()
        for stop in stops
        for raw_id in [_raw_id(str(stop["id"]), namespace)]
        if raw_id
    }
    if not included_stop_ids:
        return {}
    stop_rows = list(iter_table(archive, "stops.txt"))
    feed_stops = _feed_stop_rows(archive)
    resolve = _stop_resolution_map(feed_stops, included_stop_ids)

    def resolved_stop_times():
        for stop_time in iter_table(archive, "stop_times.txt"):
            public_stop_id = resolve(str(stop_time.get("stop_id", "")))
            if public_stop_id is None:
                continue
            yield {**stop_time, "stop_id": public_stop_id}

    lines = build_lines_by_stop_id_noncanonical(
        stop_rows=stop_rows,
        stop_times=resolved_stop_times(),
        trips=load_table(archive, "trips.txt"),
        routes=load_table(archive, "routes.txt"),
        included_stop_ids=included_stop_ids,
    )
    if not namespace:
        return lines

    return {
        _published_id(stop_id, namespace): {
            _published_id(route_id, namespace): {
                **line,
                "routeID": _published_id(str(line["routeID"]), namespace),
            }
            for route_id, line in route_lines.items()
        }
        for stop_id, route_lines in lines.items()
    }


def build_external_lines_bounded(
    archive: zipfile.ZipFile,
    package_stops_by_city_id: dict[str, list[dict[str, object]]],
    namespace: str = "",
) -> dict[str, dict[str, dict[str, object]]]:
    """Build line membership through SQLite instead of trip-sized dictionaries."""
    included_stop_ids = {
        raw_id
        for stops in package_stops_by_city_id.values()
        for stop in stops
        for raw_id in [_raw_id(str(stop["id"]), namespace)]
        if raw_id
    }
    if not included_stop_ids:
        return {}
    temporary = tempfile.TemporaryDirectory(prefix="haltewecker-external-lines-")
    connection = sqlite3.connect(Path(temporary.name) / "lines.sqlite")
    try:
        connection.executescript(
            """
            CREATE TABLE routes(route_id TEXT PRIMARY KEY, short_name TEXT, long_name TEXT, route_type TEXT, agency_id TEXT) WITHOUT ROWID;
            CREATE TABLE trip_routes(trip_id TEXT PRIMARY KEY, route_id TEXT) WITHOUT ROWID;
            CREATE TABLE feed_stops(stop_id TEXT PRIMARY KEY, parent_station TEXT) WITHOUT ROWID;
            CREATE TABLE resolved(stop_id TEXT PRIMARY KEY, public_stop_id TEXT) WITHOUT ROWID;
            CREATE TABLE stop_routes(stop_id TEXT, route_id TEXT, PRIMARY KEY(stop_id, route_id)) WITHOUT ROWID;
            CREATE TABLE raw_stop_times(trip_id TEXT NOT NULL, stop_id TEXT NOT NULL);
            """
        )
        connection.executescript(
            """
            CREATE INDEX raw_stop_times_trip_stop ON raw_stop_times(trip_id, stop_id);
            CREATE INDEX raw_stop_times_stop_trip ON raw_stop_times(stop_id, trip_id);
            """
        )
        connection.executemany(
            "INSERT INTO routes VALUES (?, ?, ?, ?, ?)",
            (
                (
                    str(row.get("route_id", "")).strip(),
                    str(row.get("route_short_name", "")).strip(),
                    str(row.get("route_long_name", "")).strip(),
                    str(row.get("route_type", "3")).strip(),
                    str(row.get("agency_id", "")).strip(),
                )
                for row in iter_table(archive, "routes.txt")
                if str(row.get("route_id", "")).strip()
            ),
        )
        connection.executemany(
            "INSERT INTO trip_routes VALUES (?, ?)",
            (
                (str(row.get("trip_id", "")).strip(), str(row.get("route_id", "")).strip())
                for row in iter_table(archive, "trips.txt")
                if str(row.get("trip_id", "")).strip() and str(row.get("route_id", "")).strip()
            ),
        )
        connection.executemany(
            "INSERT INTO feed_stops VALUES (?, ?)",
            (
                (str(row.get("stop_id", "")).strip(), str(row.get("parent_station", "") or "").strip())
                for row in iter_table(archive, "stops.txt")
                if str(row.get("stop_id", "")).strip()
            ),
        )
        for (stop_id,) in connection.execute("SELECT stop_id FROM feed_stops"):
            current = stop_id
            seen: set[str] = set()
            resolved = None
            while current and current not in seen:
                if current in included_stop_ids:
                    resolved = current
                    break
                seen.add(current)
                row = connection.execute("SELECT parent_station FROM feed_stops WHERE stop_id=?", (current,)).fetchone()
                current = str(row[0]).strip() if row else ""
            connection.execute("INSERT OR IGNORE INTO resolved VALUES (?, ?)", (stop_id, resolved))
        connection.executemany(
            "INSERT INTO raw_stop_times VALUES (?, ?)",
            (
                (str(row.get("trip_id", "")).strip(), str(row.get("stop_id", "")).strip())
                for row in iter_table(archive, "stop_times.txt")
                if str(row.get("trip_id", "")).strip() and str(row.get("stop_id", "")).strip()
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO stop_routes(stop_id, route_id)
            SELECT DISTINCT resolved.public_stop_id, trip_routes.route_id
            FROM raw_stop_times
            JOIN trip_routes ON trip_routes.trip_id=raw_stop_times.trip_id
            JOIN resolved ON resolved.stop_id=raw_stop_times.stop_id
            WHERE resolved.public_stop_id IS NOT NULL
            """
        )
        connection.commit()
        print(f"[ExternalGTFS] lines stage SQL materialization complete", flush=True)

        result: dict[str, dict[str, dict[str, object]]] = {}
        started = time.perf_counter()
        for city_id, stops in package_stops_by_city_id.items():
            for stop in stops:
                stop_id = str(stop["id"])
                raw_stop_id = _raw_id(stop_id, namespace)
                stop_result: dict[str, dict[str, object]] = {}
                for route_id, short_name, long_name, route_type, agency_id in connection.execute(
                    """
                    SELECT stop_routes.route_id, routes.short_name, routes.long_name,
                           routes.route_type, routes.agency_id
                    FROM stop_routes
                    JOIN routes ON routes.route_id=stop_routes.route_id
                    WHERE stop_routes.stop_id=?
                    ORDER BY stop_routes.route_id
                    """,
                    (raw_stop_id,),
                ):
                    names = [value for value in (short_name, long_name) if value]
                    if not names:
                        continue
                    line: dict[str, object] = {
                        "routeID": _published_id(route_id, namespace),
                        "agencyID": agency_id or None,
                        "names": names,
                    }
                    if str(route_type).isdigit():
                        line["routeType"] = int(route_type)
                    stop_result[_published_id(route_id, namespace)] = line
                if stop_result:
                    result[stop_id] = stop_result
        print(f"[ExternalGTFS] lines stage output assembly duration={time.perf_counter() - started:.2f}s", flush=True)
        return result
    finally:
        connection.close()
        temporary.cleanup()


build_external_lines = build_external_lines_bounded


def build_external_trip_index_bounded(
    archive: zipfile.ZipFile,
    cities: list[dict[str, object]],
    output: Path,
    namespace: str = "",
) -> None:
    """Write trip indexes from SQLite cursors without a trip-sized dictionary."""
    temporary = tempfile.TemporaryDirectory(prefix="haltewecker-external-trips-")
    connection = sqlite3.connect(Path(temporary.name) / "trips.sqlite")
    try:
        connection.executescript(
            "CREATE TABLE trips(trip_id TEXT PRIMARY KEY, route_id TEXT NOT NULL) WITHOUT ROWID;"
        )
        connection.executemany(
            "INSERT OR IGNORE INTO trips VALUES (?, ?)",
            (
                (str(row.get("trip_id", "")).strip(), str(row.get("route_id", "")).strip())
                for row in iter_table(archive, "trips.txt")
                if str(row.get("trip_id", "")).strip() and str(row.get("route_id", "")).strip()
            ),
        )
        connection.commit()
        trips_directory = output / "trips"
        trips_directory.mkdir(parents=True, exist_ok=True)
        for city in cities:
            city_id = str(city["id"])
            route_path = output / "routes" / f"{city_id}.json"
            route_ids = {
                str(route_id)[len(namespace):] if namespace and str(route_id).startswith(namespace) else str(route_id)
                for route_id, _payload in iter_json_object(route_path)
            } if route_path.exists() else set()
            if not route_ids:
                continue
            headsigns: dict[str, str] = {}
            departures_path = output / "departures" / f"{city_id}.json"
            if departures_path.exists():
                for kind, _stop_id, value in iter_departure_payload(departures_path):
                    if kind != "stop" or not isinstance(value, list):
                        continue
                    for item in value:
                        if not isinstance(item, dict):
                            continue
                        trip_id = str(item.get("t", ""))
                        raw_trip_id = trip_id[len(namespace):] if namespace and trip_id.startswith(namespace) else trip_id
                        headsign = str(item.get("h", "") or "")
                        if raw_trip_id and headsign:
                            headsigns.setdefault(raw_trip_id, headsign)
            path = trips_directory / f"{city_id}.json"
            placeholders = ",".join("?" for _ in route_ids)
            with path.open("w", encoding="utf-8") as stream:
                stream.write("{")
                first = True
                for trip_id, route_id in connection.execute(
                    f"SELECT trip_id, route_id FROM trips WHERE route_id IN ({placeholders}) ORDER BY trip_id",
                    tuple(route_ids),
                ):
                    entry: dict[str, str] = {"r": _published_id(route_id, namespace)}
                    if trip_id in headsigns:
                        entry["h"] = headsigns[trip_id]
                    if not first:
                        stream.write(",")
                    stream.write(json.dumps(_published_id(trip_id, namespace), ensure_ascii=False))
                    stream.write(":")
                    stream.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
                    first = False
                stream.write("}")
            print(f"[ExternalGTFS] trip index {city_id}: completed from SQLite staging")
    finally:
        connection.close()
        temporary.cleanup()


build_external_trip_index = build_external_trip_index_bounded


def _timed_external_stage(
    source_id: str,
    stage: str,
    operation: Callable[[], object],
) -> object:
    started = time.monotonic()
    try:
        result = operation()
    except Exception:
        print(
            f"[StopData] source={source_id} stage={stage} status=failed "
            f"duration={time.monotonic() - started:.2f}s",
            flush=True,
        )
        raise
    print(
        f"[StopData] source={source_id} stage={stage} status=completed "
        f"duration={time.monotonic() - started:.2f}s",
        flush=True,
    )
    return result


def _external_cache_is_eligible(
    source: dict[str, object],
    cities: list[dict[str, object]],
) -> tuple[bool, str, str | None]:
    provider_id = str(source.get("id"))
    expected_city_id = CACHEABLE_PROVIDER_CITY_IDS.get(provider_id)
    if expected_city_id is None:
        return False, "provider-not-in-class-a", None
    if len(cities) != 1 or str(cities[0].get("id")) != expected_city_id:
        return False, "class-a-requires-single-configured-city", None
    if str(source.get("namespace", "")):
        return False, "namespaced-source-not-supported", None
    if str(source.get("mergeGroup", "")).strip():
        return False, "merged-source-not-supported", None
    if source.get("supplementalStopCatalog") is not None:
        return False, "supplemental-stop-catalog-not-supported", None
    if source.get("agencyID") is not None:
        return False, "agency-scoped-source-not-supported", None
    if source.get("localPath") is not None:
        return False, "local-staged-source-not-supported", None
    if source.get("filterCitiesByProvider", False):
        return False, "provider-filtered-city-set-not-supported", None
    if source.get("buildStops", True) is not True:
        return False, "stop-builder-disabled", None
    if source.get("buildRoutes", True) is not True:
        return False, "route-builder-disabled", None
    if source.get("buildDepartures", True) is not True:
        return False, "departure-builder-disabled", None
    if source.get("buildRadarTopology", False) is not False:
        return False, "radar-topology-not-supported", None
    if str(source.get("stopIDMode", "exact")) != "exact":
        return False, "stop-id-mode-not-supported", None
    if source.get("exclusiveCityPartition", False) is not False:
        return False, "exclusive-city-partition-not-supported", None
    return True, "eligible", expected_city_id


def _cached_external_manifest_entry(
    city: dict[str, object],
    stop_count: int,
) -> dict[str, object]:
    return {
        "id": city["id"],
        "name": city["name"],
        "aliases": city.get("aliases", []),
        "stopCount": stop_count,
        "url": f"stops/{city['id']}.json",
        **({"catalogOnly": True} if city.get("catalogOnly") is True else {}),
    }


def _merge_namespaced_city_records_bounded(
    city_id: str,
    records: list[dict[str, object]],
    output: Path,
    manifest_entries: list[dict[str, object]],
    package_stops_by_city_id: dict[str, list[dict[str, object]]],
) -> None:
    """Merge namespaced assets via SQLite and stream every output JSON."""
    total_started = time.monotonic()
    stage = ExternalMergeStage()
    try:
        input_started = time.monotonic()
        for record in records:
            source_output = Path(record["output"])
            source_id = str(record["sourceID"])
            stop_path = source_output / "stops" / f"{city_id}.json"
            route_path = source_output / "routes" / f"{city_id}.json"
            departure_path = source_output / "departures" / f"{city_id}.json"
            trip_path = source_output / "trips" / f"{city_id}.json"
            if stop_path.exists():
                stage.add_stops(stop_path, source_id)
            if route_path.exists():
                stage.add_object_file(route_path, "routes", source_id)
            if departure_path.exists():
                stage.add_departures(departure_path)
            if trip_path.exists():
                stage.add_object_file(trip_path, "trips", source_id)
        print(
            f"[ExternalGTFS] city={city_id} stage=merge-input-read "
            f"duration={time.monotonic() - input_started:.2f}s",
            flush=True,
        )

        if len(stage.timezones) > 1:
            raise ValueError(
                f"Merged external city {city_id} has conflicting timezones: "
                f"{sorted(stage.timezones)}"
            )
        staging_started = time.monotonic()
        stage.commit()
        print(
            f"[ExternalGTFS] city={city_id} stage=merge-sqlite-staging "
            f"duration={time.monotonic() - staging_started:.2f}s",
            flush=True,
        )
        departure_count = stage.connection.execute(
            "SELECT count(*) FROM departures"
        ).fetchone()[0]
        if not departure_count:
            raise ValueError(f"Merged external city {city_id} has incomplete assets.")

        output_started = time.monotonic()
        stop_count, _metadata = stage.write_outputs(output, city_id)
        print(
            f"[ExternalGTFS] city={city_id} stage=merge-output-assembly-write "
            f"duration={time.monotonic() - output_started:.2f}s",
            flush=True,
        )
        merged_stops = [
            item for item in iter_json_array(output / "stops" / f"{city_id}.json")
            if isinstance(item, dict)
        ]
        package_stops_by_city_id[city_id] = merged_stops
        source_ids = ", ".join(str(record["sourceID"]) for record in records)
        city = records[0]["city"]
        manifest_entries.append({
            "id": city_id,
            "name": city["name"],
            "aliases": city.get("aliases", []),
            "stopCount": stop_count,
            "url": f"stops/{city_id}.json",
            "country": str(records[0]["source"]["country"]),
            "_source": f"External GTFS namespaces: {source_ids}",
        })
    finally:
        cleanup_started = time.monotonic()
        stage.close()
        print(
            f"[ExternalGTFS] city={city_id} stage=merge-cleanup "
            f"duration={time.monotonic() - cleanup_started:.2f}s",
            flush=True,
        )
        print(
            f"[ExternalGTFS] city={city_id} stage=merge-total "
            f"duration={time.monotonic() - total_started:.2f}s",
            flush=True,
        )


def process_external_gtfs_sources(
    *,
    repository_root: Path,
    sources_path: Path,
    url_by_provider: dict[str, str],
    output: Path,
    load_gtfs_archive,
    environ: dict[str, str] | None = None,
    occupied_city_ids: set[str] | None = None,
    selected_source_ids: set[str] | None = None,
    load_stop_catalog: Callable[[dict[str, object]], object] | None = None,
    gtfs_cache: GTFSArtifactCache | None = None,
    kyiv_resource_cache: KyivResourceCache | None = None,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, list[dict[str, object]]],
    dict[str, dict[str, dict[str, object]]],
]:
    """
    Validate and build all configured external GTFS sources.

    Returns:
      manifest_entries, external_cities, package_stops_by_city_id, lines_by_stop_id
    """
    sources = load_external_gtfs_sources(sources_path)
    if not sources:
        return [], [], {}, {}

    known_source_ids: set[str] = set()
    known_prefixes: set[str] = set()
    known_namespaces: set[str] = set()
    occupied = set(occupied_city_ids or ())
    manifest_entries: list[dict[str, object]] = []
    external_cities_by_id: dict[str, dict[str, object]] = {}
    package_stops_by_city_id: dict[str, list[dict[str, object]]] = {}
    lines_by_stop_id: dict[str, dict[str, dict[str, object]]] = {}
    external_city_sources: dict[str, dict[str, object]] = {}
    namespaced_records: dict[str, list[dict[str, object]]] = {}
    input_provenance: dict[str, dict[str, object]] = {}
    skipped_sources: dict[str, dict[str, object]] = {}
    namespace_root = output / ".external-namespaces"

    for source in sources:
        source_id = str(source["id"])
        if selected_source_ids is not None and source_id not in selected_source_ids:
            continue
        validate_external_gtfs_source(
            source,
            repository_root,
            known_source_ids=known_source_ids,
            known_prefixes=known_prefixes,
            known_namespaces=known_namespaces,
        )
        known_source_ids.add(source_id)
        identifier_prefix = str(source.get("identifierPrefix", ""))
        if identifier_prefix:
            known_prefixes.add(identifier_prefix)
        namespace = str(source.get("namespace", ""))
        if namespace:
            known_namespaces.add(namespace)

        configured_url = configured_external_url(source)
        if not configured_url:
            configured_url = str(source.get("localPath") or "").strip()
        url = url_by_provider.get(
            source_id,
            configured_url,
        ).strip()
        classification = source_classification(source)
        if not url:
            reason = f"no URL configured for {source_id}"
            if classification == "required":
                raise ValueError(
                    f"Required external GTFS source {source_id} cannot be skipped: {reason}."
                )
            skipped_sources[source_id] = {
                "sourceID": source_id,
                "classification": classification,
                "status": "skipped",
                "reason": reason,
            }
            print(
                f"[ExternalGTFS] source={source_id} status=skipped "
                f"classification={classification} reason={reason}"
            )
            continue

        raw_artifact_digest: str | None = None
        raw_artifact_size: int | None = None
        if Path(url).is_file() or Path(url).is_dir():
            raw_artifact_digest, raw_artifact_size = artifact_provenance(Path(url))
            input_provenance[source_id] = provenance_record(
                source_id=source_id,
                path=str(Path(url)),
                digest=raw_artifact_digest,
                size=raw_artifact_size,
                origin="external-gtfs",
            )
        else:
            input_provenance[source_id] = {
                "sourceID": source_id,
                "path": url,
                "origin": "external-gtfs",
                "status": "used",
                "resourceIdentity": url,
            }

        cities = load_external_cities(source, repository_root)
        for city in cities:
            city_id = str(city["id"])
            if city_id in occupied:
                previous = external_city_sources.get(city_id)
                same_merge_group = bool(
                    previous
                    and str(source.get("mergeGroup", "")).strip()
                    and str(source.get("mergeGroup"))
                    == str(previous.get("mergeGroup"))
                )
                if not same_merge_group:
                    raise ValueError(
                        f"Duplicate city id across feeds: {city_id} "
                        f"(external source {source_id})."
                    )
            else:
                occupied.add(city_id)
                external_city_sources[city_id] = {
                    "mergeGroup": source.get("mergeGroup"),
                    "namespace": namespace,
                }
                external_cities_by_id[city_id] = city

        build_cache: ExternalBuildCache | None = None
        build_cache_key: CacheKey | None = None
        build_cache_lookup = None
        build_cache_hit = False
        cache_city_id: str | None = None
        if source_id in CACHEABLE_PROVIDER_CITY_IDS:
            eligible, eligibility_reason, cache_city_id = _external_cache_is_eligible(
                source, cities
            )
            if not cache_enabled(environ):
                print(
                    f"[StopData] source={source_id} stage=build-cache status=DISABLED "
                    "reason=feature-gate-off"
                )
            elif not cache_provider_allowed(source_id, environ):
                print(
                    f"[StopData] source={source_id} stage=build-cache status=DISABLED "
                    "reason=provider-not-allowlisted"
                )
            elif not eligible or cache_city_id is None:
                print(
                    f"[StopData] source={source_id} stage=build-cache status=DISABLED "
                    f"reason={eligibility_reason}"
                )
            elif raw_artifact_digest is None:
                print(
                    f"[StopData] source={source_id} stage=build-cache status=MISS "
                    "reason=raw-sha-unavailable rawSHA=n/a "
                    "providerConfig=n/a builder=n/a"
                )
            elif gtfs_cache is None:
                print(
                    f"[StopData] source={source_id} stage=build-cache status=MISS "
                    "reason=cache-root-unavailable rawSHA="
                    f"{raw_artifact_digest[:12]} providerConfig=n/a builder=n/a"
                )
            else:
                try:
                    build_cache_key = cache_key(
                        repository_root=repository_root,
                        provider_id=source_id,
                        raw_sha256=raw_artifact_digest,
                        source=source,
                        city_id=cache_city_id,
                    )
                    build_cache = ExternalBuildCache(
                        Path(gtfs_cache.root) / "external-build",
                        provider_id=source_id,
                        city_id=cache_city_id,
                    )
                    lookup_started = time.monotonic()
                    build_cache_lookup = build_cache.lookup(build_cache_key)
                    build_cache_hit = build_cache_lookup.status == "HIT"
                    print(
                        f"[StopData] source={source_id} stage=build-cache "
                        f"status={build_cache_lookup.status} reason={build_cache_lookup.reason} "
                        f"duration={time.monotonic() - lookup_started:.2f}s "
                        f"key={build_cache_key.value[:12]} rawSHA={raw_artifact_digest[:12]} "
                        f"providerConfig={build_cache_key.provider_config_fingerprint[:12]} "
                        f"builder={build_cache_key.builder_fingerprint[:12]}"
                    )
                except (CacheKeyUnavailable, OSError, ValueError) as error:
                    build_cache = None
                    build_cache_key = None
                    print(
                        f"[StopData] source={source_id} stage=build-cache status=MISS "
                        f"reason=fingerprint-unavailable:{type(error).__name__} "
                        f"rawSHA={raw_artifact_digest[:12]} providerConfig=n/a builder=n/a"
                    )

        dynamic_resource = None
        if source_id not in url_by_provider and isinstance(source.get("dynamicResource"), dict):
            dynamic_resource = resolve_gtfs_resource(source)
        source_url = dynamic_resource.url if dynamic_resource is not None else url
        request_url, headers = authenticated_external_request(
            source_id, source_url, environ=environ
        )
        source_started = time.monotonic()
        resilience_policy = external_gtfs_resilience_policy(source)
        if resilience_policy is not None and gtfs_cache is not None:
            result = gtfs_cache.resolve(
                source_id,
                request_url,
                headers=headers,
                allow_stale=bool(resilience_policy["allowStale"]),
                state_url=url,
                source_version=(
                    {"dynamicVersion": dynamic_resource.version}
                    if dynamic_resource is not None
                    else None
                ),
                metadata_probe=bool(resilience_policy["metadataProbe"]),
                retry_attempts=int(resilience_policy["retryAttempts"]),
                validator=(
                    validate_kyiv_gtfs_archive
                    if bool(resilience_policy["requireDataRows"])
                    else None
                ),
            )
            archive = load_gtfs_archive(str(result.path))
            raw_artifact_digest, raw_artifact_size = artifact_provenance(result.path)
            input_provenance[source_id] = provenance_record(
                source_id=source_id,
                path=str(result.path),
                digest=raw_artifact_digest,
                size=raw_artifact_size,
                origin="external-gtfs-cache",
                status=result.status,
            ) | {
                "resourceIdentity": (
                    dynamic_resource.version
                    if dynamic_resource is not None
                    else url
                )
            }
        else:
            archive = load_gtfs_archive(request_url, headers=headers)
        archive = agency_scoped_archive(archive, source.get("agencyID"))
        supplemental_catalog_configuration = source.get("supplementalStopCatalog")
        supplemental_stop_catalog: object | None = None
        try:
            if supplemental_catalog_configuration is not None:
                if not isinstance(supplemental_catalog_configuration, dict):
                    raise ValueError(
                        f"External GTFS source {source_id} has an invalid "
                        "supplementalStopCatalog."
                    )
                if source_id == "kyiv" and kyiv_resource_cache is not None:
                    supplemental_stop_catalog = load_kyiv_supplemental_catalog(
                        supplemental_catalog_configuration,
                        cache=kyiv_resource_cache,
                    )
                else:
                    catalog_loader = load_stop_catalog or load_json_resource
                    supplemental_stop_catalog = catalog_loader(
                        supplemental_catalog_configuration
                    )
                catalog_identity = ":".join(
                    str(supplemental_catalog_configuration.get(key, ""))
                    for key in ("resourceID", "downloadURL")
                )
                digest, size = canonical_content_provenance(
                    supplemental_stop_catalog,
                    identity=catalog_identity,
                )
                input_provenance[f"{source_id}:supplemental-stop-catalog"] = provenance_record(
                    source_id=f"{source_id}:supplemental-stop-catalog",
                    path=catalog_identity,
                    digest=digest,
                    size=size,
                    origin="Kyiv supplemental stop catalog",
                )
        except Exception:
            archive.close()
            raise
        source_output = output
        if namespace or str(source.get("mergeGroup", "")).strip():
            source_output = namespace_root / source_id
            shutil.rmtree(source_output, ignore_errors=True)
            source_output.mkdir(parents=True, exist_ok=True)
        try:
            package_stops: dict[str, list[dict[str, object]]] = {}
            entries: list[dict[str, object]] = []
            provider_lines: dict[str, dict[str, dict[str, object]]] = {}
            if (
                build_cache_hit
                and build_cache is not None
                and build_cache_lookup is not None
                and cache_city_id is not None
            ):
                restore_started = time.monotonic()
                try:
                    restored = build_cache.restore(build_cache_lookup, source_output)
                    package_stops = {cache_city_id: restored.stops}
                    provider_lines = restored.lines_by_stop_id
                    entries = [
                        _cached_external_manifest_entry(
                            cities[0], len(restored.stops)
                        )
                    ]
                    print(
                        f"[StopData] source={source_id} stage=cache-restore "
                        "status=completed "
                        f"duration={time.monotonic() - restore_started:.2f}s "
                        "mode=copy artifacts=stops,routes,lineMembership"
                    )
                except (OSError, TypeError, ValueError) as error:
                    build_cache_hit = False
                    if build_cache_lookup.directory is not None:
                        shutil.rmtree(build_cache_lookup.directory, ignore_errors=True)
                    print(
                        f"[StopData] source={source_id} stage=build-cache "
                        "status=INVALID reason=restore-failed "
                        f"error={type(error).__name__} "
                        f"duration={time.monotonic() - restore_started:.2f}s"
                    )

            if source.get("buildStops", True):
                if not build_cache_hit:
                    entries, package_stops = _timed_external_stage(
                        source_id,
                        "stops",
                        partial(
                            build_external_stop_packages,
                            archive,
                            cities,
                            source_output,
                            stop_id_mode=str(source.get("stopIDMode", "exact")),
                            namespace=namespace,
                            publish_passenger_stop_ids=bool(
                                source.get("publishPassengerStopIDs", False)
                            ),
                            supplemental_stop_catalog=supplemental_stop_catalog,
                            supplemental_catalog_configuration=(
                                supplemental_catalog_configuration
                                if isinstance(supplemental_catalog_configuration, dict)
                                else None
                            ),
                            exclusive_city_partition=bool(
                                source.get("exclusiveCityPartition", False)
                            ),
                        ),
                    )
                else:
                    print(
                        f"[StopData] source={source_id} stage=stops "
                        "status=cache-hit duration=0.00s"
                    )
                if namespace or str(source.get("mergeGroup", "")).strip():
                    for city in cities:
                        city_id = str(city["id"])
                        namespaced_records.setdefault(city_id, []).append({
                            "sourceID": source_id,
                            "source": source,
                            "city": city,
                            "output": source_output,
                            "packageStops": package_stops.get(city_id, []),
                        })
                else:
                    for entry in entries:
                        entry_with_source = dict(entry)
                        entry_with_source["_source"] = (
                            f"External GTFS source {source_id} "
                            f"({source.get('cities')})"
                        )
                        entry_with_source["country"] = str(source["country"])
                        manifest_entries.append(entry_with_source)
                    package_stops_by_city_id.update(package_stops)

            if source.get("buildRoutes", True):
                if not build_cache_hit:
                    _timed_external_stage(
                        source_id,
                        "routes",
                        partial(
                            build_external_route_index,
                            archive,
                            cities,
                            source_output,
                            namespace=namespace,
                        ),
                    )
                else:
                    print(
                        f"[StopData] source={source_id} stage=routes "
                        "status=cache-hit duration=0.00s"
                    )

            if source.get("buildDepartures", True):
                _timed_external_stage(
                    source_id,
                    "departures",
                    partial(
                        build_external_departure_index,
                        archive,
                        cities,
                        source_output,
                        timezone_name=str(source["timezone"]),
                        namespace=namespace,
                        departure_window_days=int(source.get("departurePackageDays", 3)),
                    ),
                )

            if source.get("buildTripIndex", True):
                _timed_external_stage(
                    source_id,
                    "trip-index",
                    partial(
                        build_external_trip_index,
                        archive,
                        cities,
                        source_output,
                        namespace=namespace,
                    ),
                )
            elif source_id == CTA_PROVIDER_ID:
                print(
                    f"[StopData] source={source_id} stage=trip-index "
                    "status=skipped reason=provider-config-disabled duration=0.00s"
                )

            if source.get("buildRadarTopology", False):
                try:
                    from kyiv_radar_topology import build_radar_topology
                except ImportError:
                    from .kyiv_radar_topology import build_radar_topology

                build_radar_topology(
                    archive,
                    cities,
                    source_output,
                    namespace=namespace,
                )

                if not (namespace or str(source.get("mergeGroup", "")).strip()):
                    for entry in manifest_entries:
                        if str(entry.get("id")) in {str(city["id"]) for city in cities}:
                            entry["radarTopologyURL"] = (
                                f"radar/{entry['id']!s}.json"
                            )

            if package_stops:
                if not build_cache_hit:
                    provider_lines = _timed_external_stage(
                        source_id,
                        "lines",
                        partial(
                            build_external_lines,
                            archive,
                            package_stops,
                            namespace=namespace,
                        ),
                    )
                else:
                    print(
                        f"[StopData] source={source_id} stage=lines "
                        "status=cache-hit duration=0.00s"
                    )
                lines_by_stop_id.update(provider_lines)

            if (
                build_cache is not None
                and build_cache_key is not None
                and not build_cache_hit
            ):
                persist_started = time.monotonic()
                try:
                    build_cache.persist(
                        build_cache_key,
                        source_output,
                        provider_lines,
                    )
                    print(
                        f"[StopData] source={source_id} stage=cache-persist "
                        "status=completed "
                        f"duration={time.monotonic() - persist_started:.2f}s"
                    )
                except (OSError, TypeError, ValueError) as error:
                    print(
                        f"[StopData] source={source_id} stage=cache-persist "
                        "status=failed "
                        f"reason={type(error).__name__} "
                        f"duration={time.monotonic() - persist_started:.2f}s"
                    )
        finally:
            archive.close()

        print(
            f"[StopData] source={source_id} stage=build "
            f"duration={time.monotonic() - source_started:.2f}s"
        )

    for city_id, records in namespaced_records.items():
        merge_group_started = time.monotonic()
        _merge_namespaced_city_records_bounded(
            city_id,
            records,
            output,
            manifest_entries,
            package_stops_by_city_id,
        )
        print(
            f"[ExternalGTFS] city={city_id} stage=merge-group "
            f"duration={time.monotonic() - merge_group_started:.2f}s",
            flush=True,
        )
        continue

        city = records[0]["city"]
        merged_stop_records: list[tuple[str, dict[str, object]]] = []
        merged_routes: dict[str, object] = {}
        merged_departures: dict[str, list[dict[str, object]]] = {}
        merged_platforms: dict[str, set[str]] = {}
        merged_trip_index: dict[str, object] = {}
        timezones: set[str] = set()
        generated_at: str | None = None

        for record in records:
            source_output = Path(record["output"])
            source_id = str(record["sourceID"])
            stop_path = source_output / "stops" / f"{city_id}.json"
            if stop_path.exists():
                merged_stop_records.extend(
                    (source_id, stop)
                    for stop in json.loads(stop_path.read_text(encoding="utf-8"))
                )

            route_path = source_output / "routes" / f"{city_id}.json"
            if route_path.exists():
                payload = json.loads(route_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    overlap = set(merged_routes).intersection(payload)
                    if overlap:
                        raise ValueError(
                            f"External GTFS namespace collision in routes for {city_id}: "
                            f"{sorted(overlap)[:3]}"
                        )
                    merged_routes.update(payload)

            departure_path = source_output / "departures" / f"{city_id}.json"
            if departure_path.exists():
                payload = json.loads(departure_path.read_text(encoding="utf-8"))
                timezones.add(str(payload.get("timezone", "")))
                generated_value = payload.get("generatedAt")
                if isinstance(generated_value, str):
                    generated_at = max(generated_at or generated_value, generated_value)
                for stop_id, items in dict(payload.get("stops") or {}).items():
                    merged_departures.setdefault(str(stop_id), []).extend(items)
                for parent_id, child_ids in dict(payload.get("platforms") or {}).items():
                    merged_platforms.setdefault(str(parent_id), set()).update(
                        str(child_id) for child_id in child_ids
                    )

            trip_path = source_output / "trips" / f"{city_id}.json"
            if trip_path.exists():
                payload = json.loads(trip_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    overlap = set(merged_trip_index).intersection(payload)
                    if overlap:
                        raise ValueError(
                            f"External GTFS namespace collision in trips for {city_id}: "
                            f"{sorted(overlap)[:3]}"
                        )
                    merged_trip_index.update(payload)

        if len(timezones - {""}) > 1:
            raise ValueError(
                f"Merged external city {city_id} has conflicting timezones: "
                f"{sorted(timezones)}"
            )
        merged_stops = _deduplicate_merged_stops(merged_stop_records, city_id)
        if not merged_stops or not merged_departures:
            raise ValueError(f"Merged external city {city_id} has incomplete assets.")

        merged_stops.sort(key=lambda stop: (str(stop.get("searchName", "")), str(stop["id"])))
        stops_directory = output / "stops"
        stops_directory.mkdir(parents=True, exist_ok=True)
        (stops_directory / f"{city_id}.json").write_text(
            json.dumps(merged_stops, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        routes_directory = output / "routes"
        routes_directory.mkdir(parents=True, exist_ok=True)
        (routes_directory / f"{city_id}.json").write_text(
            json.dumps(dict(sorted(merged_routes.items())), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        for stop_id, items in merged_departures.items():
            items.sort(key=lambda item: (str(item.get("p", "")), str(item.get("t", "")), str(item.get("r", ""))))
            merged_departures[stop_id] = _dedupe_exact_departures(items)
        departures_directory = output / "departures"
        departures_directory.mkdir(parents=True, exist_ok=True)
        departure_payload = {
            "generatedAt": generated_at,
            "timezone": next(iter(timezones - {""}), "America/Toronto"),
            "stops": dict(sorted(merged_departures.items())),
            "platforms": {
                parent: sorted(children)
                for parent, children in sorted(merged_platforms.items())
            },
        }
        (departures_directory / f"{city_id}.json").write_text(
            json.dumps(departure_payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        if merged_trip_index:
            trips_directory = output / "trips"
            trips_directory.mkdir(parents=True, exist_ok=True)
            (trips_directory / f"{city_id}.json").write_text(
                json.dumps(dict(sorted(merged_trip_index.items())), ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )

        source_ids = ", ".join(str(record["sourceID"]) for record in records)
        manifest_entries.append({
            "id": city_id,
            "name": city["name"],
            "aliases": city.get("aliases", []),
            "stopCount": len(merged_stops),
            "url": f"stops/{city_id}.json",
            "country": str(records[0]["source"]["country"]),
            "_source": f"External GTFS namespaces: {source_ids}",
        })
        package_stops_by_city_id[city_id] = merged_stops

    provenance_started = time.monotonic()
    provenance_directory = output / "provenance"
    provenance_directory.mkdir(parents=True, exist_ok=True)
    (provenance_directory / "input-artifacts.json").write_text(
        json.dumps(
            {"sources": input_provenance, "skipped": skipped_sources},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        "[ExternalGTFS] stage=merge-provenance "
        f"duration={time.monotonic() - provenance_started:.2f}s",
        flush=True,
    )

    cleanup_started = time.monotonic()
    shutil.rmtree(namespace_root, ignore_errors=True)
    print(
        "[ExternalGTFS] stage=merge-cleanup "
        f"duration={time.monotonic() - cleanup_started:.2f}s",
        flush=True,
    )
    return (
        manifest_entries,
        list(external_cities_by_id.values()),
        package_stops_by_city_id,
        lines_by_stop_id,
    )
