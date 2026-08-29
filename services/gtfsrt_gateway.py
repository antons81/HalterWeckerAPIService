"""Shared GTFS-Realtime TripUpdates parsing, caching, and response shaping."""

from __future__ import annotations

import base64
import binascii
import gzip
import math
import struct
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Callable


DEFAULT_CACHE_TTL_SECONDS = 15.0
DEFAULT_MAX_STALE_SECONDS = 120.0


@dataclass(frozen=True)
class GatewayResponse:
    status: int
    payload: dict[str, object]
    cache_control: str = "no-store, max-age=0"


@dataclass(frozen=True)
class RealtimeUpdate:
    trip_id: str
    route_id: str
    direction_id: str | None
    stop_id: str
    stop_sequence: int | None
    effective_time: int | None
    delay_seconds: int | None
    is_cancelled: bool


@dataclass(frozen=True)
class RealtimeVehiclePosition:
    vehicle_id: str
    trip_id: str | None
    route_id: str | None
    direction_id: str | None
    stop_id: str | None
    stop_sequence: int | None
    latitude: float
    longitude: float
    bearing: float | None
    speed: float | None
    timestamp: int | None


@dataclass(frozen=True)
class RealtimeAlert:
    alert_id: str
    cause: int | None
    effect: int | None
    header_text: str | None
    description_text: str | None
    url: str | None
    active_periods: tuple[tuple[int | None, int | None], ...]
    informed_entity_count: int


@dataclass(frozen=True)
class GTFSRealtimeFeed:
    feed_timestamp: int | None
    entity_count: int
    trip_updates: tuple[RealtimeUpdate, ...]
    vehicle_positions: tuple[RealtimeVehiclePosition, ...]
    alerts: tuple[RealtimeAlert, ...]


@dataclass(frozen=True)
class _Snapshot:
    feed_timestamp: int | None
    retrieved_at: float
    entity_count: int
    updates: tuple[RealtimeUpdate, ...]


class GTFSRealtimeGatewayError(Exception):
    """An upstream or payload error safe to expose as a generic status."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def infer_identifier_prefix(values: object) -> str:
    """Infer one unambiguous static namespace from provider-owned IDs.

    This is only a compatibility path for releases whose ownership metadata
    predates the identifier-prefix column. Ambiguous or mixed IDs return an
    empty prefix so strict realtime joins continue to fail closed.
    """
    try:
        identifiers = [str(value) for value in values if str(value)]
    except TypeError:
        return ""
    if not identifiers or any(":" not in value for value in identifiers):
        return ""
    prefixes = {value[: value.index(":") + 1] for value in identifiers}
    return next(iter(prefixes)) if len(prefixes) == 1 else ""


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, offset
        shift += 7
    raise GTFSRealtimeGatewayError("malformed protobuf")


def _iter_fields(data: bytes):
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        field_number, wire_type = key >> 3, key & 0x07
        if field_number == 0:
            raise GTFSRealtimeGatewayError("malformed protobuf")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise GTFSRealtimeGatewayError("malformed protobuf")
            value, offset = data[offset:end], end
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise GTFSRealtimeGatewayError("malformed protobuf")
            value, offset = data[offset:end], end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise GTFSRealtimeGatewayError("malformed protobuf")
            value, offset = data[offset:end], end
        else:
            raise GTFSRealtimeGatewayError("unsupported protobuf wire type")
        yield field_number, wire_type, value


def _text(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GTFSRealtimeGatewayError("invalid protobuf text") from error


def _float32(value: bytes) -> float:
    if len(value) != 4:
        raise GTFSRealtimeGatewayError("malformed protobuf")
    return float(struct.unpack("<f", value)[0])


def _parse_vehicle_descriptor(data: bytes) -> str:
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            return _text(value)
    return ""


def _parse_position(
    data: bytes,
) -> tuple[float | None, float | None, float | None, float | None]:
    latitude = longitude = bearing = speed = None
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 5:
            latitude = _float32(value)
        elif field == 2 and wire_type == 5:
            longitude = _float32(value)
        elif field == 3 and wire_type == 5:
            bearing = _float32(value)
        elif field == 5 and wire_type == 5:
            speed = _float32(value)
    return latitude, longitude, bearing, speed


def _parse_vehicle_position(
    data: bytes,
    fallback_vehicle_id: str,
) -> RealtimeVehiclePosition | None:
    trip_id = route_id = ""
    direction_id = stop_id = None
    vehicle_id = fallback_vehicle_id
    stop_sequence = timestamp = None
    latitude = longitude = bearing = speed = None

    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            trip_id, route_id, direction_id, _ = _parse_trip_descriptor(value)
        elif field == 2 and wire_type == 2:
            latitude, longitude, bearing, speed = _parse_position(value)
        elif field == 3 and wire_type == 0:
            stop_sequence = int(value)
        elif field == 5 and wire_type == 0:
            timestamp = int(value)
        elif field == 7 and wire_type == 2:
            stop_id = _text(value)
        elif field == 8 and wire_type == 2:
            candidate = _parse_vehicle_descriptor(value)
            if candidate:
                vehicle_id = candidate

    if not vehicle_id or latitude is None or longitude is None:
        return None
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return None
    if bearing is not None and not math.isfinite(bearing):
        bearing = None
    if speed is not None and (not math.isfinite(speed) or speed <= 0):
        speed = None
    return RealtimeVehiclePosition(
        vehicle_id=vehicle_id,
        trip_id=trip_id or None,
        route_id=route_id or None,
        direction_id=direction_id,
        stop_id=stop_id,
        stop_sequence=stop_sequence,
        latitude=latitude,
        longitude=longitude,
        bearing=bearing % 360.0 if bearing is not None else None,
        speed=speed,
        timestamp=timestamp,
    )


def _translated_text(data: bytes) -> str | None:
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            for translation_field, translation_wire_type, translation_value in _iter_fields(value):
                if translation_field == 1 and translation_wire_type == 2:
                    return _text(translation_value)
            try:
                return _text(value)
            except GTFSRealtimeGatewayError:
                continue
    return None


def _parse_alert(data: bytes, fallback_alert_id: str) -> RealtimeAlert:
    cause = effect = None
    header_text = description_text = url = None
    active_periods: list[tuple[int | None, int | None]] = []
    informed_entity_count = 0
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            start = end = None
            for period_field, period_wire_type, period_value in _iter_fields(value):
                if period_field in (1, 2) and period_wire_type == 0:
                    if period_field == 1:
                        start = int(period_value)
                    else:
                        end = int(period_value)
            active_periods.append((start, end))
        elif field == 5 and wire_type == 2:
            informed_entity_count += 1
        elif field == 6 and wire_type == 0:
            cause = int(value)
        elif field == 7 and wire_type == 0:
            effect = int(value)
        elif field == 8 and wire_type == 2:
            url = _translated_text(value)
        elif field == 10 and wire_type == 2:
            header_text = _translated_text(value)
        elif field == 11 and wire_type == 2:
            description_text = _translated_text(value)
    return RealtimeAlert(
        alert_id=fallback_alert_id,
        cause=cause,
        effect=effect,
        header_text=header_text,
        description_text=description_text,
        url=url,
        active_periods=tuple(active_periods),
        informed_entity_count=informed_entity_count,
    )


def _validate_gtfs_realtime_payload(data: bytes) -> None:
    """Validate the GTFS-RT envelope without accepting arbitrary JSON/bytes."""
    if not data:
        raise GTFSRealtimeGatewayError("empty realtime payload")
    has_feed_header_or_entity = False
    for field, wire_type, _value in _iter_fields(data):
        if field in (1, 2) and wire_type == 2:
            has_feed_header_or_entity = True
    if not has_feed_header_or_entity:
        raise GTFSRealtimeGatewayError("invalid GTFS-Realtime protobuf")


def decode_gtfs_realtime_body(body: bytes, *, content_type: str = "") -> bytes:
    """Return raw GTFS-RT protobuf from a raw or strictly Base64-wrapped body."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in {"application/json", "text/json", "text/plain"}:
        try:
            encoded = body.strip().decode("ascii")
            decoded = base64.b64decode(encoded, validate=True)
        except (UnicodeDecodeError, binascii.Error, ValueError) as error:
            raise GTFSRealtimeGatewayError("invalid Base64 realtime payload") from error
        _validate_gtfs_realtime_payload(decoded)
        return decoded

    _validate_gtfs_realtime_payload(body)
    return body


def _signed_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _parse_trip_descriptor(data: bytes) -> tuple[str, str, str | None, bool]:
    trip_id = ""
    route_id = ""
    direction_id: str | None = None
    schedule_relationship = 0
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            trip_id = _text(value)
        elif field == 4 and wire_type == 0:
            schedule_relationship = int(value)
        elif field == 5 and wire_type == 2:
            route_id = _text(value)
        elif field == 6 and wire_type == 2:
            direction_id = _text(value)
    return trip_id, route_id, direction_id, schedule_relationship == 3


def _parse_stop_time_event(data: bytes) -> tuple[int | None, int | None]:
    delay_seconds: int | None = None
    event_time: int | None = None
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 0:
            delay_seconds = _signed_int32(int(value))
        elif field == 2 and wire_type == 0:
            event_time = int(value)
    return event_time, delay_seconds


def _parse_stop_time_update(
    data: bytes,
) -> tuple[str, int | None, int | None, int | None, bool]:
    stop_id = ""
    stop_sequence: int | None = None
    event_time: int | None = None
    delay_seconds: int | None = None
    skipped = False
    for field, wire_type, value in _iter_fields(data):
        if field == 3 and wire_type == 0:
            stop_sequence = int(value)
        elif field in (1, 2) and wire_type == 2:
            candidate_time, candidate_delay = _parse_stop_time_event(value)
            if candidate_time is not None and (event_time is None or field == 2):
                event_time = candidate_time
            if candidate_delay is not None and (delay_seconds is None or field == 2):
                delay_seconds = candidate_delay
        elif field == 4 and wire_type == 2:
            stop_id = _text(value)
        elif field == 5 and wire_type == 0:
            skipped = int(value) == 1
    return stop_id, stop_sequence, event_time, delay_seconds, skipped


def _parse_trip_update(
    data: bytes,
) -> tuple[
    tuple[str, str, str | None, bool],
    list[tuple[str, int | None, int | None, int | None, bool]],
]:
    trip = ("", "", None, False)
    stop_updates: list[tuple[str, int | None, int | None, int | None, bool]] = []
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            trip = _parse_trip_descriptor(value)
        elif field == 2 and wire_type == 2:
            stop_updates.append(_parse_stop_time_update(value))
    return trip, stop_updates


def _feed_timestamp(data: bytes) -> int | None:
    feed_timestamp: int | None = None
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            for header_field, header_wire_type, header_value in _iter_fields(value):
                if header_field in (3, 4) and header_wire_type == 0:
                    feed_timestamp = int(header_value)
    return feed_timestamp


def parse_gtfs_realtime_feed(
    data: bytes,
    *,
    include_missing_stop_ids: bool = False,
) -> GTFSRealtimeFeed:
    """Decode TripUpdates, VehiclePositions, and ServiceAlerts from one feed."""
    feed_timestamp = _feed_timestamp(data)
    entity_count = 0
    updates: dict[tuple[str, str, int | None], RealtimeUpdate] = {}
    vehicles: dict[str, RealtimeVehiclePosition] = {}
    alerts: dict[str, RealtimeAlert] = {}
    for field, wire_type, value in _iter_fields(data):
        if field != 2 or wire_type != 2:
            continue
        entity_count += 1
        entity_id = ""
        trip_update_payload = vehicle_payload = alert_payload = None
        for entity_field, entity_wire_type, entity_value in _iter_fields(value):
            if entity_field == 1 and entity_wire_type == 2:
                entity_id = _text(entity_value)
            elif entity_field == 3 and entity_wire_type == 2:
                trip_update_payload = entity_value
            elif entity_field == 4 and entity_wire_type == 2:
                vehicle_payload = entity_value
            elif entity_field == 5 and entity_wire_type == 2:
                alert_payload = entity_value

        if trip_update_payload is not None:
            trip, stop_updates = _parse_trip_update(trip_update_payload)
            trip_id, route_id, direction_id, is_cancelled = trip
            if trip_id:
                for stop_id, stop_sequence, event_time, delay_seconds, skipped in stop_updates:
                    if skipped or (not stop_id and not include_missing_stop_ids):
                        continue
                    updates[(trip_id, stop_id, stop_sequence)] = RealtimeUpdate(
                        trip_id=trip_id,
                        route_id=route_id,
                        direction_id=direction_id,
                        stop_id=stop_id,
                        stop_sequence=stop_sequence,
                        effective_time=event_time,
                        delay_seconds=delay_seconds,
                        is_cancelled=is_cancelled,
                    )
        if vehicle_payload is not None:
            vehicle = _parse_vehicle_position(vehicle_payload, entity_id)
            if vehicle is not None:
                vehicles[vehicle.vehicle_id] = vehicle
        if alert_payload is not None:
            alert = _parse_alert(alert_payload, entity_id)
            alerts[alert.alert_id] = alert
    return GTFSRealtimeFeed(
        feed_timestamp=feed_timestamp,
        entity_count=entity_count,
        trip_updates=tuple(updates.values()),
        vehicle_positions=tuple(vehicles.values()),
        alerts=tuple(alerts.values()),
    )


def parse_trip_updates(data: bytes) -> tuple[int | None, int, tuple[RealtimeUpdate, ...]]:
    """Decode TripUpdates while preserving the existing gateway contract."""
    feed = parse_gtfs_realtime_feed(data)
    return feed.feed_timestamp, feed.entity_count, feed.trip_updates


def parse_vehicle_positions(
    data: bytes,
) -> tuple[int | None, int, tuple[RealtimeVehiclePosition, ...]]:
    """Decode VehiclePositions while preserving all-feed entity diagnostics."""
    feed = parse_gtfs_realtime_feed(data)
    return feed.feed_timestamp, feed.entity_count, feed.vehicle_positions


@dataclass(frozen=True)
class GTFSRealtimeHTTPResponse:
    status: int
    content_type: str
    body: bytes


class GTFSRealtimeHTTPTransport:
    """Shared public GTFS-RT HTTP transport with optional response metadata."""

    def __init__(
        self,
        user_agent: str,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.user_agent = user_agent
        self.headers = dict(headers or {})
        self.timeout = timeout

    def fetch_raw(
        self,
        url: str,
        *,
        accept: str = "application/x-protobuf, application/protobuf",
    ) -> GTFSRealtimeHTTPResponse:
        request = Request(
            url,
            headers={
                "Accept": accept,
                "Accept-Encoding": "gzip",
                "User-Agent": self.user_agent,
                **self.headers,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
                return GTFSRealtimeHTTPResponse(
                    status=int(response.status),
                    content_type=response.headers.get("Content-Type", ""),
                    body=body,
                )
        except HTTPError as error:
            raise GTFSRealtimeGatewayError(
                f"upstream HTTP status {error.code}",
                status_code=error.code,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise GTFSRealtimeGatewayError("upstream unavailable") from error

    def fetch(self, url: str) -> GTFSRealtimeHTTPResponse:
        response = self.fetch_raw(url)
        try:
            body = decode_gtfs_realtime_body(
                response.body,
                content_type=response.content_type,
            )
        except GTFSRealtimeGatewayError:
            raise
        return GTFSRealtimeHTTPResponse(
            status=response.status,
            content_type=response.content_type,
            body=body,
        )

    def __call__(self, url: str) -> bytes:
        return self.fetch(url).body


_HTTPTransport = GTFSRealtimeHTTPTransport


class GTFSRealtimeGateway:
    def __init__(
        self,
        *,
        provider_id: str,
        city_id: str,
        city_ids: set[str] | None = None,
        path: str,
        upstream_url: str | Callable[[], str],
        namespace: str = "",
        transport: Callable[[str], bytes] | None = None,
        clock: Callable[[], float] = time.time,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        max_stale: float = DEFAULT_MAX_STALE_SECONDS,
        valid_trip_registry: Callable[[], tuple[set[str], dict[str, str]]] | None = None,
        valid_stop_registry: Callable[[], set[str]] | None = None,
        stop_id_mapper: Callable[[str], str] | None = None,
        user_agent: str = "HalteWecker-GTFSRT/1.0",
    ) -> None:
        self._provider_id = provider_id
        self._city_id = city_id
        self._city_ids = frozenset(city_ids or {city_id})
        self._path = path
        self._upstream_url = upstream_url
        self._namespace = namespace
        self._transport = transport or _HTTPTransport(user_agent)
        self._clock = clock
        self._cache_ttl = cache_ttl
        self._max_stale = max_stale
        self._valid_trip_registry = valid_trip_registry
        self._valid_stop_registry = valid_stop_registry
        self._stop_id_mapper = stop_id_mapper or (lambda stop_id: stop_id)
        self._snapshot: _Snapshot | None = None
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    @staticmethod
    def _requested_stop_ids(query: dict[str, list[str]]) -> list[str]:
        values = query.get("stopIDs", []) + query.get("stopID", [])
        stop_ids: list[str] = []
        for value in values:
            stop_ids.extend(part.strip() for part in value.split(","))
        unique = list(dict.fromkeys(stop_id for stop_id in stop_ids if stop_id))
        if not unique or len(unique) > 64:
            raise GTFSRealtimeGatewayError("invalid stop selection")
        return unique

    def _fetch_snapshot(self) -> _Snapshot:
        url = self._upstream_url() if callable(self._upstream_url) else self._upstream_url
        try:
            body = self._transport(url)
            feed_timestamp, entity_count, updates = parse_trip_updates(body)
        except GTFSRealtimeGatewayError:
            raise
        except Exception as error:
            raise GTFSRealtimeGatewayError("realtime source invalid") from error
        return _Snapshot(
            feed_timestamp=feed_timestamp,
            retrieved_at=self._clock(),
            entity_count=entity_count,
            updates=updates,
        )

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

    def _published_id(self, raw_id: str) -> str:
        return f"{self._namespace}{raw_id}" if self._namespace else raw_id

    @staticmethod
    def _iso_time(timestamp: int | None) -> str | None:
        if timestamp is None:
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))

    def _filter_updates(
        self,
        updates: tuple[RealtimeUpdate, ...],
        requested_stop_ids: set[str],
    ) -> list[dict[str, object]]:
        valid_trip_ids: set[str] | None = None
        route_by_trip_id: dict[str, str] = {}
        valid_stop_ids: set[str] | None = None
        if self._valid_trip_registry is not None:
            valid_trip_ids, route_by_trip_id = self._valid_trip_registry()
        if self._valid_stop_registry is not None:
            valid_stop_ids = self._valid_stop_registry()

        result: list[dict[str, object]] = []
        for update in updates:
            trip_id = self._published_id(update.trip_id)
            route_id = self._published_id(update.route_id) if update.route_id else ""
            stop_id = self._published_id(update.stop_id)
            if stop_id not in requested_stop_ids:
                continue
            if valid_stop_ids is not None and self._stop_id_mapper(update.stop_id) not in valid_stop_ids:
                continue
            if valid_trip_ids is not None:
                if trip_id not in valid_trip_ids:
                    continue
                expected_route_id = route_by_trip_id.get(trip_id)
                if expected_route_id:
                    if route_id and route_id != expected_route_id:
                        continue
                    route_id = expected_route_id
            result.append({
                "tripID": trip_id,
                "routeID": route_id,
                "directionID": update.direction_id,
                "stopID": stop_id,
                "stopSequence": update.stop_sequence,
                "effectiveTime": self._iso_time(update.effective_time),
                "delaySeconds": update.delay_seconds,
                "isCancelled": update.is_cancelled,
            })
        result.sort(key=lambda item: (
            str(item["effectiveTime"] or ""),
            str(item["routeID"] or ""),
            str(item["tripID"]),
        ))
        return result

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != self._path:
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        requested_city_id = query.get("cityID", [None])[0]
        if requested_city_id not in self._city_ids:
            return GatewayResponse(HTTPStatus.BAD_REQUEST, {"error": "unsupported cityID"})
        try:
            requested_stop_ids = self._requested_stop_ids(query)
            snapshot, stale = self._snapshot_for_request()
        except GTFSRealtimeGatewayError as error:
            message = str(error)
            status = (
                HTTPStatus.BAD_REQUEST
                if message == "invalid stop selection"
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            return GatewayResponse(
                status,
                {"error": "realtime source unavailable" if status != HTTPStatus.BAD_REQUEST else message},
            )

        updates = self._filter_updates(snapshot.updates, set(requested_stop_ids))
        payload = {
            "schemaVersion": 1,
            "providerID": self._provider_id,
            "cityID": requested_city_id,
            "stopIDs": requested_stop_ids,
            "feedTimestamp": self._iso_time(snapshot.feed_timestamp),
            "retrievedAt": self._iso_time(int(snapshot.retrieved_at)),
            "stale": stale,
            "entityCount": snapshot.entity_count,
            "routeCount": len({str(update["routeID"]) for update in updates if update["routeID"]}),
            "updates": updates,
        }
        return GatewayResponse(HTTPStatus.OK, payload)
