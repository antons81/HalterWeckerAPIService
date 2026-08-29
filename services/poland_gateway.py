"""Generic Poland GTFS-RT gateways built on the shared GTFS-RT layer."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import Callable, Iterable, Mapping
from urllib.parse import quote

try:
    from dynamic_resource_resolver import DynamicResourceError, resolve_realtime_manifest
except ImportError:
    from scripts.dynamic_resource_resolver import DynamicResourceError, resolve_realtime_manifest
from gtfsrt_gateway import (
    GTFSRealtimeFeed,
    GTFSRealtimeGatewayError,
    GTFSRealtimeHTTPTransport,
    GTFSRealtimeHTTPResponse,
    GatewayResponse,
    RealtimeAlert,
    RealtimeUpdate,
    RealtimeVehiclePosition,
    parse_gtfs_realtime_feed,
)


LOGGER = logging.getLogger("haltewecker.poland_gateway")
POLAND_CACHE_TTL_SECONDS = 15.0
POLAND_MAX_STALE_SECONDS = 120.0
MAX_REQUESTED_STOPS = 64


@dataclass(frozen=True)
class _SourceResult:
    source_id: str
    feed: GTFSRealtimeFeed | None
    http_status: int | None
    error: str | None
    fetch_completed_at: float | None = None


@dataclass(frozen=True)
class _Snapshot:
    retrieved_at: float
    feed_timestamp: int | None
    entity_count: int
    trip_updates: tuple[RealtimeUpdate, ...]
    vehicle_positions: tuple[RealtimeVehiclePosition, ...]
    alerts: tuple[RealtimeAlert, ...]
    source_results: tuple[_SourceResult, ...]
    stale_coordinate_count: int
    invalid_coordinate_count: int


class _CombinedFeedCache:
    """Share one parsed combined feed between the provider's RT gateways."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        ttl: float = POLAND_CACHE_TTL_SECONDS,
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._entries: dict[str, tuple[float, GTFSRealtimeHTTPResponse, GTFSRealtimeFeed]] = {}
        self._lock = threading.Lock()

    def fetch(
        self,
        url: str,
        transport: GTFSRealtimeHTTPTransport,
    ) -> tuple[GTFSRealtimeHTTPResponse, GTFSRealtimeFeed, float]:
        """Fetch and parse a combined payload once for the current refresh window."""
        now = self._clock()
        with self._lock:
            cached = self._entries.get(url)
            if cached is not None and now - cached[0] <= self._ttl:
                fetched_at, response, feed = cached
                return response, feed, fetched_at

            response = transport.fetch(url)
            fetched_at = self._clock()
            feed = parse_gtfs_realtime_feed(
                response.body,
                include_missing_stop_ids=True,
            )
            self._entries[url] = (fetched_at, response, feed)
            return response, feed, fetched_at


def _iso_time(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _in_region(latitude: float, longitude: float, region: Mapping[str, object]) -> bool:
    try:
        return (
            float(region["minimumLatitude"]) <= latitude <= float(region["maximumLatitude"])
            and float(region["minimumLongitude"]) <= longitude <= float(region["maximumLongitude"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def _namespace(namespace: str, value: str | None) -> str | None:
    if not value:
        return None
    return f"{namespace}{value}" if namespace else value


def _parse_timestamp(value: object, timezone_name: str) -> int | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo

            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except Exception:
            return None
    return int(parsed.timestamp())


def _first_value(record: Mapping[str, object], keys: Iterable[str]) -> object:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def parse_wroclaw_vehicle_positions(
    body: bytes,
    *,
    timezone_name: str = "Europe/Warsaw",
) -> tuple[int | None, int, tuple[RealtimeVehiclePosition, ...]]:
    """Parse the official Wrocław vehicle JSON dataset without interpolation."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GTFSRealtimeGatewayError("invalid Wrocław vehicle JSON") from error
    records: object = payload
    if isinstance(payload, dict):
        records = _first_value(payload, ("dane", "vehicles", "data", "results"))
    if not isinstance(records, list):
        raise GTFSRealtimeGatewayError("Wrocław vehicle JSON is not a list")

    latest_timestamp: int | None = None
    vehicles: dict[str, RealtimeVehiclePosition] = {}
    for index, raw_record in enumerate(records):
        if not isinstance(raw_record, dict):
            continue
        record = {str(key): value for key, value in raw_record.items()}
        latitude_value = _first_value(
            record,
            ("Ostatnia_Pozycja_Szerokosc", "latitude", "lat"),
        )
        longitude_value = _first_value(
            record,
            ("Ostatnia_Pozycja_Dlugosc", "longitude", "lon", "lng"),
        )
        try:
            latitude = float(latitude_value)
            longitude = float(longitude_value)
        except (TypeError, ValueError):
            continue
        vehicle_id = str(
            _first_value(record, ("Nr_Boczny", "vehicleID", "vehicle_id")) or ""
        ).strip()
        if not vehicle_id:
            continue
        timestamp = _parse_timestamp(
            _first_value(record, ("Data_Aktualizacji", "timestamp", "updatedAt")),
            timezone_name,
        )
        latest_timestamp = max(
            (value for value in (latest_timestamp, timestamp) if value is not None),
            default=None,
        )
        route_id = str(
            _first_value(record, ("Nazwa_Linii", "routeID", "route_id")) or ""
        ).strip() or None
        if route_id is None:
            continue
        trip_id = str(
            _first_value(record, ("Brygada", "tripID", "trip_id")) or ""
        ).strip() or None
        vehicles[vehicle_id] = RealtimeVehiclePosition(
            vehicle_id=vehicle_id,
            trip_id=trip_id,
            route_id=route_id,
            direction_id=None,
            stop_id=None,
            stop_sequence=None,
            latitude=latitude,
            longitude=longitude,
            bearing=None,
            speed=None,
            timestamp=timestamp,
        )
    return latest_timestamp, len(records), tuple(vehicles.values())


def _parse_gdynia_records(payload: object) -> list[Mapping[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("departures", "delay", "delays", "results", "data", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [payload]


def _parse_gdynia_delay_payload(
    body: bytes,
    *,
    namespace: str,
    requested_stop_id: str,
) -> tuple[RealtimeUpdate, ...]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GTFSRealtimeGatewayError("invalid Gdynia delay JSON") from error
    updates: list[RealtimeUpdate] = []
    last_update = _first_value(payload, ("lastUpdate",)) if isinstance(payload, dict) else None
    last_update_timestamp = _parse_timestamp(last_update, "Europe/Warsaw")
    last_update_date = None
    if isinstance(last_update, str):
        try:
            last_update_date = datetime.fromisoformat(
                last_update.replace("Z", "+00:00")
            ).date()
        except ValueError:
            pass
    for index, record in enumerate(_parse_gdynia_records(payload)):
        route_id = str(
            _first_value(record, ("routeID", "routeId", "line", "lineNumber", "route")) or ""
        ).strip()
        trip_id = str(
            _first_value(record, ("trip", "tripID", "tripId", "course", "courseId", "vehicleId"))
            or f"delay-{requested_stop_id}-{index}"
        ).strip()
        stop_id = str(
            _first_value(record, ("stopID", "stopId", "stop", "stop_code"))
            or requested_stop_id
        ).strip()
        delay_value = _first_value(
            record,
            ("delayInSeconds", "delaySeconds", "delay", "delay_sec", "deviation"),
        )
        try:
            delay_seconds = int(float(delay_value)) if delay_value is not None else None
        except (TypeError, ValueError):
            delay_seconds = None
        effective_value = _first_value(
            record,
            ("effectiveTime", "expectedTime", "departureTime", "estimatedTime", "time"),
        )
        effective_time = _parse_timestamp(effective_value, "Europe/Warsaw")
        if effective_time is None and isinstance(effective_value, str) and last_update_date:
            effective_time = _parse_timestamp(
                f"{last_update_date.isoformat()}T{effective_value}",
                "Europe/Warsaw",
            )
        if (
            effective_time is not None
            and last_update_timestamp is not None
            and effective_time < last_update_timestamp - 12 * 60 * 60
        ):
            effective_time += 24 * 60 * 60
        updates.append(
            RealtimeUpdate(
                trip_id=trip_id,
                route_id=route_id,
                direction_id=None,
                stop_id=stop_id.removeprefix(namespace),
                stop_sequence=None,
                effective_time=effective_time,
                delay_seconds=delay_seconds,
                is_cancelled=False,
            )
        )
    return tuple(updates)


class PolandGTFSRealtimeGateway:
    """Cache and normalize one or more Polish GTFS-RT sources."""

    def __init__(
        self,
        *,
        provider_id: str,
        city_ids: set[str],
        sources: Iterable[Mapping[str, object]],
        path: str,
        kind: str,
        city_regions: Mapping[str, Mapping[str, object]] | None = None,
        clock: Callable[[], float] = time.time,
        cache_ttl: float = POLAND_CACHE_TTL_SECONDS,
        max_stale: float = POLAND_MAX_STALE_SECONDS,
        transport: GTFSRealtimeHTTPTransport | None = None,
        combined_feed_cache: _CombinedFeedCache | None = None,
        trip_stop_resolver: Callable[
            [str, set[tuple[str, int]]], Mapping[tuple[str, int], str]
        ] | None = None,
    ) -> None:
        if kind not in {"tripUpdates", "vehiclePositions", "alerts"}:
            raise ValueError(f"unsupported Poland gateway kind: {kind}")
        self._provider_id = provider_id
        self._city_ids = frozenset(city_ids)
        self._sources = tuple(dict(source) for source in sources)
        self._path = path
        self._kind = kind
        self._city_regions = dict(city_regions or {})
        self._clock = clock
        self._cache_ttl = cache_ttl
        self._max_stale = max_stale
        self._transport = transport or GTFSRealtimeHTTPTransport(
            f"HalteWecker-{provider_id}-GTFSRT/1.0"
        )
        self._combined_feed_cache = combined_feed_cache
        self._trip_stop_resolver = trip_stop_resolver
        self._manifest_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._snapshot: _Snapshot | None = None
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    @staticmethod
    def _requested_stop_ids(query: Mapping[str, list[str]]) -> list[str]:
        values = query.get("stopIDs", []) + query.get("stopID", [])
        stop_ids = [part.strip() for value in values for part in value.split(",")]
        unique = list(dict.fromkeys(value for value in stop_ids if value))
        if not unique or len(unique) > MAX_REQUESTED_STOPS:
            raise GTFSRealtimeGatewayError("invalid stop selection")
        return unique

    def _source_urls(self, source: Mapping[str, object]) -> dict[str, str]:
        realtime = source.get("realtime")
        result = {
            str(key): str(value).strip()
            for key, value in (realtime.items() if isinstance(realtime, dict) else ())
            if isinstance(value, str) and value.strip()
        }
        if "combined" not in result and "combinedURL" in result:
            result["combined"] = result["combinedURL"]
        manifest_url = result.get("manifestURL")
        if not manifest_url:
            return result
        now = self._clock()
        cached = self._manifest_cache.get(manifest_url)
        if cached is not None and now - cached[0] <= self._cache_ttl:
            return result | cached[1]
        manifest = resolve_realtime_manifest(manifest_url)
        self._manifest_cache[manifest_url] = (now, manifest)
        return result | manifest

    def _fetch_source(self, source: Mapping[str, object]) -> _SourceResult:
        source_id = str(source.get("id", ""))
        try:
            urls = self._source_urls(source)
            if self._kind == "vehiclePositions" and urls.get("vehicleFormat") == "wroclawJSON":
                url = urls.get("vehiclePositionsURL")
                if not url:
                    raise GTFSRealtimeGatewayError("vehicle JSON URL is missing")
                response = self._transport.fetch_raw(
                    url,
                    accept="application/json, text/json",
                )
                fetch_completed_at = self._clock()
                timestamp, entity_count, vehicles = parse_wroclaw_vehicle_positions(
                    response.body,
                    timezone_name=str(source.get("timezone") or "Europe/Warsaw"),
                )
                feed = GTFSRealtimeFeed(
                    feed_timestamp=timestamp,
                    entity_count=entity_count,
                    trip_updates=(),
                    vehicle_positions=vehicles,
                    alerts=(),
                )
                return _SourceResult(
                    source_id,
                    feed,
                    response.status,
                    None,
                    fetch_completed_at,
                )

            url_key = {
                "tripUpdates": "tripUpdatesURL",
                "vehiclePositions": "vehiclePositionsURL",
                "alerts": "alertsURL",
            }[self._kind]
            combined_url = urls.get("combined")
            url = urls.get(url_key) or combined_url
            if not url:
                raise GTFSRealtimeGatewayError(f"{self._kind} URL is missing")
            if combined_url == url and self._combined_feed_cache is not None:
                response, feed, fetch_completed_at = self._combined_feed_cache.fetch(
                    url,
                    self._transport,
                )
            else:
                response = self._transport.fetch(url)
                fetch_completed_at = self._clock()
                feed = parse_gtfs_realtime_feed(
                    response.body,
                    include_missing_stop_ids=self._kind == "tripUpdates",
                )
            return _SourceResult(
                source_id,
                feed,
                response.status,
                None,
                fetch_completed_at,
            )
        except DynamicResourceError as error:
            return _SourceResult(source_id, None, None, str(error))
        except GTFSRealtimeGatewayError as error:
            return _SourceResult(source_id, None, getattr(error, "status_code", None), str(error))
        except Exception as error:
            return _SourceResult(source_id, None, None, type(error).__name__)

    def _fetch_snapshot(self) -> _Snapshot:
        results = tuple(self._fetch_source(source) for source in self._sources)
        successful = tuple(result for result in results if result.feed is not None)
        if not successful:
            reason = "; ".join(
                f"{result.source_id}:{result.error or 'unavailable'}" for result in results
            )
            raise GTFSRealtimeGatewayError(reason or "realtime source unavailable")

        feeds = tuple(result.feed for result in successful if result.feed is not None)
        feed_timestamps = [feed.feed_timestamp for feed in feeds if feed.feed_timestamp is not None]
        vehicles = tuple(vehicle for feed in feeds for vehicle in feed.vehicle_positions)
        stale_coordinate_count = sum(
            1
            for result in successful
            if result.fetch_completed_at is not None
            for vehicle in result.feed.vehicle_positions
            if vehicle.timestamp is not None
            and result.fetch_completed_at - vehicle.timestamp > self._max_stale
        )
        invalid_coordinate_count = sum(
            1
            for vehicle in vehicles
            if not (
                math.isfinite(vehicle.latitude)
                and math.isfinite(vehicle.longitude)
                and -90 <= vehicle.latitude <= 90
                and -180 <= vehicle.longitude <= 180
                and not (vehicle.latitude == 0 and vehicle.longitude == 0)
            )
        )
        snapshot = _Snapshot(
            retrieved_at=self._clock(),
            feed_timestamp=max(feed_timestamps, default=None),
            entity_count=sum(feed.entity_count for feed in feeds),
            trip_updates=tuple(update for feed in feeds for update in feed.trip_updates),
            vehicle_positions=vehicles,
            alerts=tuple(alert for feed in feeds for alert in feed.alerts),
            source_results=results,
            stale_coordinate_count=stale_coordinate_count,
            invalid_coordinate_count=invalid_coordinate_count,
        )
        LOGGER.info(
            "event=poland_realtime_refresh provider=%s kind=%s sources=%d successful=%d "
            "entities=%d tripUpdates=%d vehicles=%d alerts=%d stale=%d invalidCoordinates=%d",
            self._provider_id,
            self._kind,
            len(results),
            len(successful),
            snapshot.entity_count,
            len(snapshot.trip_updates),
            len(snapshot.vehicle_positions),
            len(snapshot.alerts),
            snapshot.stale_coordinate_count,
            snapshot.invalid_coordinate_count,
        )
        return snapshot

    def _snapshot_for_request(self) -> tuple[_Snapshot, bool]:
        now = self._clock()
        with self._lock:
            cached = self._snapshot
            if cached is not None and now - cached.retrieved_at <= self._cache_ttl:
                return cached, False
        with self._refresh_lock:
            now = self._clock()
            with self._lock:
                cached = self._snapshot
                if cached is not None and now - cached.retrieved_at <= self._cache_ttl:
                    return cached, False
            try:
                fresh = self._fetch_snapshot()
            except GTFSRealtimeGatewayError:
                if cached is not None and now - cached.retrieved_at <= self._max_stale:
                    return cached, True
                raise
            with self._lock:
                self._snapshot = fresh
            return fresh, False

    def _health(self, snapshot: _Snapshot, stale: bool) -> dict[str, object]:
        success_count = sum(result.feed is not None for result in snapshot.source_results)
        source_count = len(snapshot.source_results)
        status = "stale" if stale else "healthy"
        if success_count < source_count:
            status = "degraded" if success_count else "unavailable"
        return {
            "status": status,
            "sourceCount": source_count,
            "successfulSourceCount": success_count,
            "partial": success_count < source_count,
            "httpStatuses": {
                result.source_id: result.http_status
                for result in snapshot.source_results
                if result.http_status is not None
            },
            "sourceFetchCompletedAt": {
                result.source_id: _iso_time(int(result.fetch_completed_at))
                for result in snapshot.source_results
                if result.fetch_completed_at is not None
            },
            "errors": {
                result.source_id: result.error
                for result in snapshot.source_results
                if result.error
            },
            "entityCount": snapshot.entity_count,
            "tripUpdateCount": len(snapshot.trip_updates),
            "vehiclePositionCount": len(snapshot.vehicle_positions),
            "alertCount": len(snapshot.alerts),
            "staleCoordinateCount": snapshot.stale_coordinate_count,
            "invalidCoordinateCount": snapshot.invalid_coordinate_count,
            "maxStaleSeconds": self._max_stale,
        }

    def _namespace_for_source(self, source_id: str) -> str:
        for source in self._sources:
            if str(source.get("id", "")) == source_id:
                return str(source.get("namespace") or "")
        return ""

    def _vehicle_payload(
        self,
        snapshot: _Snapshot,
        city_id: str,
    ) -> tuple[list[dict[str, object]], int]:
        region = self._city_regions.get(city_id)
        vehicles: list[dict[str, object]] = []
        dropped = 0
        now = self._clock()
        for result in snapshot.source_results:
            if result.feed is None:
                continue
            namespace = self._namespace_for_source(result.source_id)
            for vehicle in result.feed.vehicle_positions:
                if not (
                    math.isfinite(vehicle.latitude)
                    and math.isfinite(vehicle.longitude)
                    and -90 <= vehicle.latitude <= 90
                    and -180 <= vehicle.longitude <= 180
                    and not (vehicle.latitude == 0 and vehicle.longitude == 0)
                ):
                    dropped += 1
                    continue
                if vehicle.timestamp is not None and now - vehicle.timestamp > self._max_stale:
                    dropped += 1
                    continue
                if region is not None and not _in_region(vehicle.latitude, vehicle.longitude, region):
                    dropped += 1
                    continue
                vehicles.append(
                    {
                        "vehicleID": _namespace(namespace, vehicle.vehicle_id),
                        "tripID": _namespace(namespace, vehicle.trip_id),
                        "routeID": _namespace(namespace, vehicle.route_id),
                        "directionID": vehicle.direction_id,
                        "destination": None,
                        "stopID": _namespace(namespace, vehicle.stop_id),
                        "stopSequence": vehicle.stop_sequence,
                        "latitude": vehicle.latitude,
                        "longitude": vehicle.longitude,
                        "bearing": vehicle.bearing,
                        "speed": vehicle.speed,
                        "timestamp": _iso_time(vehicle.timestamp),
                    }
                )
        vehicles.sort(key=lambda item: str(item["vehicleID"]))
        return vehicles, dropped

    def _trip_payload(
        self,
        snapshot: _Snapshot,
        stop_ids: set[str],
    ) -> list[dict[str, object]]:
        result = []
        for source_result in snapshot.source_results:
            if source_result.feed is None:
                continue
            namespace = self._namespace_for_source(source_result.source_id)
            resolved_stop_ids: Mapping[tuple[str, int], str] = {}
            if self._trip_stop_resolver is not None:
                sequence_keys = {
                    (update.trip_id, update.stop_sequence)
                    for update in source_result.feed.trip_updates
                    if not update.stop_id and update.stop_sequence is not None
                }
                if sequence_keys:
                    try:
                        resolved_stop_ids = self._trip_stop_resolver(
                            source_result.source_id,
                            sequence_keys,
                        )
                    except Exception as error:
                        LOGGER.warning(
                            "event=poland_trip_stop_resolution_failed provider=%s source=%s reason=%s",
                            self._provider_id,
                            source_result.source_id,
                            type(error).__name__,
                        )
            for update in source_result.feed.trip_updates:
                raw_stop_id = update.stop_id or (
                    resolved_stop_ids.get((update.trip_id, update.stop_sequence))
                    if update.stop_sequence is not None
                    else None
                )
                stop_id = _namespace(namespace, raw_stop_id)
                if stop_id not in stop_ids and update.stop_id not in stop_ids:
                    continue
                result.append(
                    {
                        "tripID": _namespace(namespace, update.trip_id),
                        "routeID": _namespace(namespace, update.route_id),
                        "directionID": update.direction_id,
                        "stopID": stop_id,
                        "stopSequence": update.stop_sequence,
                        "effectiveTime": _iso_time(update.effective_time),
                        "delaySeconds": update.delay_seconds,
                        "isCancelled": update.is_cancelled,
                        "tripUpdateTimestamp": _iso_time(snapshot.feed_timestamp),
                    }
                )
        result.sort(key=lambda item: (str(item["effectiveTime"] or ""), str(item["tripID"])))
        return result

    def _alerts_payload(self, snapshot: _Snapshot) -> list[dict[str, object]]:
        return [
            {
                "alertID": alert.alert_id,
                "cause": alert.cause,
                "effect": alert.effect,
                "title": {"pl": alert.header_text} if alert.header_text else {},
                "description": {"pl": alert.description_text} if alert.description_text else {},
                "url": {"pl": alert.url} if alert.url else {},
                "activePeriods": [
                    {"start": _iso_time(start), "end": _iso_time(end)}
                    for start, end in alert.active_periods
                ],
                "routeIDs": [],
                "stopIDs": [],
                "tripIDs": [],
                "providerSelectors": (
                    [{"namespace": self._namespace_for_source(source_id)}]
                    if self._namespace_for_source(source_id)
                    else []
                ),
                "informedEntityCount": alert.informed_entity_count,
            }
            for source_id, source_result in (
                (result.source_id, result)
                for result in snapshot.source_results
                if result.feed is not None
            )
            for alert in source_result.feed.alerts
        ]

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != self._path:
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        city_id = query.get("cityID", [None])[0]
        if city_id not in self._city_ids:
            return GatewayResponse(HTTPStatus.BAD_REQUEST, {"error": "unsupported cityID"})
        try:
            stop_ids = self._requested_stop_ids(query) if self._kind == "tripUpdates" else []
            snapshot, stale = self._snapshot_for_request()
        except GTFSRealtimeGatewayError as error:
            LOGGER.warning(
                "event=poland_realtime_unavailable provider=%s kind=%s reason=%s",
                self._provider_id,
                self._kind,
                error,
            )
            return GatewayResponse(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "schemaVersion": 1,
                    "providerID": self._provider_id,
                    "cityID": city_id,
                    "health": {"status": "unavailable", "maxStaleSeconds": self._max_stale},
                    "error": "realtime source unavailable",
                },
            )

        payload: dict[str, object] = {
            "schemaVersion": 1,
            "providerID": self._provider_id,
            "cityID": city_id,
            "feedTimestamp": _iso_time(snapshot.feed_timestamp),
            "retrievedAt": _iso_time(int(snapshot.retrieved_at)),
            "stale": stale,
            "entityCount": snapshot.entity_count,
            "health": self._health(snapshot, stale),
        }
        if self._kind == "tripUpdates":
            updates = self._trip_payload(snapshot, set(stop_ids))
            payload.update({
                "stopIDs": stop_ids,
                "routeCount": len({str(item["routeID"]) for item in updates if item["routeID"]}),
                "updates": updates,
            })
        elif self._kind == "vehiclePositions":
            vehicles, dropped = self._vehicle_payload(snapshot, str(city_id))
            payload.update({
                "vehicleCount": len(vehicles),
                "droppedVehicleCount": dropped,
                "vehicles": vehicles,
            })
        else:
            alerts = self._alerts_payload(snapshot)
            payload.update({"alertCount": len(alerts), "alerts": alerts})
        return GatewayResponse(HTTPStatus.OK, payload)


class GdyniaDelaysGateway:
    """Normalize the official stop-scoped Gdynia delay API as trip updates."""

    def __init__(
        self,
        *,
        provider_id: str,
        city_ids: set[str],
        source: Mapping[str, object],
        path: str,
        clock: Callable[[], float] = time.time,
        transport: GTFSRealtimeHTTPTransport | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._city_ids = frozenset(city_ids)
        self._source = dict(source)
        self._path = path
        self._clock = clock
        self._transport = transport or GTFSRealtimeHTTPTransport(
            f"HalteWecker-{provider_id}-delays/1.0"
        )

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != self._path:
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        city_id = query.get("cityID", [None])[0]
        if city_id not in self._city_ids:
            return GatewayResponse(HTTPStatus.BAD_REQUEST, {"error": "unsupported cityID"})
        values = query.get("stopIDs", []) + query.get("stopID", [])
        requested = list(dict.fromkeys(
            part.strip() for value in values for part in value.split(",") if part.strip()
        ))
        if not requested or len(requested) > MAX_REQUESTED_STOPS:
            return GatewayResponse(HTTPStatus.BAD_REQUEST, {"error": "invalid stop selection"})
        realtime = self._source.get("realtime")
        template = str(
            realtime.get("delaysURL", "") if isinstance(realtime, dict) else ""
        ).strip()
        namespace = str(self._source.get("namespace") or "")
        if not template:
            return GatewayResponse(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "realtime source unavailable"})
        updates: list[dict[str, object]] = []
        statuses: dict[str, int] = {}
        errors: dict[str, str] = {}
        for public_stop_id in requested:
            native_stop_id = public_stop_id.removeprefix(namespace)
            url = template.replace("{STOP_ID}", quote(native_stop_id, safe=""))
            try:
                response = self._transport.fetch_raw(
                    url,
                    accept="application/json, text/json",
                )
                statuses[public_stop_id] = response.status
                parsed = _parse_gdynia_delay_payload(
                    response.body,
                    namespace=namespace,
                    requested_stop_id=native_stop_id,
                )
                for update in parsed:
                    updates.append(
                        {
                            "tripID": _namespace(namespace, update.trip_id),
                            "routeID": _namespace(namespace, update.route_id),
                            "directionID": update.direction_id,
                            "stopID": _namespace(namespace, update.stop_id),
                            "stopSequence": update.stop_sequence,
                            "effectiveTime": _iso_time(update.effective_time),
                            "delaySeconds": update.delay_seconds,
                            "isCancelled": update.is_cancelled,
                            "tripUpdateTimestamp": _iso_time(int(self._clock())),
                        }
                    )
            except Exception as error:
                errors[public_stop_id] = str(error)
        if errors and not updates:
            return GatewayResponse(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "schemaVersion": 1,
                    "providerID": self._provider_id,
                    "cityID": city_id,
                    "health": {"status": "unavailable", "httpStatuses": statuses, "errors": errors},
                    "error": "realtime source unavailable",
                },
            )
        return GatewayResponse(
            HTTPStatus.OK,
            {
                "schemaVersion": 1,
                "providerID": self._provider_id,
                "cityID": city_id,
                "stopIDs": requested,
                "feedTimestamp": _iso_time(int(self._clock())),
                "retrievedAt": _iso_time(int(self._clock())),
                "stale": False,
                "entityCount": len(updates),
                "routeCount": len({str(item["routeID"]) for item in updates if item["routeID"]}),
                "health": {
                    "status": "degraded" if errors else "healthy",
                    "httpStatuses": statuses,
                    "errors": errors,
                    "tripUpdateCount": len(updates),
                    "maxStaleSeconds": POLAND_MAX_STALE_SECONDS,
                },
                "updates": updates,
            },
        )
