#!/usr/bin/env python3
"""Aggregate VAG PULS journeys and expose computed bus positions."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


@dataclass(frozen=True)
class VAGGatewayConfiguration:
    base_url: str = "https://start.vag.de/dm/api"
    host: str = "0.0.0.0"
    port: int = 8081
    snapshot_ttl_seconds: float = 15
    trip_detail_ttl_seconds: float = 120
    maximum_detail_requests_per_refresh: int = 24

    @classmethod
    def from_environment(cls) -> "VAGGatewayConfiguration":
        return cls(
            base_url=os.environ.get(
                "VAG_PULS_BASE_URL", "https://start.vag.de/dm/api"
            ).rstrip("/"),
            host=os.environ.get("VAG_GATEWAY_HOST", "0.0.0.0"),
            port=int(os.environ.get("VAG_GATEWAY_PORT", "8081")),
            snapshot_ttl_seconds=float(
                os.environ.get("VAG_SNAPSHOT_TTL_SECONDS", "15")
            ),
            trip_detail_ttl_seconds=float(
                os.environ.get("VAG_TRIP_DETAIL_TTL_SECONDS", "120")
            ),
            maximum_detail_requests_per_refresh=int(
                os.environ.get("VAG_MAX_DETAIL_REQUESTS_PER_REFRESH", "24")
            ),
        )


class VAGPULSClient:
    def __init__(self, configuration: VAGGatewayConfiguration):
        self.configuration = configuration

    def active_bus_journeys(self) -> dict[str, object]:
        return self._json("fahrten.json/bus?timespan=10")

    def trip_detail(self, service_date: str, trip_number: str) -> dict[str, object]:
        date = urllib.parse.quote(service_date, safe="")
        trip = urllib.parse.quote(trip_number, safe="")
        return self._json(f"fahrten.json/bus/{date}/{trip}")

    def _json(self, path: str) -> dict[str, object]:
        request = urllib.request.Request(
            f"{self.configuration.base_url}/{path}",
            headers={
                "Accept": "application/json",
                "User-Agent": "HalteWeckerVAGGateway/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise ValueError("VAG PULS returned a non-object JSON payload")
        return payload


class VAGVehicleService:
    def __init__(
        self,
        configuration: VAGGatewayConfiguration,
        client: VAGPULSClient,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ):
        self.configuration = configuration
        self.client = client
        self.monotonic_clock = monotonic_clock
        self._lock = threading.Lock()
        self._snapshot: dict[str, object] | None = None
        self._snapshot_expires_at = 0.0
        self._details: dict[tuple[str, str], tuple[float, dict[str, object]]] = {}

    def snapshot(self, now: datetime | None = None) -> dict[str, object]:
        with self._lock:
            monotonic_now = self.monotonic_clock()
            if self._snapshot is not None and monotonic_now < self._snapshot_expires_at:
                return self._snapshot

            timestamp = now or datetime.now(timezone.utc)
            self._snapshot = self._refresh(timestamp, monotonic_now)
            self._snapshot_expires_at = (
                monotonic_now + self.configuration.snapshot_ttl_seconds
            )
            return self._snapshot

    def _refresh(
        self,
        now: datetime,
        monotonic_now: float,
    ) -> dict[str, object]:
        active_payload = self.client.active_bus_journeys()
        journeys = active_payload.get("Fahrten", [])
        if not isinstance(journeys, list):
            raise ValueError("VAG PULS active journey response has no Fahrten array")

        active: dict[tuple[str, str], dict[str, object]] = {}
        for journey in journeys:
            if not isinstance(journey, dict) or not journey.get("Prognose", False):
                continue
            service_date = str(journey.get("Betriebstag", "")).strip()
            trip_number = str(journey.get("Fahrtnummer", "")).strip()
            if service_date and trip_number:
                active[(service_date, trip_number)] = journey

        self._details = {
            key: value for key, value in self._details.items() if key in active
        }
        stale_keys = sorted(
            active,
            key=lambda key: self._details.get(key, (-math.inf, {}))[0],
        )
        stale_keys = [
            key for key in stale_keys
            if key not in self._details
            or monotonic_now - self._details[key][0]
            >= self.configuration.trip_detail_ttl_seconds
        ][:self.configuration.maximum_detail_requests_per_refresh]
        self._refresh_details(stale_keys, monotonic_now)

        vehicles = []
        dropped = 0
        for key, journey in active.items():
            cached = self._details.get(key)
            if cached is None:
                dropped += 1
                continue
            vehicle = compute_vehicle(cached[1], journey, now)
            if vehicle is None:
                dropped += 1
            else:
                vehicles.append(vehicle)

        vehicles.sort(key=lambda vehicle: (vehicle["lineName"], vehicle["id"]))
        return {
            "updatedAt": now.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "positionSource": "scheduleEstimate",
            "vehicles": vehicles,
            "droppedItemCount": dropped,
        }

    def _refresh_details(
        self,
        keys: list[tuple[str, str]],
        monotonic_now: float,
    ) -> None:
        if not keys:
            return
        worker_count = min(6, len(keys))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(self.client.trip_detail, date, trip): (date, trip)
                for date, trip in keys
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    self._details[key] = (monotonic_now, future.result())
                except Exception:
                    # Keep an older cached detail when a single journey refresh fails.
                    continue


def compute_vehicle(
    detail: dict[str, object],
    journey: dict[str, object],
    now: datetime,
) -> dict[str, object] | None:
    raw_stops = detail.get("Fahrtverlauf", [])
    if not isinstance(raw_stops, list):
        return None
    stops = [stop for stop in raw_stops if valid_stop(stop)]
    if not stops:
        return None

    position = interpolated_position(stops, now)
    if position is None:
        return None
    latitude, longitude, delay_seconds = position
    trip_number = str(detail.get("Fahrtnummer", journey.get("Fahrtnummer", ""))).strip()
    vehicle_number = str(
        detail.get("Fahrzeugnummer", journey.get("Fahrzeugnummer", ""))
    ).strip()
    line_name = str(detail.get("Linienname", journey.get("Linienname", ""))).strip()
    if not trip_number or not line_name:
        return None

    return {
        "id": vehicle_number or trip_number,
        "tripID": trip_number,
        "lineName": line_name,
        "directionName": str(
            detail.get("Richtungstext", journey.get("Richtungstext", ""))
        ).strip(),
        "latitude": latitude,
        "longitude": longitude,
        "delaySeconds": delay_seconds,
        "mode": "bus",
    }


def valid_stop(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    latitude = value.get("Latitude")
    longitude = value.get("Longitude")
    return (
        isinstance(latitude, (int, float))
        and isinstance(longitude, (int, float))
        and -90 <= float(latitude) <= 90
        and -180 <= float(longitude) <= 180
    )


def interpolated_position(
    stops: list[dict[str, object]],
    now: datetime,
) -> tuple[float, float, int | None] | None:
    if len(stops) == 1:
        return coordinates(stops[0]) + (delay_at(stops[0]),)

    first_time = departure_time(stops[0]) or arrival_time(stops[0])
    if first_time is not None and now <= first_time:
        return coordinates(stops[0]) + (delay_at(stops[0]),)

    for current, following in zip(stops, stops[1:]):
        departure = departure_time(current) or arrival_time(current)
        arrival = arrival_time(following) or departure_time(following)
        if departure is None or arrival is None or arrival <= departure:
            continue
        if now < departure:
            return coordinates(current) + (delay_at(current),)
        if now <= arrival:
            progress = min(1.0, max(0.0, (now - departure) / (arrival - departure)))
            current_latitude, current_longitude = coordinates(current)
            next_latitude, next_longitude = coordinates(following)
            return (
                current_latitude + (next_latitude - current_latitude) * progress,
                current_longitude + (next_longitude - current_longitude) * progress,
                delay_at(following),
            )

    return coordinates(stops[-1]) + (delay_at(stops[-1]),)


def coordinates(stop: dict[str, object]) -> tuple[float, float]:
    return float(stop["Latitude"]), float(stop["Longitude"])


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def arrival_time(stop: dict[str, object]) -> datetime | None:
    return parse_time(stop.get("AnkunftszeitIst")) or parse_time(
        stop.get("AnkunftszeitSoll")
    )


def departure_time(stop: dict[str, object]) -> datetime | None:
    return parse_time(stop.get("AbfahrtszeitIst")) or parse_time(
        stop.get("AbfahrtszeitSoll")
    )


def delay_at(stop: dict[str, object]) -> int | None:
    actual = parse_time(stop.get("AbfahrtszeitIst")) or parse_time(
        stop.get("AnkunftszeitIst")
    )
    scheduled = parse_time(stop.get("AbfahrtszeitSoll")) or parse_time(
        stop.get("AnkunftszeitSoll")
    )
    if actual is None or scheduled is None:
        return None
    return int((actual - scheduled).total_seconds())


def make_handler(service: VAGVehicleService) -> type[BaseHTTPRequestHandler]:
    class VAGGatewayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(HTTPStatus.OK, {"status": "ok"})
                return
            if self.path != "/vag/vehicles.json":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            try:
                self._send_json(HTTPStatus.OK, service.snapshot())
            except Exception as error:
                self.log_error("VAG upstream request failed: %s", error)
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "vag_upstream_unavailable"},
                )

        def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=10")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return VAGGatewayHandler


def main() -> None:
    configuration = VAGGatewayConfiguration.from_environment()
    service = VAGVehicleService(configuration, VAGPULSClient(configuration))
    server = ThreadingHTTPServer(
        (configuration.host, configuration.port),
        make_handler(service),
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
