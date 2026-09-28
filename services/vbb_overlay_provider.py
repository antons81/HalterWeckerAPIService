"""Read-only provider-aware adapter for the published VBB Berlin overlay."""

from __future__ import annotations

import json
import math
import re
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

VBB_PROVIDER_ID = "vbb-berlin"
VBB_CITY_ID = "berlin"
VBB_TIMEZONE = "Europe/Berlin"
VBB_RUNTIME_FEATURES = ["firstDepartures", "stopLookup"]
_FILE_RE = re.compile(r"^(\d{8})-(\d{2})\.json$")


class VBBOverlayUnavailable(RuntimeError):
    """Raised when the VBB overlay is missing, stale, or not addressable."""


def normalize_vbb_radar_manifest(value: object) -> object:
    """Remove unsupported realtime claims from the published VBB metadata view."""
    if not isinstance(value, dict) or not isinstance(value.get("cities"), list):
        return value
    result = dict(value)
    cities = []
    for raw_city in value["cities"]:
        if not isinstance(raw_city, dict):
            cities.append(raw_city)
            continue
        city = dict(raw_city)
        providers = []
        for raw_provider in city.get("providers", []):
            if not isinstance(raw_provider, dict):
                providers.append(raw_provider)
                continue
            provider = dict(raw_provider)
            if provider.get("adapter") == "vbb" or provider.get("providerID") == VBB_PROVIDER_ID:
                provider["features"] = list(VBB_RUNTIME_FEATURES)
                provider["statusMessage"] = f'Scheduled departures for {city.get("name") or VBB_CITY_ID}'
            providers.append(provider)
        if "providers" in city:
            city["providers"] = providers
        cities.append(city)
    result["cities"] = cities
    return result


@dataclass(frozen=True)
class _OverlayFile:
    path: Path
    instant: datetime


def _normal(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"\bberlin\b", " ", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _distance(first: dict[str, object], second: dict[str, object]) -> float:
    try:
        lat1, lon1 = float(first["latitude"]), float(first["longitude"])
        lat2, lon2 = float(second["latitude"]), float(second["longitude"])
    except (KeyError, TypeError, ValueError):
        return float("inf")
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = lat2 - lat1, math.radians(lon2 - lon1)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_000 * 2 * math.asin(math.sqrt(min(1.0, value)))


def _day(value: object) -> date:
    try:
        return datetime.strptime(str(value or ""), "%Y%m%d").date()
    except ValueError:
        raise VBBOverlayUnavailable("invalid VBB serviceDate") from None


def _clock(value: object) -> str:
    try:
        seconds = max(0, int(value))
    except (TypeError, ValueError):
        raise VBBOverlayUnavailable("invalid VBB stop time") from None
    hour, remainder = divmod(seconds, 3600)
    minute, second = divmod(remainder, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}"


class VBBOverlayProviderAdapter:
    """Serve Berlin from hourly VBB JSON without opening departures.sqlite."""

    provider_id = VBB_PROVIDER_ID
    timezone_name = VBB_TIMEZONE

    def __init__(self, stop_data_root: Path, *, now_provider: Callable[[], datetime] | None = None, max_fallback_hours: int = 2) -> None:
        self.stop_data_root = Path(stop_data_root).resolve()
        self._zone = ZoneInfo(self.timezone_name)
        self._now_provider = now_provider or (lambda: datetime.now(self._zone))
        self._max_fallback_hours = max(0, int(max_fallback_hours))
        self._lock = threading.RLock()
        self._files: tuple[_OverlayFile, ...] | None = None
        self._stops: tuple[dict[str, object], ...] | None = None
        self._lines: tuple[dict[str, object], ...] | None = None
        self._payloads: OrderedDict[Path, dict[str, object]] = OrderedDict()

    @staticmethod
    def handles_city(city_id: str) -> bool:
        return str(city_id or "").strip().casefold() in {"berlin", "berlin-de"}

    def resolve_city(self, city_id: str) -> str:
        if not self.handles_city(city_id):
            raise VBBOverlayUnavailable(f"unsupported VBB city={city_id}")
        return VBB_CITY_ID

    def city_departure_mode(self) -> tuple[str, str, str, str]:
        return "vbb-overlay", self.timezone_name, "", ""

    def city_departure_prefixes(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return (), ()

    def _stops_catalog(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            if self._stops is None:
                path = self.stop_data_root / "stops" / "berlin.json"
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise VBBOverlayUnavailable(f"Berlin stops unavailable: {path}") from error
                if not isinstance(value, list):
                    raise VBBOverlayUnavailable("Berlin stops are not an array")
                self._stops = tuple(item for item in value if isinstance(item, dict) and item.get("id"))
            return self._stops

    def city_stop_registry(self) -> set[str]:
        return {str(item["id"]) for item in self._stops_catalog()}

    def _line_catalog(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            if self._lines is None:
                path = self.stop_data_root / "transit" / "city-lines" / "berlin.json"
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise VBBOverlayUnavailable(f"Berlin lines unavailable: {path}") from error
                lines = value.get("lines") if isinstance(value, dict) else None
                if not isinstance(lines, list):
                    raise VBBOverlayUnavailable("Berlin line catalog is invalid")
                self._lines = tuple(item for item in lines if isinstance(item, dict))
            return self._lines

    def _overlay_files(self) -> tuple[_OverlayFile, ...]:
        with self._lock:
            if self._files is None:
                root = self.stop_data_root / "transit" / "vbb" / "berlin"
                files = []
                for path in root.glob("*.json"):
                    match = _FILE_RE.fullmatch(path.name)
                    if match is None or not path.is_file():
                        continue
                    instant = datetime.strptime(f"{match.group(1)}-{match.group(2)}", "%Y%m%d-%H").replace(tzinfo=self._zone)
                    files.append(_OverlayFile(path, instant))
                files.sort(key=lambda item: item.instant)
                if not files:
                    raise VBBOverlayUnavailable(f"Berlin VBB overlay unavailable: {root}")
                self._files = tuple(files)
            return self._files

    def _payload(self, item: _OverlayFile) -> dict[str, object]:
        with self._lock:
            cached = self._payloads.get(item.path)
            if cached is not None:
                self._payloads.move_to_end(item.path)
                return cached
            try:
                value = json.loads(item.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise VBBOverlayUnavailable(f"invalid VBB overlay: {item.path}") from error
            if not isinstance(value, dict) or not isinstance(value.get("stops"), list) or not isinstance(value.get("trips"), list):
                raise VBBOverlayUnavailable(f"invalid VBB overlay schema: {item.path}")
            self._payloads[item.path] = value
            while len(self._payloads) > 3:
                self._payloads.popitem(last=False)
            return value

    def _now(self) -> datetime:
        value = self._now_provider()
        if value.tzinfo is None:
            value = value.replace(tzinfo=self._zone)
        return value.astimezone(self._zone)

    def _window(self, start: datetime, end: datetime) -> tuple[dict[str, object], ...]:
        start = start.replace(tzinfo=start.tzinfo or self._zone).astimezone(self._zone)
        end = end.replace(tzinfo=end.tzinfo or self._zone).astimezone(self._zone)
        anchor = start.replace(minute=0, second=0, microsecond=0)
        files = self._overlay_files()
        selected = next((item for item in files if item.instant == anchor), None)
        if selected is None:
            selected = min(files, key=lambda item: abs(item.instant - anchor))
            if abs(selected.instant - anchor) > timedelta(hours=self._max_fallback_hours):
                raise VBBOverlayUnavailable(f"stale VBB overlay for {anchor.isoformat()}")
        end_hour = end.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        items = tuple(item for item in files if selected.instant - timedelta(hours=1) <= item.instant <= end_hour)
        return tuple(self._payload(item) for item in items or (selected,))

    @staticmethod
    def _payload_stops(payloads: tuple[dict[str, object], ...]) -> dict[str, dict[str, object]]:
        result = {}
        for payload in payloads:
            for item in payload.get("stops", []):
                if isinstance(item, dict) and item.get("id"):
                    result[str(item["id"])] = item
        return result

    def _native_ids(self, requested: str, payloads: tuple[dict[str, object], ...]) -> tuple[str, ...]:
        native = self._payload_stops(payloads)
        if requested in native:
            return (requested,)
        public = next((item for item in self._stops_catalog() if str(item.get("id")) == requested), None)
        if public is None:
            raise VBBOverlayUnavailable(f"unknown Berlin stop={requested}")
        key = _normal(public.get("name"))
        matches = tuple(sorted(
            stop_id for stop_id, item in native.items()
            if _normal(item.get("name")) == key and _distance(public, item) <= 150
        ))
        if not matches:
            raise VBBOverlayUnavailable(f"no safe VBB mapping for Berlin stop={requested}")
        return matches

    def city_has_stop(self, city_id: str, stop_id: str) -> bool:
        if not self.handles_city(city_id):
            return False
        if str(stop_id or "") in self.city_stop_registry():
            return True
        try:
            now = self._now()
            self._native_ids(str(stop_id), self._window(now, now + timedelta(hours=1)))
            return True
        except VBBOverlayUnavailable:
            return False

    @staticmethod
    def _trip_times(trip: dict[str, object]) -> tuple[dict[str, object], ...]:
        values = trip.get("stopTimes")
        return tuple(item for item in values if isinstance(item, dict)) if isinstance(values, list) else ()

    @classmethod
    def _last_stop(cls, trip: dict[str, object]) -> str | None:
        values = cls._trip_times(trip)
        return str(values[-1].get("stopID")) if values and values[-1].get("stopID") else None

    def _line_metadata(self, line: str, route_type: object) -> dict[str, object]:
        matches = [item for item in self._line_catalog() if line in {str(value) for value in item.get("names", [])}]
        return next((item for item in matches if str(item.get("routeType")) == str(route_type)), matches[0] if len(matches) == 1 else {})

    def _board_item(self, trip: dict[str, object], stop_time: dict[str, object], requested: str, absolute: datetime) -> dict[str, object]:
        route_id, line = str(trip.get("routeID") or ""), str(trip.get("lineName") or trip.get("routeID") or "")
        direction = str(trip.get("directionName") or "") or None
        route_type = trip.get("routeType")
        try:
            mode = {0: "tram", 1: "subway", 2: "train", 3: "bus"}[int(route_type)]
        except (KeyError, TypeError, ValueError):
            mode = "unknown"
        metadata = self._line_metadata(line, route_type)
        return {
            "serviceDate": _day(trip.get("serviceDate")).isoformat(),
            "scheduledTime": _clock(stop_time.get("departureSeconds")),
            "scheduledDeparture": _clock(stop_time.get("departureSeconds")),
            "departureDateTime": absolute.isoformat(),
            "tripID": str(trip.get("id") or ""),
            "routeID": route_id,
            "line": line,
            "destination": direction,
            "directionID": None,
            "direction": direction,
            "directionKey": f"{route_id}|direction:{direction or ''}",
            "destinationStopID": self._last_stop(trip),
            "platform": None,
            "stopID": requested,
            "stopSequence": int(stop_time.get("stopSequence") or 0),
            "isRealtime": False,
            "source": "vbb-overlay",
            "providerID": self.provider_id,
            "operator": str(metadata.get("agencyID") or "") or None,
            "agencyID": str(metadata.get("agencyID") or "") or None,
            "parentStation": None,
            "platformStopID": str(stop_time.get("stopID") or ""),
            "floor": None,
            "stopDesc": None,
            "locationType": 0,
            "routeType": route_type,
            "transportMode": mode,
        }

    def board(self, city_id: str, stop_id: str, limit: int, from_date: datetime | None = None, to_date: datetime | None = None) -> list[dict[str, object]]:
        if not self.handles_city(city_id):
            raise VBBOverlayUnavailable(f"unsupported VBB city={city_id}")
        start = from_date or self._now()
        start = start.replace(tzinfo=start.tzinfo or self._zone).astimezone(self._zone)
        end_value = to_date or start + timedelta(hours=6)
        end = end_value.replace(tzinfo=end_value.tzinfo or self._zone).astimezone(self._zone)
        payloads = self._window(start, end)
        native_ids = set(self._native_ids(str(stop_id), payloads))
        result, seen = [], set()
        for payload in payloads:
            for trip in payload.get("trips", []):
                if not isinstance(trip, dict):
                    continue
                try:
                    service_day = _day(trip.get("serviceDate"))
                except VBBOverlayUnavailable:
                    continue
                for stop_time in self._trip_times(trip):
                    native_id = str(stop_time.get("stopID") or "")
                    if native_id not in native_ids:
                        continue
                    try:
                        absolute = datetime.combine(service_day, datetime.min.time(), self._zone) + timedelta(seconds=int(stop_time.get("departureSeconds") or 0))
                    except (TypeError, ValueError):
                        continue
                    if not start <= absolute <= end:
                        continue
                    key = (str(trip.get("id") or ""), native_id, service_day.isoformat())
                    if key in seen:
                        continue
                    seen.add(key)
                    result.append(self._board_item(trip, stop_time, str(stop_id), absolute))
        result.sort(key=lambda item: (str(item.get("departureDateTime") or ""), str(item.get("tripID") or "")))
        return result[:max(1, int(limit))]

    def external_departures_for(self, city_id: str, stop_id: str, limit: int, from_datetime: datetime | None, timezone_name: str, now_provider: Callable[[], datetime] | None = None) -> list[dict[str, object]]:
        start = from_datetime or (now_provider() if now_provider else self._now())
        return self.board(city_id, stop_id, limit, start, start + timedelta(hours=6))

    def lines(self, city_id: str, stop_id: str) -> list[dict[str, str | None]]:
        now = self._now()
        payloads = self._window(now, now + timedelta(hours=6))
        native_ids = set(self._native_ids(str(stop_id), payloads))
        result = {}
        for payload in payloads:
            for trip in payload.get("trips", []):
                if not isinstance(trip, dict) or not any(str(item.get("stopID") or "") in native_ids for item in self._trip_times(trip)):
                    continue
                route_id, line = str(trip.get("routeID") or ""), str(trip.get("lineName") or trip.get("routeID") or "")
                direction = str(trip.get("directionName") or "")
                metadata = self._line_metadata(line, trip.get("routeType"))
                result[(route_id, line, direction)] = {
                    "routeID": route_id,
                    "line": line,
                    "directionID": None,
                    "direction": direction or None,
                    "destination": direction or None,
                    "destinationStopID": self._last_stop(trip),
                    "directionKey": f"{route_id}|direction:{direction}",
                    "providerID": self.provider_id,
                    "agencyID": str(metadata.get("agencyID") or "") or None,
                }
        return sorted(result.values(), key=lambda item: (str(item.get("line") or ""), str(item.get("routeID") or ""), str(item.get("direction") or "")))

    def trip_details(self, city_id: str, trip_id: str, static_root: str, service_date: str | None = None) -> dict[str, object] | None:
        if not self.handles_city(city_id):
            return None
        requested = str(trip_id or "").removeprefix(f"{self.provider_id}:")
        if service_date:
            day = date.fromisoformat(service_date)
            payloads = tuple(
                self._payload(item)
                for item in self._overlay_files()
                if item.instant.date() == day
            )
            if not payloads:
                raise VBBOverlayUnavailable(f"VBB overlay unavailable for service date={service_date}")
        else:
            start, end = self._now(), self._now() + timedelta(hours=6)
            payloads = self._window(start, end)
        for payload in payloads:
            stops = self._payload_stops((payload,))
            for trip in payload.get("trips", []):
                if not isinstance(trip, dict) or str(trip.get("id") or "") != requested:
                    continue
                route_type = trip.get("routeType")
                try:
                    mode = {0: "tram", 1: "subway", 2: "train", 3: "bus"}[int(route_type)]
                except (KeyError, TypeError, ValueError):
                    mode = "unknown"
                ordered = []
                for stop_time in self._trip_times(trip):
                    native_id = str(stop_time.get("stopID") or "")
                    stop = stops.get(native_id, {})
                    ordered.append({
                        "id": native_id,
                        "name": stop.get("name"),
                        "stopSequence": int(stop_time.get("stopSequence") or 0),
                        "scheduledArrival": _clock(stop_time.get("arrivalSeconds")),
                        "scheduledDeparture": _clock(stop_time.get("departureSeconds")),
                        "latitude": stop.get("latitude"),
                        "longitude": stop.get("longitude"),
                        "platform": None,
                        "floor": None,
                    })
                metadata = self._line_metadata(str(trip.get("lineName") or ""), route_type)
                return {
                    "tripID": requested,
                    "routeID": str(trip.get("routeID") or ""),
                    "line": str(trip.get("lineName") or ""),
                    "destination": str(trip.get("directionName") or "") or (ordered[-1]["name"] if ordered else None),
                    "directionID": None,
                    "operatorID": str(metadata.get("agencyID") or "") or None,
                    "operator": str(metadata.get("agencyID") or "") or None,
                    "transportMode": mode,
                    "timezone": self.timezone_name,
                    "serviceDate": _day(trip.get("serviceDate")).isoformat(),
                    "stops": ordered,
                    "geometry": None,
                    "source": "vbb-overlay",
                    "providerID": self.provider_id,
                    "isRealtime": False,
                }
        return None

    def close(self) -> None:
        with self._lock:
            self._payloads.clear()
            self._files = None
            self._stops = None
            self._lines = None
