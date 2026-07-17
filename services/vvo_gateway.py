#!/usr/bin/env python3
"""Aggregate VVO departures and expose computed vehicle positions.

This gateway implements estimated live tracking for VVO Dresden.
VVO WebAPI provides realtime departures with delays, but not live vehicle coordinates.
The gateway:
1. Polls /dm endpoint for active departures
2. Fetches /dm/trip details to get stop sequence
3. Interpolates vehicle positions between consecutive stops
4. Marks all positions as "scheduleEstimate" - this is NOT real GPS tracking

Note: VVO uses GK4 (Gauss-Kruger zone 4) integer coordinates in WebAPI responses.
This gateway relies on VVO_STOPS.JSON catalog for WGS84 coordinates.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


VVO_STOPS_CATALOG_URL = "https://www.vvo-online.de/open_data/VVO_STOPS.JSON"
VVO_API_BASE_URL = "https://www.vvo-online.de/webapi"

# Cache VVO stop catalog globally
_stops_catalog: dict[str, dict[str, Any]] = {}
_stops_catalog_lock = threading.Lock()
_stops_catalog_last_fetch = 0.0
_stops_catalog_ttl = 3600  # 1 hour


@dataclass(frozen=True)
class VVOGatewayConfiguration:
    base_url: str = VVO_API_BASE_URL
    host: str = "0.0.0.0"
    port: int = 8082
    snapshot_ttl_seconds: float = 15
    trip_detail_ttl_seconds: float = 120
    maximum_detail_requests_per_refresh: int = 24

    @classmethod
    def from_environment(cls) -> "VVOGatewayConfiguration":
        return cls(
            base_url=os.environ.get(
                "VVO_BASE_URL", VVO_API_BASE_URL
            ).rstrip("/"),
            host=os.environ.get("VVO_GATEWAY_HOST", "0.0.0.0"),
            port=int(os.environ.get("VVO_GATEWAY_PORT", "8082")),
            snapshot_ttl_seconds=float(
                os.environ.get("VVO_SNAPSHOT_TTL_SECONDS", "15")
            ),
            trip_detail_ttl_seconds=float(
                os.environ.get("VVO_TRIP_DETAIL_TTL_SECONDS", "120")
            ),
            maximum_detail_requests_per_refresh=int(
                os.environ.get("VVO_MAX_DETAIL_REQUESTS_PER_REFRESH", "24")
            ),
        )


class VVOAPIClient:
    """Low-level client for VVO WebAPI."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def fetch_departures(self, stop_id: str, limit: int = 50) -> dict[str, Any]:
        """Fetch departures from /dm endpoint."""
        url = f"{self.base_url}/dm?stopid={urllib.parse.quote(stop_id)}&Limit={limit}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "HalteWeckerVVOGateway/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.load(response)

    def fetch_trip_details(self, stop_id: str, departure_id: str, time: str) -> dict[str, Any]:
        """Fetch trip details from /dm/trip endpoint."""
        url = f"{self.base_url}/dm/trip?stopid={urllib.parse.quote(stop_id)}&id={urllib.parse.quote(departure_id)}&time={urllib.parse.quote(time)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "HalteWeckerVVOGateway/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.load(response)


def _fetch_stops_catalog() -> dict[str, dict[str, Any]]:
    """Fetch VVO stop catalog from remote URL."""
    global _stops_catalog, _stops_catalog_last_fetch
    
    with _stops_catalog_lock:
        now = time.time()
        if _stops_catalog and now - _stops_catalog_last_fetch < _stops_catalog_ttl:
            return _stops_catalog
        
        try:
            request = urllib.request.Request(
                VVO_STOPS_CATALOG_URL,
                headers={"User-Agent": "HalteWeckerVVOGateway/1.0"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
            
            # Build catalog indexed by WebAPI ID
            catalog: dict[str, dict[str, Any]] = {}
            for stop in data:
                web_api_id = str(stop.get("id", "")).strip()
                if web_api_id:
                    catalog[web_api_id] = stop
            
            _stops_catalog = catalog
            _stops_catalog_last_fetch = now
            return catalog
        except Exception as e:
            print(f"Failed to fetch VVO stop catalog: {e}")
            if _stops_catalog:
                return _stops_catalog
            raise


def get_stop_coordinates(stop_id: str) -> tuple[float, float] | None:
    """Get WGS84 coordinates for a stop from the catalog."""
    catalog = _fetch_stops_catalog()
    stop = catalog.get(str(stop_id))
    if stop:
        try:
            lat = float(stop.get("y", stop.get("latitude", 0)))
            lon = float(stop.get("x", stop.get("longitude", 0)))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return (lat, lon)
        except (ValueError, TypeError):
            pass
    return None


def parse_vvo_timestamp(ts: str | None) -> datetime | None:
    """Parse VVO timestamp in format YYYYMMDDHHmmss."""
    if not ts or len(ts) < 14:
        return None
    try:
        year = int(ts[0:4])
        month = int(ts[4:6])
        day = int(ts[6:8])
        hour = int(ts[8:10])
        minute = int(ts[10:12])
        second = int(ts[12:14])
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def effective_time(stop: dict[str, Any]) -> datetime | None:
    """Get effective time (realtime if available, otherwise scheduled)."""
    realtime = stop.get("RealTime") or stop.get("realtime")
    scheduled = stop.get("Time") or stop.get("time")
    
    if realtime:
        rt = parse_vvo_timestamp(str(realtime))
        if rt:
            return rt
    
    if scheduled:
        st = parse_vvo_timestamp(str(scheduled))
        if st:
            return st
    
    return None


def compute_interpolated_position(
    stops: list[dict[str, Any]],
    now: datetime,
) -> tuple[float, float, int | None] | None:
    """Compute vehicle position by interpolating between stops.
    
    Returns (latitude, longitude, delay_seconds) or None if position cannot be determined.
    """
    if not stops:
        return None
    
    # Filter stops with valid times
    valid_stops = []
    for stop in stops:
        et = effective_time(stop)
        if et is not None:
            valid_stops.append((stop, et))
    
    if not valid_stops:
        return None
    
    # Sort by effective time
    valid_stops.sort(key=lambda x: x[1])
    
    # Check if now is before first stop
    first_stop, first_time = valid_stops[0]
    if now <= first_time:
        coords = get_stop_coordinates(str(first_stop.get("Id", first_stop.get("id", ""))))
        if coords:
            return (coords[0], coords[1], None)
        return None
    
    # Check if now is after last stop
    last_stop, last_time = valid_stops[-1]
    if now > last_time:
        coords = get_stop_coordinates(str(last_stop.get("Id", last_stop.get("id", ""))))
        if coords:
            return (coords[0], coords[1], None)
        return None
    
    # Find segment where now falls
    for i in range(len(valid_stops) - 1):
        current_stop, current_time = valid_stops[i]
        next_stop, next_time = valid_stops[i + 1]
        
        if current_time <= now <= next_time:
            # Found the segment
            progress = min(1.0, max(0.0, (now - current_time) / (next_time - current_time)))
            
            start_coords = get_stop_coordinates(str(current_stop.get("Id", current_stop.get("id", ""))))
            end_coords = get_stop_coordinates(str(next_stop.get("Id", next_stop.get("id", ""))))
            
            if start_coords and end_coords:
                latitude = start_coords[0] + (end_coords[0] - start_coords[0]) * progress
                longitude = start_coords[1] + (end_coords[1] - start_coords[1]) * progress
                
                # Calculate delay from departure
                delay_seconds = None
                scheduled = parse_vvo_timestamp(str(current_stop.get("Time", current_stop.get("time", ""))))
                realtime = parse_vvo_timestamp(str(current_stop.get("RealTime", current_stop.get("realtime", ""))))
                if scheduled and realtime:
                    delay = int((realtime - scheduled).total_seconds())
                    delay_seconds = delay if delay != 0 else None
                
                return (latitude, longitude, delay_seconds)
    
    return None


def compute_vehicle(
    departure: dict[str, Any],
    trip_details: dict[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    """Compute a single vehicle from departure and trip details."""
    line_name = str(departure.get("LineName", "")).strip()
    direction = str(departure.get("Direction", "")).strip()
    mot = str(departure.get("Mot", "")).lower()
    
    if not line_name:
        return None
    
    # Map VVO MOT to standard mode
    mode_map = {
        "bus": "bus",
        "tram": "tram",
        "strassenbahn": "tram",
        "u-bahn": "subway",
        "ubahn": "subway",
        "s-bahn": "suburbanTrain",
        "sbahn": "suburbanTrain",
        "regional": "regionalTrain",
        "ferry": "ferry",
        "faehre": "ferry",
    }
    mode = mode_map.get(mot, "unknown")
    
    departure_id = str(departure.get("Id", departure.get("id", "")))
    stop_id = str(departure.get("stopid", ""))
    
    if not departure_id:
        return None
    
    # Get stops from trip details
    stops = trip_details.get("Stops", [])
    if not isinstance(stops, list):
        return None
    
    position = compute_interpolated_position(stops, now)
    if position is None:
        # Fallback: use departure stop coordinates
        coords = get_stop_coordinates(stop_id)
        if coords:
            # Try to calculate delay
            delay_seconds = None
            scheduled = parse_vvo_timestamp(str(departure.get("ScheduledTime", departure.get("scheduledTime", ""))))
            realtime = parse_vvo_timestamp(str(departure.get("RealTime", departure.get("realtime", ""))))
            if scheduled and realtime:
                delay = int((realtime - scheduled).total_seconds())
                delay_seconds = delay if delay != 0 else None
            
            return {
                "id": f"vvo-{departure_id}",
                "tripID": departure_id,
                "lineName": line_name,
                "directionName": direction,
                "latitude": coords[0],
                "longitude": coords[1],
                "delaySeconds": delay_seconds,
                "mode": mode,
            }
        return None
    
    latitude, longitude, delay_seconds = position
    
    return {
        "id": f"vvo-{departure_id}",
        "tripID": departure_id,
        "lineName": line_name,
        "directionName": direction,
        "latitude": latitude,
        "longitude": longitude,
        "delaySeconds": delay_seconds,
        "mode": mode,
    }


class VVOVehicleService:
    """Main service that aggregates departures and computes vehicle positions."""

    def __init__(
        self,
        configuration: VVOGatewayConfiguration,
        client: VVOAPIClient,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ):
        self.configuration = configuration
        self.client = client
        self.monotonic_clock = monotonic_clock
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_expires_at = 0.0
        self._trip_details: dict[str, tuple[float, dict[str, Any]]] = {}
        # Track active stops and their departures
        self._active_stops: dict[str, list[dict[str, Any]]] = {}

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
        """Refresh vehicle data by fetching departures and trip details."""
        # For MVP, we fetch departures from a set of major stops in Dresden
        # In production, this should be configurable or use a stop catalog
        major_stops = [
            "33000028",  # Dresden Hauptbahnhof
            "33000029",  # Dresden-Neustadt
            "33000001",  # Dresden Postplatz
            "33000005",  # Dresden Konstante Straße
        ]
        
        vehicles: list[dict[str, Any]] = []
        dropped = 0
        seen_trip_ids = set()
        
        for stop_id in major_stops:
            try:
                departures_response = self.client.fetch_departures(stop_id, limit=20)
                departures = departures_response.get("Departures", [])
            except Exception as e:
                print(f"Failed to fetch departures for stop {stop_id}: {e}")
                continue
            
            for departure in departures:
                if not isinstance(departure, dict):
                    continue
                
                # Skip cancelled departures
                state = str(departure.get("State", "")).lower()
                if "cancelled" in state or "abgesagt" in state:
                    continue
                
                departure_id = str(departure.get("Id", "")).strip()
                scheduled_time = str(departure.get("ScheduledTime", "")).strip()
                
                if not departure_id or not scheduled_time:
                    continue
                
                # Check if we've already processed this trip
                if departure_id in seen_trip_ids:
                    continue
                seen_trip_ids.add(departure_id)
                
                # Get scheduled date
                scheduled_dt = parse_vvo_timestamp(scheduled_time)
                if scheduled_dt is None:
                    continue
                
                # Skip past departures (more than 10 minutes ago)
                if now - scheduled_dt > timedelta(minutes=10):
                    continue
                
                # Try to get trip details
                try:
                    trip_response = self.client.fetch_trip_details(
                        stop_id, departure_id, scheduled_time
                    )
                except Exception as e:
                    print(f"Failed to fetch trip details for {departure_id}: {e}")
                    dropped += 1
                    continue
                
                vehicle = compute_vehicle(departure, trip_response, now)
                if vehicle is None:
                    dropped += 1
                else:
                    vehicles.append(vehicle)
        
        vehicles.sort(key=lambda v: (v.get("lineName", ""), v.get("id", "")))
        
        return {
            "updatedAt": now.astimezone(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            "positionSource": "scheduleEstimate",
            "vehicles": vehicles,
            "droppedItemCount": dropped,
        }


def make_handler(service: VVOVehicleService) -> type[BaseHTTPRequestHandler]:
    class VVOGatewayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(HTTPStatus.OK, {"status": "ok"})
                return
            if self.path != "/vvo/vehicles.json":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            try:
                self._send_json(HTTPStatus.OK, service.snapshot())
            except Exception as error:
                self.log_error("VVO upstream request failed: %s", error)
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "vvo_upstream_unavailable"},
                )

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=10")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return VVOGatewayHandler


def main() -> None:
    configuration = VVOGatewayConfiguration.from_environment()
    service = VVOVehicleService(configuration, VVOAPIClient(configuration.base_url))
    server = ThreadingHTTPServer(
        (configuration.host, configuration.port),
        make_handler(service),
    )
    print(f"VVO Gateway listening on {configuration.host}:{configuration.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
