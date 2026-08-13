"""Server-side Geofox GTI proxy.

Credentials never leave the server. The request body is signed once and the
same bytes are sent upstream, which is required by Geofox HMAC authentication.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GEOFOX_BASE_URL = "https://gti.geofox.de/gti/public"
GEOFOX_PATH_PREFIX = "/geofox/"
Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


@dataclass(frozen=True)
class GeofoxResponse:
    status: int
    payload: object
    cache_control: str = "no-store"


def hmac_signature(password: str, body: bytes) -> str:
    digest = hmac.new(password.encode("utf-8"), body, hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _default_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except HTTPError as error:
        return int(error.code), error.read()
    except (URLError, TimeoutError):
        return 599, b""


class GeofoxProxy:
    def __init__(
        self,
        username: str,
        password: str,
        base_url: str = GEOFOX_BASE_URL,
        transport: Transport = _default_transport,
    ) -> None:
        self._username = username.strip()
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    @classmethod
    def from_environment(cls) -> "GeofoxProxy | None":
        username = os.environ.get("GEOFOX_USER", "").strip()
        password = os.environ.get("GEOFOX_PASS", "")
        if not username or not password:
            return None
        return cls(username=username, password=password, base_url=os.environ.get("GEOFOX_API_BASE_URL", GEOFOX_BASE_URL))

    def handle(self, path: str, body: bytes) -> GeofoxResponse:
        operation = path.removeprefix(GEOFOX_PATH_PREFIX).strip("/")
        if operation not in {"init", "checkName", "departureList", "departureCourse"}:
            return GeofoxResponse(404, {"error": "not found"})
        if not self._username or not self._password:
            return GeofoxResponse(503, {"error": "Geofox provider unavailable"})
        try:
            json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return GeofoxResponse(400, {"error": "invalid JSON body"})

        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "geofox-auth-type": "HmacSHA1",
            "geofox-auth-user": self._username,
            "geofox-auth-signature": hmac_signature(self._password, body),
        }
        status, upstream_body = self._transport(
            f"{self._base_url}/{operation}", body, headers, 20.0
        )
        if not upstream_body:
            return GeofoxResponse(503, {"error": "Geofox provider unavailable"})
        try:
            payload = json.loads(upstream_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return GeofoxResponse(503, {"error": "Geofox provider returned invalid JSON"})
        if 200 <= status < 300:
            return GeofoxResponse(status, payload, "no-store")
        return GeofoxResponse(status if status in (400, 401, 403, 404, 429) else 503, payload)

