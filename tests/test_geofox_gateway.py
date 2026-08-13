import base64
import hashlib
import hmac
import json
import unittest

from services.geofox_gateway import GeofoxProxy, hmac_signature


class GeofoxGatewayTests(unittest.TestCase):
    def test_signature_is_raw_hmac_sha1_base64(self):
        body = b'{"version":63}'
        expected = base64.b64encode(hmac.new(b"password", body, hashlib.sha1).digest()).decode()
        self.assertEqual(hmac_signature("password", body), expected)
        self.assertNotEqual(hmac_signature("password", body + b"\n"), expected)

    def test_same_body_is_signed_and_sent_unchanged(self):
        captured = {}

        def transport(url, body, headers, timeout):
            captured.update(url=url, body=body, headers=headers)
            return 200, b'{"returnCode":"OK","departureList":[]}'

        body = b'{"version":63,"useRealtime":true}'
        response = GeofoxProxy("user", "password", transport=transport).handle("/geofox/departureList", body)
        self.assertEqual(response.status, 200)
        self.assertEqual(captured["body"], body)
        self.assertEqual(captured["headers"]["geofox-auth-user"], "user")
        self.assertEqual(captured["headers"]["geofox-auth-signature"], hmac_signature("password", body))

    def test_application_error_is_preserved_without_leaking_credentials(self):
        def transport(url, body, headers, timeout):
            return 200, json.dumps({"returnCode": "ERROR_TEXT", "errorDevInfo": "private"}).encode()

        response = GeofoxProxy("user", "password", transport=transport).handle("/geofox/checkName", b"{}")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["returnCode"], "ERROR_TEXT")


if __name__ == "__main__":
    unittest.main()
