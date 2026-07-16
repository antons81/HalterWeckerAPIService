#!/usr/bin/env python3
"""Keep rnv OAuth credentials server-side and proxy the GTFS-RT vehicle feed."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_FEED_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class RNVGatewayConfiguration:
    oauth_url: str
    client_id: str
    client_secret: str
    resource_id: str
    vehicle_positions_url: str
    host: str = "0.0.0.0"
    port: int = 8080

    @classmethod
    def from_environment(cls) -> "RNVGatewayConfiguration":
        values = {
            "oauth_url": os.environ.get("RNV_OAUTH_URL", "").strip(),
            "client_id": os.environ.get("RNV_CLIENT_ID", "").strip(),
            "client_secret": os.environ.get("RNV_CLIENT_SECRET", "").strip(),
            "resource_id": os.environ.get("RNV_RESOURCE_ID", "").strip(),
            "vehicle_positions_url": os.environ.get(
                "RNV_GTFS_RT_VEHICLE_POSITIONS_URL", ""
            ).strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"Missing rnv environment values: {', '.join(missing)}")
        return cls(
            **values,
            host=os.environ.get("RNV_GATEWAY_HOST", "0.0.0.0"),
            port=int(os.environ.get("RNV_GATEWAY_PORT", "8080")),
        )


class OAuthTokenProvider:
    def __init__(self, configuration: RNVGatewayConfiguration):
        self.configuration = configuration
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at = 0.0

    def access_token(self) -> str:
        with self._lock:
            if self._access_token and time.time() < self._expires_at - 60:
                return self._access_token

            payload = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": self.configuration.client_id,
                "client_secret": self.configuration.client_secret,
                "resource": self.configuration.resource_id,
            }).encode("utf-8")
            request = urllib.request.Request(
                self.configuration.oauth_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                token_payload = json.load(response)

            token = str(token_payload.get("access_token", "")).strip()
            if not token:
                raise ValueError("rnv OAuth response does not contain access_token")

            now = time.time()
            expires_on = token_payload.get("expires_on")
            expires_in = token_payload.get("expires_in")
            if expires_on is not None:
                self._expires_at = float(expires_on)
            elif expires_in is not None:
                self._expires_at = now + float(expires_in)
            else:
                self._expires_at = now + 300
            self._access_token = token
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._access_token = None
            self._expires_at = 0


class RNVVehicleFeedClient:
    def __init__(
        self,
        configuration: RNVGatewayConfiguration,
        token_provider: OAuthTokenProvider,
    ):
        self.configuration = configuration
        self.token_provider = token_provider

    def fetch(self) -> bytes:
        for attempt in range(2):
            request = urllib.request.Request(
                self.configuration.vehicle_positions_url,
                headers={
                    "Accept": "application/x-protobuf, application/protobuf",
                    "Authorization": f"Bearer {self.token_provider.access_token()}",
                    "User-Agent": "HalteWeckerRNVGateway/1.0",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    data = response.read(MAX_FEED_BYTES + 1)
                if len(data) > MAX_FEED_BYTES:
                    raise ValueError("rnv vehicle feed exceeds size limit")
                return data
            except urllib.error.HTTPError as error:
                if error.code == HTTPStatus.UNAUTHORIZED and attempt == 0:
                    self.token_provider.invalidate()
                    continue
                raise
        raise RuntimeError("rnv vehicle feed request failed")


def make_handler(feed_client: RNVVehicleFeedClient) -> type[BaseHTTPRequestHandler]:
    class RNVGatewayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(HTTPStatus.OK, {"status": "ok"})
                return
            if self.path != "/rnv/vehicle-positions.pb":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return

            try:
                payload = feed_client.fetch()
            except Exception as error:
                self.log_error("rnv upstream request failed: %s", error)
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "rnv_upstream_unavailable"},
                )
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: HTTPStatus, payload: dict[str, str]) -> None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return RNVGatewayHandler


def main() -> None:
    configuration = RNVGatewayConfiguration.from_environment()
    token_provider = OAuthTokenProvider(configuration)
    feed_client = RNVVehicleFeedClient(configuration, token_provider)
    server = ThreadingHTTPServer(
        (configuration.host, configuration.port),
        make_handler(feed_client),
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
