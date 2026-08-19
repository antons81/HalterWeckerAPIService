#!/usr/bin/env python3
"""Official Kyiv Open Data loaders and normalized system artifact builder."""

from __future__ import annotations

import json
import errno
import os
import shutil
import socket
import ssl
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from .artifact_provenance import canonical_content_provenance
except ImportError:
    from artifact_provenance import canonical_content_provenance

try:
    import certifi
except ImportError:  # pragma: no cover - the production image bundles certifi
    certifi = None


CKAN_DATASTORE_URL = (
    "https://data.kyivcity.gov.ua/api/action/datastore_search"
)
KYIV_DATA_HOST = "data.kyivcity.gov.ua"
KYIV_REFERER = "https://data.kyivcity.gov.ua/"
KYIV_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36"
)
KYIV_REQUEST_TIMEOUT_SECONDS = 45
KYIV_MAX_ATTEMPTS = 3
KYIV_RETRY_BACKOFF_SECONDS = (0.5, 1.0)
DEFAULT_KYIV_CACHE_ROOT = Path("/srv/haltewecker/cache/kyiv-open-data")
TLS_CONTEXT = ssl.create_default_context(
    cafile=certifi.where() if certifi is not None else None
)


class KyivOpenDataError(ValueError):
    """Raised when an official Kyiv resource is unavailable or malformed."""


class _KyivHTTPError(KyivOpenDataError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"HTTP {status}")


def request_headers(*, accept: str = "application/json,application/geo+json;q=0.9,*/*;q=0.8") -> dict[str, str]:
    return {
        "Accept": accept,
        "Referer": KYIV_REFERER,
        "User-Agent": KYIV_USER_AGENT,
    }


def _is_transient_error(error: BaseException) -> bool:
    if isinstance(error, _KyivHTTPError):
        return error.status == 429 or 500 <= error.status <= 504
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or 500 <= error.code <= 504
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, BaseException):
            return _is_transient_error(reason)
        return False
    if isinstance(error, (TimeoutError, socket.timeout, ConnectionError)):
        return True
    if isinstance(error, OSError):
        return getattr(error, "errno", None) in {
            errno.ECONNABORTED,
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
            errno.ETIMEDOUT,
            getattr(errno, "EAI_AGAIN", -1),
        }
    return False


def _error_summary(error: BaseException) -> str:
    if isinstance(error, (_KyivHTTPError, urllib.error.HTTPError)):
        status = getattr(error, "status", None) or getattr(error, "code", None)
        return f"HTTP {status}"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, BaseException):
            return _error_summary(reason)
        return "temporary connection failure" if _is_transient_error(error) else "URL error"
    if isinstance(error, ConnectionError) or (
        isinstance(error, OSError)
        and getattr(error, "errno", None)
        in {
            errno.ECONNABORTED,
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
            errno.ETIMEDOUT,
        }
    ):
        return "temporary connection failure"
    return type(error).__name__


def _read_url(
    url: str,
    *,
    opener: Callable[..., Any] | None = None,
    accept: str = "application/json,application/geo+json;q=0.9,*/*;q=0.8",
    source_name: str = "resource",
    sleep: Callable[[float], None] | None = None,
) -> tuple[bytes, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname != KYIV_DATA_HOST:
        raise KyivOpenDataError(f"Kyiv source must use {KYIV_DATA_HOST}")
    request = urllib.request.Request(url, headers=request_headers(accept=accept))
    sleeper = sleep or time.sleep
    for attempt in range(1, KYIV_MAX_ATTEMPTS + 1):
        try:
            if opener is None:
                response_context = urllib.request.urlopen(
                    request,
                    timeout=KYIV_REQUEST_TIMEOUT_SECONDS,
                    context=TLS_CONTEXT,
                )
            else:
                response_context = opener(request, timeout=KYIV_REQUEST_TIMEOUT_SECONDS)
            with response_context as response:
                status = int(getattr(response, "status", 200))
                body = response.read()
                content_type = str(response.headers.get("Content-Type", ""))
            if status < 200 or status >= 300:
                raise _KyivHTTPError(status)
            if not body or body.lstrip().startswith((b"<", b"<!DOCTYPE")):
                raise KyivOpenDataError("Kyiv source returned HTML or empty body")
            return body, content_type
        except Exception as error:
            summary = _error_summary(error)
            print(f"[Kyiv] source={source_name} attempt={attempt} failed: {summary}")
            if attempt >= KYIV_MAX_ATTEMPTS or not _is_transient_error(error):
                raise KyivOpenDataError(
                    f"Kyiv source request failed source={source_name}: {summary}"
                ) from error
            sleeper(KYIV_RETRY_BACKOFF_SECONDS[attempt - 1])
    raise AssertionError("Kyiv retry loop did not return or raise")


def datastore_page(
    resource_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
    opener: Callable[..., Any] | None = None,
    source_name: str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    if limit <= 0 or offset < 0:
        raise KyivOpenDataError("DataStore pagination values are invalid")
    query = urllib.parse.urlencode(
        {"resource_id": resource_id, "limit": limit, "offset": offset}
    )
    body, _ = _read_url(
        f"{CKAN_DATASTORE_URL}?{query}",
        opener=opener,
        source_name=source_name or resource_id,
        sleep=sleep,
    )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise KyivOpenDataError("Kyiv DataStore response is not JSON") from error
    if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
        raise KyivOpenDataError(f"Kyiv DataStore response is unsuccessful for {resource_id}")
    return payload["result"]


def load_datastore_records(
    resource_id: str,
    *,
    page_size: int = 100,
    opener: Callable[..., Any] | None = None,
    source_name: str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Read every DataStore record, honoring result.total and pagination."""
    records: list[dict[str, Any]] = []
    offset = 0
    expected_total: int | None = None
    while True:
        result = datastore_page(
            resource_id,
            limit=page_size,
            offset=offset,
            opener=opener,
            source_name=source_name,
            sleep=sleep,
        )
        raw_total = result.get("total")
        if isinstance(raw_total, int):
            expected_total = raw_total
        page = result.get("records")
        if not isinstance(page, list):
            raise KyivOpenDataError(f"DataStore records are missing for {resource_id}")
        if not page:
            break
        if not all(isinstance(record, dict) for record in page):
            raise KyivOpenDataError(f"DataStore records are not objects for {resource_id}")
        records.extend(page)
        offset += len(page)
        if expected_total is not None and offset >= expected_total:
            break
        if len(page) < page_size:
            break
    if expected_total is None:
        expected_total = len(records)
    if len(records) != expected_total:
        raise KyivOpenDataError(
            f"DataStore pagination incomplete for {resource_id}: "
            f"expected {expected_total}, got {len(records)}"
        )
    return records, expected_total


def resolve_datastore_download_url(
    resource_id: str,
    *,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> str:
    records, total = load_datastore_records(resource_id, opener=opener, sleep=sleep)
    if total == 0 or not records:
        raise KyivOpenDataError(f"DataStore has no public URL for {resource_id}")
    urls = [str(record.get("resource_url", "")).strip() for record in records]
    urls = [url for url in urls if url]
    if not urls:
        raise KyivOpenDataError(f"DataStore has no resource_url for {resource_id}")
    url = urls[0]
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname != KYIV_DATA_HOST or "/data/download" not in parsed.path:
        raise KyivOpenDataError(
            f"DataStore resolved a non-public download URL for {resource_id}"
        )
    return url


def load_json_resource(
    spec: dict[str, Any],
    *,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    """Load a file resource or an is_api resource via CKAN DataStore metadata."""
    source_name = str(spec.get("name") or spec.get("resourceID") or "resource")
    if bool(spec.get("isAPI")):
        records, _ = load_datastore_records(
            str(spec["resourceID"]),
            opener=opener,
            source_name=source_name,
            sleep=sleep,
        )
        resource_urls = [
            str(record.get("resource_url", "")).strip()
            for record in records
            if isinstance(record, dict)
        ]
        resource_urls = [url for url in resource_urls if url]
        if resource_urls:
            url = resource_urls[0]
            parsed = urllib.parse.urlparse(url)
            if parsed.hostname != KYIV_DATA_HOST or "/data/download" not in parsed.path:
                raise KyivOpenDataError(
                    f"DataStore resolved a non-public download URL for {source_name}"
                )
        elif records and all(isinstance(record, dict) for record in records):
            return datastore_records_to_geojson(records, name=str(spec.get("name", "resource")))
        else:
            raise KyivOpenDataError(f"DataStore has no public data for {spec['resourceID']}")
    else:
        url = str(spec.get("downloadURL", "")).strip()
    if not url:
        raise KyivOpenDataError(f"Kyiv resource {source_name} has no public download URL")
    body, _ = _read_url(url, opener=opener, source_name=source_name, sleep=sleep)
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise KyivOpenDataError(
            f"Kyiv resource {spec.get('name', spec.get('resourceID'))} is not JSON"
        ) from error


def _resource_cache_key(spec: dict[str, Any]) -> str:
    resource_id = str(spec.get("resourceID") or spec.get("name") or "").strip()
    if not resource_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in resource_id):
        raise KyivOpenDataError("Kyiv resource cache key is invalid")
    return resource_id


class KyivResourceCache:
    """Atomic last-known-good cache for validated Kyiv JSON resources."""

    def __init__(self, root: Path | str = DEFAULT_KYIV_CACHE_ROOT) -> None:
        self.root = Path(root)

    def path_for(self, spec: dict[str, Any]) -> Path:
        return self.root / f"{_resource_cache_key(spec)}.json"

    def load(self, spec: dict[str, Any]) -> tuple[Any, datetime] | None:
        path = self.path_for(spec)
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(envelope, dict) or envelope.get("schemaVersion") != 1:
                return None
            if str(envelope.get("resourceID", "")) != str(spec.get("resourceID", "")):
                return None
            payload = envelope.get("payload")
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            return payload, modified_at
        except (OSError, TypeError, ValueError):
            return None

    def store(self, spec: dict[str, Any], payload: Any) -> Path:
        path = self.path_for(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "schemaVersion": 1,
                        "resourceID": str(spec.get("resourceID", "")),
                        "name": str(spec.get("name", "")),
                        "fetchedAt": datetime.now(timezone.utc).isoformat(),
                        "payload": payload,
                    },
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
            return path
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass


def _cache_age_text(modified_at: datetime) -> str:
    seconds = max(0, int((datetime.now(timezone.utc) - modified_at).total_seconds()))
    days, remainder = divmod(seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes = remainder // 60
    if days:
        return f"{days}d{hours}h{minutes:02d}m"
    return f"{hours}h{minutes:02d}m"


def datastore_records_to_geojson(
    records: list[dict[str, Any]],
    *,
    name: str,
) -> dict[str, Any]:
    """Normalize a CKAN DataStore record page into the internal GeoJSON shape."""
    features: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        geometry = record.get("geometry")
        if isinstance(geometry, str):
            try:
                geometry = json.loads(geometry)
            except json.JSONDecodeError as error:
                raise KyivOpenDataError(f"Kyiv DataStore {name} geometry is invalid") from error
        properties = record.get("properties")
        if not isinstance(properties, dict):
            properties = {
                key: value
                for key, value in record.items()
                if key not in {"geometry", "_id"}
            }
        if not isinstance(geometry, dict):
            latitude = record.get("latitude", record.get("lat", record.get("stop_lat")))
            longitude = record.get("longitude", record.get("lon", record.get("stop_lon")))
            if latitude is not None and longitude is not None:
                geometry = {
                    "type": "Point",
                    "coordinates": [float(longitude), float(latitude)],
                }
        if not isinstance(geometry, dict):
            raise KyivOpenDataError(f"Kyiv DataStore {name} record has no geometry")
        features.append({
            "type": "Feature",
            "id": record.get("_id", index),
            "geometry": geometry,
            "properties": properties,
        })
    return {"type": "FeatureCollection", "features": features}


def _features(payload: Any, *, name: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise KyivOpenDataError(f"Kyiv {name} is not a GeoJSON feature collection")
    features = payload["features"]
    if not all(isinstance(feature, dict) for feature in features):
        raise KyivOpenDataError(f"Kyiv {name} contains invalid features")
    return features


def _attributes(payload: Any, *, name: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise KyivOpenDataError(f"Kyiv {name} is not an ArcGIS JSON feature collection")
    features = payload["features"]
    attributes = [feature.get("attributes") for feature in features if isinstance(feature, dict)]
    if len(attributes) != len(features) or not all(isinstance(item, dict) for item in attributes):
        raise KyivOpenDataError(f"Kyiv {name} contains invalid attributes")
    return attributes


def _point(feature: dict[str, Any], *, name: str) -> tuple[float, float]:
    geometry = feature.get("geometry")
    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise KyivOpenDataError(f"Kyiv {name} has no point coordinates")
    longitude, latitude = float(coordinates[0]), float(coordinates[1])
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise KyivOpenDataError(f"Kyiv {name} has invalid coordinates")
    return latitude, longitude


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.casefold() in {"none", "null", "nan"}:
        return None
    return text


def normalize_station_features(payload: Any, *, system: str) -> list[dict[str, Any]]:
    stations: list[dict[str, Any]] = []
    for feature in _features(payload, name=f"{system} stations"):
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            raise KyivOpenDataError(f"Kyiv {system} station has no properties")
        station_id = str(properties.get("code1", "")).strip()
        name = str(properties.get("name", "")).strip()
        if not station_id or not name:
            raise KyivOpenDataError(f"Kyiv {system} station is missing code1/name")
        latitude, longitude = _point(feature, name=f"{system} station {station_id}")
        stations.append({
            "id": station_id,
            "name": name,
            "nameEnglish": _optional_text(properties.get("name_eng")),
            "line": _optional_text(properties.get("line")),
            "lineEnglish": _optional_text(properties.get("line_eng")),
            "latitude": latitude,
            "longitude": longitude,
            "transferStation": _optional_text(properties.get("transf_st")),
        })
    if len({item["id"] for item in stations}) != len(stations):
        raise KyivOpenDataError(f"Kyiv {system} station IDs are not unique")
    return stations


def normalize_topology(payload: Any, *, system: str) -> list[dict[str, Any]]:
    topology: list[dict[str, Any]] = []
    for feature in _features(payload, name=f"{system} topology"):
        properties = feature.get("properties")
        geometry = feature.get("geometry")
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            raise KyivOpenDataError(f"Kyiv {system} topology feature is incomplete")
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or geometry.get("type") != "LineString":
            raise KyivOpenDataError(f"Kyiv {system} topology geometry is not LineString")
        topology.append({
            "id": str(properties.get("globalid") or feature.get("id") or ""),
            "fromID": str(properties.get("from_code1", "")).strip(),
            "toID": str(properties.get("to_code1", "")).strip(),
            "line": str(properties.get("num_route", "")).strip(),
            "lineEnglish": str(properties.get("num_r_eng", "")).strip() or None,
            "direction": str(properties.get("napryamok", "")).strip(),
            "order": properties.get("order_"),
            "geometry": coordinates,
        })
    if any(not item["fromID"] or not item["toID"] for item in topology):
        raise KyivOpenDataError(f"Kyiv {system} topology has an incomplete edge")
    return topology


def normalize_stop_times(payload: Any, *, system: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _attributes(payload, name=f"{system} stop times"):
        station_id = str(item.get("code1", "")).strip()
        if not station_id:
            raise KyivOpenDataError(f"Kyiv {system} stop time has no code1")
        result.append({
            "stationID": station_id,
            "name": str(item.get("name", "")).strip() or None,
            "line": str(item.get("line", "")).strip() or None,
            "direction": str(item.get("napryamok", "")).strip() or None,
            "first": str(item.get("first_trn1", "")).strip() or None,
            "last": str(item.get("last_trn1", "")).strip() or None,
        })
    return result


def normalize_periods(payload: Any, *, system: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _attributes(payload, name=f"{system} periods"):
        result.append({
            "line": str(item.get("line", "")).strip(),
            "timePeriod": str(item.get("timeperiod", "")).strip(),
            "weekdayForward": str(item.get("st_weekday", "")).strip(),
            "weekdayReverse": str(item.get("rv_weekday", "")).strip(),
            "holidayForward": str(item.get("st_holiday", "")).strip(),
            "holidayReverse": str(item.get("rv_holiday", "")).strip(),
        })
    return result


def normalize_calendar(payload: Any, *, system: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _attributes(payload, name=f"{system} calendar"):
        result.append({
            "stationID": str(item.get("code1", "")).strip(),
            "line": str(item.get("line", "")).strip() or None,
            "opening": str(item.get("open_max", "")).strip() or None,
            "closing": str(item.get("close_min", "")).strip() or None,
        })
    return result


def normalize_interchanges(payload: Any, *, system: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _attributes(payload, name=f"{system} interchanges"):
        result.append({
            "fromID": str(item.get("from_code1", "")).strip(),
            "toID": str(item.get("to_code1", "")).strip(),
            "fromName": str(item.get("from_name", "")).strip() or None,
            "toName": str(item.get("to_name", "")).strip() or None,
            "comment": str(item.get("comment", "")).strip() or None,
        })
    return result


def normalize_city_express_stop_times(payload: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _attributes(payload, name="city express stop times"):
        station_id = str(item.get("code1", "")).strip()
        train_id = str(item.get("train", "")).strip()
        if not station_id or not train_id:
            raise KyivOpenDataError("City Express stop time is missing code1/train")
        result.append({
            "stationID": station_id,
            "name": str(item.get("name", "")).strip() or None,
            "route": str(item.get("num_route", "")).strip(),
            "routeEnglish": str(item.get("num_r_eng", "")).strip() or None,
            "direction": str(item.get("napryamok", "")).strip(),
            "service": str(item.get("type", "")).strip(),
            "trainID": train_id,
            "arrival": str(item.get("arrival", "")).strip() or None,
            "departure": str(item.get("departure", "")).strip() or None,
            "actual": bool(item.get("actual")),
        })
    return result


def validate_kyiv_resource_payload(
    name: str,
    payload: Any,
    config: dict[str, Any],
) -> None:
    """Validate one resource before it is allowed into the local cache."""
    if name == "metroStations":
        stations = normalize_station_features(payload, system="metro")
        if len(stations) != int(config["expectedCounts"]["metroStations"]):
            raise KyivOpenDataError(f"Kyiv metro station count changed: {len(stations)}")
    elif name == "funicularStations":
        stations = normalize_station_features(payload, system="funicular")
        if len(stations) != int(config["expectedCounts"]["funicularStations"]):
            raise KyivOpenDataError(
                f"Kyiv funicular station count changed: {len(stations)}"
            )
        expected_ids = set(
            config.get("expectedIDs", {}).get("funicularStations", ["fn01", "fn02"])
        )
        if {station["id"] for station in stations} != expected_ids:
            raise KyivOpenDataError("Kyiv funicular station IDs are not fn01/fn02")
    elif name == "expressStations":
        stations = normalize_station_features(payload, system="cityExpress")
        if len(stations) != int(config["expectedCounts"]["expressPlatforms"]):
            raise KyivOpenDataError(
                f"Kyiv City Express platform count changed: {len(stations)}"
            )
    elif name == "metroTopology":
        normalize_topology(payload, system="metro")
    elif name == "funicularTopology":
        normalize_topology(payload, system="funicular")
    elif name == "expressTopology":
        normalize_topology(payload, system="cityExpress")
    elif name in {"metroStopTimes", "funicularStopTimes"}:
        normalize_stop_times(payload, system=name.removesuffix("StopTimes"))
    elif name == "expressStopTimes":
        stop_times = normalize_city_express_stop_times(payload)
        if len(stop_times) != int(config["expectedCounts"]["expressStopTimes"]):
            raise KyivOpenDataError(
                f"Kyiv City Express stop time count changed: {len(stop_times)}"
            )
    elif name in {"metroPeriods", "funicularPeriods"}:
        normalize_periods(payload, system=name.removesuffix("Periods"))
    elif name in {"metroCalendar", "funicularCalendar"}:
        normalize_calendar(payload, system=name.removesuffix("Calendar"))
    elif name in {"metroInterchanges", "funicularInterchanges", "expressInterchanges"}:
        normalize_interchanges(payload, system=name.removesuffix("Interchanges"))
    else:
        raise KyivOpenDataError(f"Kyiv resource {name} has no validation rule")


def validate_kyiv_systems_artifact(path: Path) -> dict[str, Any]:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise KyivOpenDataError("Previous Kyiv systems artifact is not valid JSON") from error
    if not isinstance(artifact, dict):
        raise KyivOpenDataError("Previous Kyiv systems artifact is not an object")
    if artifact.get("schemaVersion") != 1 or artifact.get("cityID") != "kyiv":
        raise KyivOpenDataError("Previous Kyiv systems artifact has an invalid identity")
    source = artifact.get("source")
    systems = artifact.get("systems")
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("contentDigest"), str)
        or not source["contentDigest"]
        or not isinstance(source.get("contentSize"), int)
        or source["contentSize"] <= 0
        or not isinstance(systems, dict)
    ):
        raise KyivOpenDataError("Previous Kyiv systems artifact provenance is incomplete")
    required_systems = {
        "metro": {"stations", "topology", "stopTimes", "periods", "calendar", "interchanges"},
        "funicular": {"stations", "topology", "stopTimes", "periods", "calendar", "interchanges"},
        "cityExpress": {"platforms", "topology", "stopTimes", "interchanges"},
    }
    for system_name, required_keys in required_systems.items():
        system = systems.get(system_name)
        if not isinstance(system, dict) or not required_keys.issubset(system):
            raise KyivOpenDataError(
                f"Previous Kyiv systems artifact is missing {system_name} data"
            )
        if any(not isinstance(system[key], list) for key in required_keys):
            raise KyivOpenDataError(
                f"Previous Kyiv systems artifact has invalid {system_name} data"
            )

        primary_key = "platforms" if system_name == "cityExpress" else "stations"
        if not system[primary_key]:
            raise KyivOpenDataError(
                f"Previous Kyiv systems artifact has no {system_name} {primary_key}"
            )
        for item in system[primary_key]:
            if not isinstance(item, dict):
                raise KyivOpenDataError(
                    f"Previous Kyiv systems artifact has invalid {system_name} {primary_key}"
                )
            if not all(str(item.get(field, "")).strip() for field in ("id", "name")):
                raise KyivOpenDataError(
                    f"Previous Kyiv systems artifact has incomplete {system_name} {primary_key}"
                )
            try:
                latitude = float(item["latitude"])
                longitude = float(item["longitude"])
            except (KeyError, TypeError, ValueError) as error:
                raise KyivOpenDataError(
                    f"Previous Kyiv systems artifact has invalid {system_name} coordinates"
                ) from error
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise KyivOpenDataError(
                    f"Previous Kyiv systems artifact has invalid {system_name} coordinates"
                )

        if not system["stopTimes"]:
            raise KyivOpenDataError(
                f"Previous Kyiv systems artifact has no {system_name} stop times"
            )
        for item in system["stopTimes"]:
            if not isinstance(item, dict) or not str(item.get("stationID", "")).strip():
                raise KyivOpenDataError(
                    f"Previous Kyiv systems artifact has invalid {system_name} stop times"
                )
            if system_name == "cityExpress" and not str(item.get("trainID", "")).strip():
                raise KyivOpenDataError(
                    "Previous Kyiv systems artifact has invalid cityExpress trains"
                )
    return artifact


def copy_validated_kyiv_systems_artifact(source: Path, output: Path) -> Path:
    """Copy a previous artifact into staging without exposing a partial file."""
    validate_kyiv_systems_artifact(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as destination, source.open("rb") as origin:
            shutil.copyfileobj(origin, destination)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, output)
        KyivResourceCache._fsync_directory(output.parent)
        return output
    finally:
        temporary.unlink(missing_ok=True)


def build_kyiv_systems_artifact(
    *,
    repository_root: Path,
    output: Path,
    sources_path: Path | None = None,
    opener: Callable[..., Any] | None = None,
    cache_root: Path | str | None = DEFAULT_KYIV_CACHE_ROOT,
    sleep: Callable[[float], None] | None = None,
) -> Path:
    config_path = sources_path or repository_root / "config" / "kyiv-systems-resources.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("resources"), dict):
        raise KyivOpenDataError("Kyiv systems resource config is invalid")
    resources = config["resources"]
    cache = KyivResourceCache(cache_root) if cache_root is not None else None
    loaded: dict[str, Any] = {}
    for name, spec in resources.items():
        try:
            payload = load_json_resource(spec, opener=opener, sleep=sleep)
            validate_kyiv_resource_payload(name, payload, config)
            if cache is not None:
                try:
                    cache.store(spec, payload)
                except OSError as error:
                    print(f"[Kyiv] source={name} cache update failed: {type(error).__name__}")
            loaded[name] = payload
        except KyivOpenDataError as error:
            cached = cache.load(spec) if cache is not None else None
            if cached is None:
                raise
            cached_payload, modified_at = cached
            try:
                validate_kyiv_resource_payload(name, cached_payload, config)
            except KyivOpenDataError:
                raise error
            print(
                f"[Kyiv] source={name} using cached resource "
                f"age={_cache_age_text(modified_at)}"
            )
            loaded[name] = cached_payload
    provenance_payload = {
        name: {
            "resourceID": str(spec["resourceID"]),
            "payload": loaded[name],
        }
        for name, spec in sorted(resources.items())
    }
    content_digest, content_size = canonical_content_provenance(
        provenance_payload,
        identity="kyiv-systems-resources-v1",
    )
    metro_stations = normalize_station_features(loaded["metroStations"], system="metro")
    funicular_stations = normalize_station_features(loaded["funicularStations"], system="funicular")
    express_stations = normalize_station_features(loaded["expressStations"], system="cityExpress")
    if len(metro_stations) != int(config["expectedCounts"]["metroStations"]):
        raise KyivOpenDataError(f"Kyiv metro station count changed: {len(metro_stations)}")
    if len(funicular_stations) != int(config["expectedCounts"]["funicularStations"]):
        raise KyivOpenDataError(f"Kyiv funicular station count changed: {len(funicular_stations)}")
    expected_funicular_ids = set(
        config.get("expectedIDs", {}).get("funicularStations", ["fn01", "fn02"])
    )
    if {station["id"] for station in funicular_stations} != expected_funicular_ids:
        raise KyivOpenDataError("Kyiv funicular station IDs are not fn01/fn02")
    if len(express_stations) != int(config["expectedCounts"]["expressPlatforms"]):
        raise KyivOpenDataError(f"Kyiv City Express platform count changed: {len(express_stations)}")
    artifact = {
        "schemaVersion": 1,
        "cityID": "kyiv",
        "timezone": "Europe/Kyiv",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": {
            "provider": "Kyiv Open Data Portal",
            "baseURL": "https://data.kyivcity.gov.ua/",
            "resourceIDs": {
                name: str(spec["resourceID"])
                for name, spec in resources.items()
            },
            "contentDigest": content_digest,
            "contentSize": content_size,
            "provenanceStatus": "used",
            "provenanceIdentity": "kyiv-systems-resources-v1",
        },
        "systems": {
            "metro": {
                "stations": metro_stations,
                "topology": normalize_topology(loaded["metroTopology"], system="metro"),
                "stopTimes": normalize_stop_times(loaded["metroStopTimes"], system="metro"),
                "periods": normalize_periods(loaded["metroPeriods"], system="metro"),
                "calendar": normalize_calendar(loaded["metroCalendar"], system="metro"),
                "interchanges": normalize_interchanges(loaded["metroInterchanges"], system="metro"),
            },
            "funicular": {
                "stations": funicular_stations,
                "topology": normalize_topology(loaded["funicularTopology"], system="funicular"),
                "stopTimes": normalize_stop_times(loaded["funicularStopTimes"], system="funicular"),
                "periods": normalize_periods(loaded["funicularPeriods"], system="funicular"),
                "calendar": normalize_calendar(loaded["funicularCalendar"], system="funicular"),
                "interchanges": normalize_interchanges(loaded["funicularInterchanges"], system="funicular"),
            },
            "cityExpress": {
                "platforms": express_stations,
                "topology": normalize_topology(loaded["expressTopology"], system="cityExpress"),
                "stopTimes": normalize_city_express_stop_times(loaded["expressStopTimes"]),
                "interchanges": normalize_interchanges(loaded["expressInterchanges"], system="cityExpress"),
            },
        },
    }
    city_express = artifact["systems"]["cityExpress"]
    if len(city_express["stopTimes"]) != int(config["expectedCounts"]["expressStopTimes"]):
        raise KyivOpenDataError(
            f"Kyiv City Express stop time count changed: {len(city_express['stopTimes'])}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        KyivResourceCache._fsync_directory(output.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def build_kyiv_systems_asset_for_release(release_dir: Path, repository_root: Path) -> Path:
    output = release_dir / "stop-data" / "transit" / "kyiv-systems.json"
    return build_kyiv_systems_artifact(repository_root=repository_root, output=output)
