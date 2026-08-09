#!/usr/bin/env python3
"""Server-side proxy for the Transport for London Unified API."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


TFL_API_BASE_URL = "https://api.tfl.gov.uk"
TFL_MODES = "bus,tube"


@dataclass(frozen=True)
class TfLProxyResponse:
    status: int
    payload: object
    cache_control: str


@dataclass(frozen=True)
class _CachedResponse:
    body: bytes
    expires_at: float


Transport = Callable[[str, float], tuple[int, bytes]]


def _default_transport(url: str, timeout: float) -> tuple[int, bytes]:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except HTTPError as error:
        return int(error.code), error.read()
    except URLError:
        return 599, b""


class TfLProxy:
    def __init__(
        self,
        api_key: str | None,
        base_url: str = TFL_API_BASE_URL,
        transport: Transport = _default_transport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_key = api_key.strip() if api_key else ""
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._clock = clock
        self._cache: dict[str, _CachedResponse] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "TfLProxy":
        return cls(
            api_key=os.environ.get("TFL_API_KEY"),
            base_url=os.environ.get("TFL_API_BASE_URL", TFL_API_BASE_URL),
        )

    def handle(self, path: str, query: dict[str, list[str]]) -> TfLProxyResponse:
        if not self._api_key:
            return TfLProxyResponse(
                status=503,
                payload={"error": "TfL provider unavailable"},
                cache_control="no-store",
            )

        route = self._route(path, query)
        if route is None:
            return TfLProxyResponse(
                status=404,
                payload={"error": "not found"},
                cache_control="no-store",
            )

        upstream_path, upstream_query, ttl = route
        cache_key = upstream_path + "?" + urlencode(upstream_query)
        now = self._clock()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached.expires_at > now:
                return TfLProxyResponse(
                    status=200,
                    payload=json.loads(cached.body),
                    cache_control=f"private, max-age={int(ttl)}",
                )

        upstream_query = [*upstream_query, ("app_key", self._api_key)]
        url = f"{self._base_url}{upstream_path}?{urlencode(upstream_query)}"
        status, body = self._transport(url, 25.0)

        if not 200 <= status < 300:
            if status in (401, 403, 404, 429):
                return TfLProxyResponse(
                    status=status,
                    payload={"error": "TfL provider request rejected"},
                    cache_control="no-store",
                )
            return TfLProxyResponse(
                status=503,
                payload={"error": "TfL provider unavailable"},
                cache_control="no-store",
            )

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return TfLProxyResponse(
                status=502,
                payload={"error": "TfL provider response invalid"},
                cache_control="no-store",
            )

        with self._lock:
            self._cache[cache_key] = _CachedResponse(
                body=body,
                expires_at=now + ttl,
            )
        return TfLProxyResponse(
            status=200,
            payload=payload,
            cache_control=f"private, max-age={int(ttl)}",
        )

    def _route(
        self,
        path: str,
        query: dict[str, list[str]],
    ) -> tuple[str, list[tuple[str, str]], float] | None:
        if path == "/tfl/stops/search":
            value = self._required_query(query, "query")
            if not value:
                return None
            return (
                f"/StopPoint/Search/{quote(value, safe='')}",
                [("modes", TFL_MODES)],
                60.0,
            )

        if path == "/tfl/stops/nearby":
            latitude = self._float_query(query, "lat", -90.0, 90.0)
            longitude = self._float_query(query, "lon", -180.0, 180.0)
            radius = self._int_query(query, "radius", 100, 10_000)
            if latitude is None or longitude is None or radius is None:
                return None
            return (
                "/StopPoint",
                [
                    ("lat", str(latitude)),
                    ("lon", str(longitude)),
                    ("radius", str(radius)),
                    ("modes", TFL_MODES),
                    ("stopTypes", "NaptanPublicBusCoachTram,NaptanMetroStation"),
                ],
                60.0,
            )

        parts = [part for part in path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["tfl", "stops"] and parts[3] == "arrivals":
            stop_id = self._path_id(parts[2])
            if stop_id is None:
                return None
            return f"/StopPoint/{quote(stop_id, safe='')}/Arrivals", [("modes", TFL_MODES)], 8.0

        if len(parts) == 4 and parts[:2] == ["tfl", "vehicles"] and parts[3] == "arrivals":
            vehicle_id = self._path_id(parts[2])
            if vehicle_id is None:
                return None
            return f"/Vehicle/{quote(vehicle_id, safe='')}/Arrivals", [("modes", TFL_MODES)], 8.0

        if len(parts) == 4 and parts[:2] == ["tfl", "lines"] and parts[3] == "topology":
            line_id = self._path_id(parts[2])
            direction = self._required_query(query, "direction")
            if line_id is None or direction not in {"inbound", "outbound"}:
                return None
            return (
                f"/Line/{quote(line_id, safe='')}/Route/Sequence/{direction}",
                [("direction", direction)],
                12 * 60 * 60,
            )

        return None

    @staticmethod
    def _required_query(query: dict[str, list[str]], name: str) -> str | None:
        values = query.get(name, [])
        value = values[0].strip() if values else ""
        return value or None

    @classmethod
    def _float_query(
        cls,
        query: dict[str, list[str]],
        name: str,
        minimum: float,
        maximum: float,
    ) -> float | None:
        value = cls._required_query(query, name)
        try:
            parsed = float(value) if value is not None else None
        except ValueError:
            return None
        return parsed if parsed is not None and minimum <= parsed <= maximum else None

    @classmethod
    def _int_query(
        cls,
        query: dict[str, list[str]],
        name: str,
        minimum: int,
        maximum: int,
    ) -> int | None:
        value = cls._required_query(query, name)
        try:
            parsed = int(value) if value is not None else None
        except ValueError:
            return None
        return parsed if parsed is not None and minimum <= parsed <= maximum else None

    @staticmethod
    def _path_id(value: str) -> str | None:
        value = value.strip()
        return value if value and value not in {".", ".."} and "/" not in value else None
