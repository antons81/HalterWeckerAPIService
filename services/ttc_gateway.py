"""Server-side TTC GTFS-Realtime TripUpdates gateway."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Callable

from gtfsrt_gateway import (
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_MAX_STALE_SECONDS,
    GTFSRealtimeGateway,
)


TTC_TRIP_UPDATES_URL = "https://gtfsrt.ttc.ca/trips/update?format=binary"
TTC_SURFACE_NAMESPACE = "ttc-surface:"


class TTCProxy(GTFSRealtimeGateway):
    def __init__(
        self,
        *,
        static_data_root: str | Path = "",
        trip_registry: Callable[[], tuple[set[str], dict[str, str]]] | None = None,
        transport=None,
        clock=None,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        max_stale: float = DEFAULT_MAX_STALE_SECONDS,
    ) -> None:
        self._static_data_root = Path(static_data_root) if static_data_root else None
        self._trip_registry = trip_registry or self._load_trip_registry
        self._registry_lock = Lock()
        self._registry_identity: tuple[int, int] | None = None
        self._cached_registry: tuple[set[str], dict[str, str]] = (set(), {})
        super().__init__(
            provider_id="ttc-toronto",
            city_id="toronto",
            path="/ttc/realtime/trip-updates",
            upstream_url=TTC_TRIP_UPDATES_URL,
            namespace=TTC_SURFACE_NAMESPACE,
            transport=transport,
            clock=clock or __import__("time").time,
            cache_ttl=cache_ttl,
            max_stale=max_stale,
            valid_trip_registry=self._trip_registry,
            user_agent="HalteWecker-TTC-GTFSRT/1.0",
        )

    @classmethod
    def from_environment(cls) -> "TTCProxy":
        return cls(static_data_root=os.environ.get("STATIC_DATA_ROOT", ""))

    def _load_trip_registry(self) -> tuple[set[str], dict[str, str]]:
        root = self._static_data_root
        if root is None:
            return set(), {}
        path = root / "trips" / "toronto.json"
        try:
            stat = path.stat()
        except OSError:
            return set(), {}
        identity = (stat.st_ino, stat.st_mtime_ns)
        with self._registry_lock:
            if identity == self._registry_identity:
                return self._cached_registry
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                self._registry_identity = identity
                self._cached_registry = (set(), {})
                return self._cached_registry
            if not isinstance(payload, dict):
                self._registry_identity = identity
                self._cached_registry = (set(), {})
                return self._cached_registry
            valid_trip_ids: set[str] = set()
            route_by_trip_id: dict[str, str] = {}
            for trip_id, entry in payload.items():
                if not str(trip_id).startswith(TTC_SURFACE_NAMESPACE):
                    continue
                if not isinstance(entry, dict):
                    continue
                route_id = str(entry.get("r", ""))
                if not route_id.startswith(TTC_SURFACE_NAMESPACE):
                    continue
                valid_trip_ids.add(str(trip_id))
                route_by_trip_id[str(trip_id)] = route_id
            self._registry_identity = identity
            self._cached_registry = (valid_trip_ids, route_by_trip_id)
            return self._cached_registry
