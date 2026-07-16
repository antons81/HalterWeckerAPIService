#!/usr/bin/env python3

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.rnv_gateway import OAuthTokenProvider, RNVGatewayConfiguration


class MockHTTPResponse:
    def __init__(self, payload: dict[str, object]):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return io.BytesIO(self.payload)

    def __exit__(self, exception_type, exception, traceback):
        return False


class RNVGatewayTests(unittest.TestCase):
    def test_oauth_token_is_cached_and_credentials_are_form_encoded(self) -> None:
        configuration = RNVGatewayConfiguration(
            oauth_url="https://oauth.example/token",
            client_id="client id",
            client_secret="secret&value",
            resource_id="resource/id",
            vehicle_positions_url="https://rnv.example/vehicles.pb",
        )
        provider = OAuthTokenProvider(configuration)
        requests = []

        def urlopen(request, timeout):
            requests.append(request)
            return MockHTTPResponse({
                "access_token": "token",
                "expires_in": 3_600,
            })

        with patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(provider.access_token(), "token")
            self.assertEqual(provider.access_token(), "token")

        self.assertEqual(len(requests), 1)
        body = requests[0].data.decode("utf-8")
        self.assertIn("grant_type=client_credentials", body)
        self.assertIn("client_id=client+id", body)
        self.assertIn("client_secret=secret%26value", body)
        self.assertIn("resource=resource%2Fid", body)


if __name__ == "__main__":
    unittest.main()
