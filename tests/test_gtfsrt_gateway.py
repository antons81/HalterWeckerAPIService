import base64
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from fintraffic_gateway import PublicGTFSRealtimeHTTPTransport  # noqa: E402
from gtfsrt_gateway import (  # noqa: E402
    GTFSRealtimeGatewayError,
    decode_gtfs_realtime_body,
    infer_identifier_prefix,
)


class _Response:
    def __init__(self, body: bytes, content_type: str) -> None:
        self._body = body
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class GTFSRealtimeBodyDecodingTests(unittest.TestCase):
    def test_identifier_prefix_inference_fails_closed_for_mixed_ids(self) -> None:
        self.assertEqual(
            infer_identifier_prefix(("au-seq:trip", "au-seq:route")),
            "au-seq:",
        )
        self.assertEqual(
            infer_identifier_prefix(("au-seq:trip", "other:route")),
            "",
        )

    def test_raw_protobuf_response_is_preserved(self) -> None:
        raw = b"\x0a\x00"

        self.assertEqual(decode_gtfs_realtime_body(raw, content_type="application/x-google-protobuf"), raw)

    def test_valid_base64_wrapped_protobuf_response_is_decoded(self) -> None:
        raw = b"\x0a\x00"
        wrapped = base64.b64encode(raw)

        self.assertEqual(decode_gtfs_realtime_body(wrapped, content_type="application/json"), raw)

    def test_malformed_base64_is_rejected(self) -> None:
        with self.assertRaisesRegex(GTFSRealtimeGatewayError, "invalid Base64"):
            decode_gtfs_realtime_body(b"not-base64%", content_type="application/json")

    def test_base64_decoding_to_invalid_protobuf_is_rejected(self) -> None:
        wrapped = base64.b64encode(b"\x08\x01")

        with self.assertRaisesRegex(GTFSRealtimeGatewayError, "invalid GTFS-Realtime protobuf"):
            decode_gtfs_realtime_body(wrapped, content_type="application/json")

    def test_public_transport_decodes_json_wrapped_feed(self) -> None:
        raw = b"\x0a\x00"
        response = _Response(base64.b64encode(raw), "application/json; charset=utf-8")
        transport = PublicGTFSRealtimeHTTPTransport("test-agent")

        with mock.patch("fintraffic_gateway.urlopen", return_value=response):
            self.assertEqual(transport("https://example.invalid/feed"), raw)


if __name__ == "__main__":
    unittest.main()
