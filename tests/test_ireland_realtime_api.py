import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import services.static_departures_api as api


class FakeHandler(api.Handler):
    def __init__(self):
        self.response_status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status, message=None):
        self.response_status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


class IrelandRealtimeAPITests(unittest.TestCase):
    def test_valid_payload_is_returned_as_original_bytes(self):
        payload = b'{"header":{"gtfs_realtime_version":"2.0"},"entity":[]}'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "vehicles.json").write_bytes(payload)
            with mock.patch.object(api, "IRELAND_REALTIME_ROOT", root):
                handler = FakeHandler()
                handler.send_ireland_realtime("vehicles.json")
        self.assertEqual(handler.response_status, 200)
        self.assertEqual(handler.wfile.getvalue(), payload)
        self.assertEqual(handler.headers["Content-Type"], "application/json; charset=utf-8")

    def test_missing_and_malformed_payloads_are_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "vehicles.json").write_text("not-json", encoding="utf-8")
            with mock.patch.object(api, "IRELAND_REALTIME_ROOT", root):
                malformed = FakeHandler()
                malformed.send_ireland_realtime("vehicles.json")
                missing = FakeHandler()
                missing.send_ireland_realtime("trip_updates.json")
        self.assertEqual(malformed.response_status, 503)
        self.assertEqual(missing.response_status, 503)
        self.assertTrue(json.loads(malformed.wfile.getvalue()))


if __name__ == "__main__":
    unittest.main()
