"""Server-side TransLink GTFS-Realtime TripUpdates gateway.

The upstream API key is read from the server environment and is never part of
the normalized response returned to clients.
"""

from __future__ import annotations

import gzip
import os
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TRANSLINK_TRIP_UPDATES_URL = "https://gtfsapi.translink.ca/v3/gtfsrealtime"
DEFAULT_CACHE_TTL_SECONDS = 15.0
DEFAULT_MAX_STALE_SECONDS = 180.0


@dataclass(frozen=True)
class GatewayResponse:
    status: int
    payload: dict[str, object]
    cache_control: str = "no-store, max-age=0"


@dataclass(frozen=True)
class _RealtimeUpdate:
    trip_id: str
    route_id: str
    direction_id: str | None
    stop_id: str
    stop_sequence: int | None
    effective_time: int | None
    delay_seconds: int | None
    is_cancelled: bool


@dataclass(frozen=True)
class _Snapshot:
    feed_timestamp: int | None
    retrieved_at: float
    entity_count: int
    route_count: int
    updates: tuple[_RealtimeUpdate, ...]


class TransLinkGatewayError(Exception):
    """An upstream or payload error safe to expose as a generic status."""


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
    raise TransLinkGatewayError("malformed protobuf")


def _iter_fields(data: bytes):
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        field_number, wire_type = key >> 3, key & 0x07
        if field_number == 0:
            raise TransLinkGatewayError("malformed protobuf")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise TransLinkGatewayError("malformed protobuf")
            value, offset = data[offset:end], end
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise TransLinkGatewayError("malformed protobuf")
            value, offset = data[offset:end], end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise TransLinkGatewayError("malformed protobuf")
            value, offset = data[offset:end], end
        else:
            raise TransLinkGatewayError("unsupported protobuf wire type")
        yield field_number, wire_type, value


def _text(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TransLinkGatewayError("invalid protobuf text") from error


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


def _parse_stop_time_update(data: bytes) -> tuple[str, int | None, int | None, int | None, bool]:
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


def _parse_trip_update(data: bytes) -> tuple[tuple[str, str, str | None, bool], int | None, list[tuple[str, int | None, int | None, int | None, bool]]]:
    trip = ("", "", None, False)
    timestamp: int | None = None
    stop_updates: list[tuple[str, int | None, int | None, int | None, bool]] = []
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            trip = _parse_trip_descriptor(value)
        elif field == 2 and wire_type == 2:
            stop_updates.append(_parse_stop_time_update(value))
        elif field == 4 and wire_type == 0:
            timestamp = int(value)
    return trip, timestamp, stop_updates


def parse_trip_updates(data: bytes) -> tuple[int | None, int, tuple[_RealtimeUpdate, ...]]:
    """Decode only the GTFS-RT fields needed by the normalized client API."""
    feed_timestamp: int | None = None
    entity_count = 0
    updates: dict[tuple[str, str, int | None], _RealtimeUpdate] = {}
    for field, wire_type, value in _iter_fields(data):
        if field == 1 and wire_type == 2:
            for header_field, header_wire_type, header_value in _iter_fields(value):
                if header_field == 4 and header_wire_type == 0:
                    feed_timestamp = int(header_value)
        elif field == 2 and wire_type == 2:
            entity_count += 1
            for entity_field, entity_wire_type, entity_value in _iter_fields(value):
                if entity_field != 3 or entity_wire_type != 2:
                    continue
                trip, _trip_timestamp, stop_updates = _parse_trip_update(entity_value)
                trip_id, route_id, direction_id, is_cancelled = trip
                if not trip_id:
                    continue
                for stop_id, stop_sequence, event_time, delay_seconds, skipped in stop_updates:
                    if not stop_id or skipped:
                        continue
                    update = _RealtimeUpdate(
                        trip_id=trip_id,
                        route_id=route_id,
                        direction_id=direction_id,
                        stop_id=stop_id,
                        stop_sequence=stop_sequence,
                        effective_time=event_time,
                        delay_seconds=delay_seconds,
                        is_cancelled=is_cancelled,
                    )
                    updates[(trip_id, stop_id, stop_sequence)] = update
    return feed_timestamp, entity_count, tuple(updates.values())


class _HTTPTransport:
    def __call__(self, url: str) -> bytes:
        request = Request(
            url,
            headers={
                "Accept": "application/x-protobuf, application/protobuf",
                "Accept-Encoding": "gzip",
                "User-Agent": "HalteWecker-TransLink-GTFSRT/1.0",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                body = response.read()
                encoding = response.headers.get("Content-Encoding", "")
                if encoding.lower() == "gzip":
                    body = gzip.decompress(body)
                return body
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise TransLinkGatewayError("upstream unavailable") from error


class TransLinkProxy:
    def __init__(
        self,
        api_key: str,
        transport=None,
        clock=time.time,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        max_stale: float = DEFAULT_MAX_STALE_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._transport = transport or _HTTPTransport()
        self._clock = clock
        self._cache_ttl = cache_ttl
        self._max_stale = max_stale
        self._snapshot: _Snapshot | None = None
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "TransLinkProxy | None":
        api_key = os.environ.get("TRANSLINK_API_KEY", "").strip()
        return cls(api_key) if api_key else None

    @staticmethod
    def _requested_stop_ids(query: dict[str, list[str]]) -> list[str]:
        values = query.get("stopIDs", []) + query.get("stopID", [])
        stop_ids: list[str] = []
        for value in values:
            stop_ids.extend(part.strip() for part in value.split(","))
        unique = list(dict.fromkeys(stop_id for stop_id in stop_ids if stop_id))
        if not unique or len(unique) > 64:
            raise TransLinkGatewayError("invalid stop selection")
        return unique

    def _fetch_snapshot(self) -> _Snapshot:
        upstream_url = f"{TRANSLINK_TRIP_UPDATES_URL}?{urlencode({'apikey': self._api_key})}"
        try:
            body = self._transport(upstream_url)
            feed_timestamp, entity_count, updates = parse_trip_updates(body)
        except TransLinkGatewayError:
            raise
        except Exception as error:
            raise TransLinkGatewayError("realtime source invalid") from error
        retrieved_at = self._clock()
        return _Snapshot(
            feed_timestamp=feed_timestamp,
            retrieved_at=retrieved_at,
            entity_count=entity_count,
            route_count=len({update.route_id for update in updates if update.route_id}),
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
            except TransLinkGatewayError:
                if cached is not None and now - cached.retrieved_at <= self._max_stale:
                    return cached, True
                raise
            with self._lock:
                self._snapshot = fresh
            return fresh, False

    @staticmethod
    def _iso_time(timestamp: int | None) -> str | None:
        if timestamp is None:
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))

    def handle(self, path: str, query: dict[str, list[str]]) -> GatewayResponse:
        if path != "/translink/realtime/trip-updates":
            return GatewayResponse(HTTPStatus.NOT_FOUND, {"error": "not found"})
        if query.get("cityID", [None])[0] != "vancouver":
            return GatewayResponse(HTTPStatus.BAD_REQUEST, {"error": "unsupported cityID"})
        try:
            requested_stop_ids = self._requested_stop_ids(query)
            snapshot, stale = self._snapshot_for_request()
        except TransLinkGatewayError as error:
            message = str(error)
            status = HTTPStatus.BAD_REQUEST if message == "invalid stop selection" else HTTPStatus.SERVICE_UNAVAILABLE
            return GatewayResponse(status, {"error": "realtime source unavailable" if status != HTTPStatus.BAD_REQUEST else message})

        requested = set(requested_stop_ids)
        updates = [
            {
                "tripID": update.trip_id,
                "routeID": update.route_id,
                "directionID": update.direction_id,
                "stopID": update.stop_id,
                "stopSequence": update.stop_sequence,
                "effectiveTime": self._iso_time(update.effective_time),
                "delaySeconds": update.delay_seconds,
                "isCancelled": update.is_cancelled,
            }
            for update in snapshot.updates
            if update.stop_id in requested
        ]
        updates.sort(key=lambda item: (item["effectiveTime"] or "", item["routeID"] or "", item["tripID"]))
        payload = {
            "schemaVersion": 1,
            "providerID": "translink-vancouver",
            "cityID": "vancouver",
            "stopIDs": requested_stop_ids,
            "feedTimestamp": self._iso_time(snapshot.feed_timestamp),
            "retrievedAt": self._iso_time(int(snapshot.retrieved_at)),
            "stale": stale,
            "entityCount": snapshot.entity_count,
            "routeCount": snapshot.route_count,
            "updates": updates,
        }
        return GatewayResponse(HTTPStatus.OK, payload)
