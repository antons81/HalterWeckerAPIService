#!/usr/bin/env python3
"""Expose estimated VVO vehicle positions using the VVO EFA API.

Upstream:
    https://efa.vvo-online.de/VMSSL3/

The VVO EFA API provides stop search, departures, realtime timestamps, delays,
platforms and line metadata. It does not provide vehicle GPS coordinates.

This gateway therefore exposes conservative schedule estimates:
1. Poll XML_DM_REQUEST for a configurable set of VVO stops.
2. Read scheduled and realtime departure timestamps.
3. Place each active departure at its stop coordinate from VVO_STOPS.JSON.
4. Mark every position as "scheduleEstimate".

The response shape remains compatible with the HalteWecker transit-radar backend:
    GET /health
    GET /vvo/vehicles.json
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from zoneinfo import ZoneInfo


VVO_EFA_BASE_URL = "https://efa.vvo-online.de/VMSSL3/"
VVO_STOPS_CATALOG_URL = "https://www.vvo-online.de/open_data/VVO_STOPS.JSON"
VVO_TIMEZONE = ZoneInfo("Europe/Berlin")

DEFAULT_MAJOR_STOPS = (
    "33000028",  # Dresden Hauptbahnhof
    "33000029",  # Dresden-Neustadt
    "33000001",  # Dresden Postplatz
    "33000005",  # Dresden Wiener Platz / central area, verify against catalog
)

_stops_catalog: dict[str, dict[str, Any]] = {}
_stops_catalog_lock = threading.Lock()
_stops_catalog_last_fetch = 0.0
_stops_catalog_ttl = 3600.0


@dataclass(frozen=True)
class VVOGatewayConfiguration:
    base_url: str = VVO_EFA_BASE_URL
    host: str = "0.0.0.0"
    port: int = 8082
    snapshot_ttl_seconds: float = 15.0
    departures_per_stop: int = 30
    active_window_before_minutes: int = 3
    active_window_after_minutes: int = 20
    stop_ids: tuple[str, ...] = DEFAULT_MAJOR_STOPS

    @classmethod
    def from_environment(cls) -> "VVOGatewayConfiguration":
        raw_stop_ids = os.environ.get("VVO_STOP_IDS", "")
        stop_ids = tuple(
            item.strip()
            for item in raw_stop_ids.split(",")
            if item.strip()
        ) or DEFAULT_MAJOR_STOPS

        return cls(
            base_url=os.environ.get(
                "VVO_BASE_URL",
                VVO_EFA_BASE_URL,
            ).rstrip("/") + "/",
            host=os.environ.get("VVO_GATEWAY_HOST", "0.0.0.0"),
            port=int(os.environ.get("VVO_GATEWAY_PORT", "8082")),
            snapshot_ttl_seconds=float(
                os.environ.get("VVO_SNAPSHOT_TTL_SECONDS", "15")
            ),
            departures_per_stop=int(
                os.environ.get("VVO_DEPARTURES_PER_STOP", "30")
            ),
            active_window_before_minutes=int(
                os.environ.get("VVO_ACTIVE_WINDOW_BEFORE_MINUTES", "3")
            ),
            active_window_after_minutes=int(
                os.environ.get("VVO_ACTIVE_WINDOW_AFTER_MINUTES", "20")
            ),
            stop_ids=stop_ids,
        )


class VVOEFAClient:
    """Small client for VVO's EFA RapidJSON departure monitor."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def fetch_departures(
        self,
        stop_id: str,
        *,
        limit: int,
        now: datetime,
    ) -> dict[str, Any]:
        local_now = now.astimezone(VVO_TIMEZONE)
        params = {
            "outputFormat": "RapidJSON",
            "stateless": "1",
            "type_dm": "any",
            "name_dm": stop_id,
            "mode": "direct",
            "useRealtime": "1",
            "limit": str(limit),
            "language": "de",
            "coordOutputFormat": "WGS84",
            "itdDateYear": str(local_now.year),
            "itdDateMonth": str(local_now.month),
            "itdDateDay": str(local_now.day),
            "itdTimeHour": str(local_now.hour),
            "itdTimeMinute": str(local_now.minute),
        }
        endpoint = urllib.parse.urljoin(
            self.base_url,
            "XML_DM_REQUEST",
        )
        url = f"{endpoint}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "User-Agent": "HalteWeckerVVOGateway/2.0",
            },
        )

        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8-sig"))


def _fetch_stops_catalog() -> dict[str, dict[str, Any]]:
    global _stops_catalog, _stops_catalog_last_fetch

    with _stops_catalog_lock:
        now = time.time()
        if (
            _stops_catalog
            and now - _stops_catalog_last_fetch < _stops_catalog_ttl
        ):
            return _stops_catalog

        request = urllib.request.Request(
            VVO_STOPS_CATALOG_URL,
            headers={"User-Agent": "HalteWeckerVVOGateway/2.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)

            catalog: dict[str, dict[str, Any]] = {}
            if not isinstance(data, list):
                raise ValueError("VVO stop catalog is not a JSON array")

            for stop in data:
                if not isinstance(stop, dict):
                    continue
                stop_id = str(
                    stop.get("id")
                    or stop.get("Id")
                    or stop.get("stopID")
                    or ""
                ).strip()
                if stop_id:
                    catalog[stop_id] = stop

            if not catalog:
                raise ValueError("VVO stop catalog is empty")

            _stops_catalog = catalog
            _stops_catalog_last_fetch = now
            return catalog
        except Exception as error:
            print(f"[VVO] Stop catalog fetch failed: {error}")
            if _stops_catalog:
                return _stops_catalog
            raise


def _float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_stop_coordinates(stop_id: str) -> tuple[float, float] | None:
    stop = _fetch_stops_catalog().get(str(stop_id))
    if not stop:
        return None

    latitude = _float_value(
        stop.get("y")
        or stop.get("latitude")
        or stop.get("lat")
    )
    longitude = _float_value(
        stop.get("x")
        or stop.get("longitude")
        or stop.get("lon")
        or stop.get("lng")
    )
    if (
        latitude is None
        or longitude is None
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return None
    return latitude, longitude


def _dictionary(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def parse_efa_datetime(value: Any) -> datetime | None:
    """Parse EFA dateTime/realDateTime dictionaries in Europe/Berlin."""
    data = _dictionary(value)
    if not data:
        return None

    try:
        year = int(data["year"])
        month = int(data["month"])
        day = int(data["day"])
        hour = int(data.get("hour", 0))
        minute = int(data.get("minute", 0))
        second = int(data.get("second", 0))
        local = datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=VVO_TIMEZONE,
        )
        return local.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None


def _departure_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract departures from common EFA RapidJSON response variants."""
    candidates = (
        payload.get("departureList"),
        _dictionary(payload.get("stopEvents")).get("departure"),
        _dictionary(payload.get("dm")).get("departureList"),
    )
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _transport_mode(serving_line: dict[str, Any]) -> str:
    raw_class = (
        serving_line.get("motType")
        or serving_line.get("class")
        or serving_line.get("productClass")
    )
    try:
        transport_class = int(raw_class)
    except (TypeError, ValueError):
        transport_class = None

    if transport_class in {0, 1, 13, 15, 16}:
        return "regionalTrain"
    if transport_class in {2, 3}:
        return "subway"
    if transport_class == 4:
        return "tram"
    if transport_class in {5, 6, 7, 8, 11}:
        return "bus"
    if transport_class == 9:
        return "ferry"

    name = _string(
        serving_line.get("name"),
        serving_line.get("number"),
        serving_line.get("symbol"),
    ).lower()
    if name.startswith("s"):
        return "suburbanTrain"
    if name.startswith(("re", "rb", "ic", "ice", "ec")):
        return "regionalTrain"
    return "unknown"


def _delay_seconds(
    scheduled: datetime | None,
    realtime: datetime | None,
) -> int | None:
    if scheduled is None or realtime is None:
        return None
    delay = int((realtime - scheduled).total_seconds())
    return delay if delay != 0 else None


def compute_vehicle(
    departure: dict[str, Any],
    *,
    requested_stop_id: str,
    now: datetime,
    before_window: timedelta,
    after_window: timedelta,
) -> dict[str, Any] | None:
    serving_line = _dictionary(departure.get("servingLine"))
    scheduled = parse_efa_datetime(
        departure.get("dateTime")
        or departure.get("scheduledDateTime")
    )
    realtime = parse_efa_datetime(
        departure.get("realDateTime")
        or departure.get("realtimeDateTime")
    )
    effective = realtime or scheduled
    if effective is None:
        return None

    if effective < now - before_window or effective > now + after_window:
        return None

    stop_id = _string(
        departure.get("stopID"),
        departure.get("stopId"),
        _dictionary(departure.get("location")).get("id"),
        requested_stop_id,
    )
    coordinates = get_stop_coordinates(stop_id)
    if coordinates is None and stop_id != requested_stop_id:
        coordinates = get_stop_coordinates(requested_stop_id)
    if coordinates is None:
        return None

    line_name = _string(
        serving_line.get("number"),
        serving_line.get("name"),
        serving_line.get("symbol"),
        departure.get("lineName"),
    )
    if not line_name:
        return None

    direction = _string(
        serving_line.get("direction"),
        serving_line.get("directionFrom"),
        departure.get("direction"),
    )
    trip_id = _string(
        serving_line.get("stateless"),
        serving_line.get("id"),
        departure.get("tripID"),
        departure.get("tripId"),
        departure.get("id"),
    )
    if not trip_id:
        timestamp = int(effective.timestamp())
        trip_id = f"{stop_id}-{line_name}-{timestamp}"

    cancelled = bool(
        departure.get("cancelled")
        or departure.get("isCancelled")
        or _string(departure.get("state")).lower()
        in {"cancelled", "canceled", "abgesagt"}
    )
    if cancelled:
        return None

    latitude, longitude = coordinates
    return {
        "id": f"vvo-{trip_id}",
        "tripID": trip_id,
        "lineName": line_name,
        "directionName": direction,
        "latitude": latitude,
        "longitude": longitude,
        "delaySeconds": _delay_seconds(scheduled, realtime),
        "mode": _transport_mode(serving_line),
    }


class VVOVehicleService:
    def __init__(
        self,
        configuration: VVOGatewayConfiguration,
        client: VVOEFAClient,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ):
        self.configuration = configuration
        self.client = client
        self.monotonic_clock = monotonic_clock
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_expires_at = 0.0

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            monotonic_now = self.monotonic_clock()
            if (
                self._snapshot is not None
                and monotonic_now < self._snapshot_expires_at
            ):
                return self._snapshot

            timestamp = now or datetime.now(timezone.utc)
            snapshot = self._refresh(timestamp)
            self._snapshot = snapshot
            self._snapshot_expires_at = (
                monotonic_now + self.configuration.snapshot_ttl_seconds
            )
            return snapshot

    def _refresh(self, now: datetime) -> dict[str, Any]:
        vehicles_by_id: dict[str, dict[str, Any]] = {}
        dropped = 0
        upstream_errors: list[str] = []
        before_window = timedelta(
            minutes=self.configuration.active_window_before_minutes
        )
        after_window = timedelta(
            minutes=self.configuration.active_window_after_minutes
        )

        for stop_id in self.configuration.stop_ids:
            try:
                payload = self.client.fetch_departures(
                    stop_id,
                    limit=self.configuration.departures_per_stop,
                    now=now,
                )
                departures = _departure_list(payload)
            except Exception as error:
                message = f"{stop_id}: {type(error).__name__}: {error}"
                print(f"[VVO] Departure fetch failed: {message}")
                upstream_errors.append(message)
                continue

            for departure in departures:
                vehicle = compute_vehicle(
                    departure,
                    requested_stop_id=stop_id,
                    now=now,
                    before_window=before_window,
                    after_window=after_window,
                )
                if vehicle is None:
                    dropped += 1
                    continue
                vehicles_by_id[vehicle["id"]] = vehicle

        vehicles = sorted(
            vehicles_by_id.values(),
            key=lambda item: (
                item.get("lineName", ""),
                item.get("directionName", ""),
                item.get("id", ""),
            ),
        )

        return {
            "updatedAt": now.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "positionSource": "scheduleEstimate",
            "vehicles": vehicles,
            "droppedItemCount": dropped,
            "upstreamErrorCount": len(upstream_errors),
        }


def make_handler(
    service: VVOVehicleService,
) -> type[BaseHTTPRequestHandler]:
    class VVOGatewayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/health":
                self._send_json(HTTPStatus.OK, {"status": "ok"})
                return
            if path != "/vvo/vehicles.json":
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "not_found"},
                )
                return

            try:
                self._send_json(HTTPStatus.OK, service.snapshot())
            except Exception as error:
                self.log_error(
                    "VVO upstream request failed: %s",
                    error,
                )
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "vvo_upstream_unavailable"},
                )

        def log_message(self, format: str, *args: Any) -> None:
            print(
                "%s - - [%s] %s"
                % (
                    self.address_string(),
                    self.log_date_time_string(),
                    format % args,
                )
            )

        def _send_json(
            self,
            status: HTTPStatus,
            payload: dict[str, Any],
        ) -> None:
            data = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8",
            )
            self.send_header("Cache-Control", "public, max-age=10")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return VVOGatewayHandler


def main() -> None:
    configuration = VVOGatewayConfiguration.from_environment()
    service = VVOVehicleService(
        configuration,
        VVOEFAClient(configuration.base_url),
    )
    server = ThreadingHTTPServer(
        (configuration.host, configuration.port),
        make_handler(service),
    )
    print(
        "VVO EFA gateway listening on "
        f"{configuration.host}:{configuration.port}"
    )
    print(f"VVO EFA base URL: {configuration.base_url}")
    print(f"Configured stops: {', '.join(configuration.stop_ids)}")
    server.serve_forever()


if __name__ == "__main__":
    main()
