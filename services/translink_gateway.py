"""Server-side TransLink GTFS-Realtime TripUpdates gateway."""

from __future__ import annotations

import os
import time
from urllib.parse import urlencode

from gtfsrt_gateway import (
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_MAX_STALE_SECONDS,
    GTFSRealtimeGateway,
    GTFSRealtimeGatewayError,
)


TRANSLINK_TRIP_UPDATES_URL = "https://gtfsapi.translink.ca/v3/gtfsrealtime"
TransLinkGatewayError = GTFSRealtimeGatewayError


class TransLinkProxy(GTFSRealtimeGateway):
    def __init__(
        self,
        api_key: str,
        transport=None,
        clock=None,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        max_stale: float = DEFAULT_MAX_STALE_SECONDS,
    ) -> None:
        self._api_key = api_key
        super().__init__(
            provider_id="translink-vancouver",
            city_id="vancouver",
            path="/translink/realtime/trip-updates",
            upstream_url=lambda: (
                f"{TRANSLINK_TRIP_UPDATES_URL}?"
                f"{urlencode({'apikey': self._api_key})}"
            ),
            transport=transport,
            clock=clock or time.time,
            cache_ttl=cache_ttl,
            max_stale=max_stale,
            user_agent="HalteWecker-TransLink-GTFSRT/1.0",
        )

    @classmethod
    def from_environment(cls) -> "TransLinkProxy | None":
        api_key = os.environ.get("TRANSLINK_API_KEY", "").strip()
        return cls(api_key) if api_key else None
