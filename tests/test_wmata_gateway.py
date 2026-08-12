import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from mbta_gateway import parse_mbta_trip_updates  # noqa: E402
from test_mbta_gateway import trip_update_feed, vehicle_feed  # noqa: E402
from wmata_gateway import (  # noqa: E402
    WMATATripUpdatesGateway,
    WMATAVehiclePositionsGateway,
    WMATA_TRIP_UPDATES_PATH,
    WMATA_VEHICLE_POSITIONS_PATH,
    WMATA_URLS,
)


class WMATAGatewayTests(unittest.TestCase):
    def test_trip_updates_keep_native_ids_without_internal_namespace(self) -> None:
        gateway = WMATATripUpdatesGateway(
            api_key="test-key",
            trip_stop_resolver=lambda trip_ids, sequence_keys: {
                key: "70075" for key in sequence_keys
            },
            valid_trip_registry=lambda: ({"trip-1"}, {"trip-1": "route-1"}),
            valid_stop_registry=lambda: {"70075"},
            clock=lambda: 1000.0,
        )
        payloads = {WMATA_URLS[mode]["trip"]: trip_update_feed() for mode in ("bus", "rail")}
        for child in gateway._gateways:
            child._transport = lambda url, payloads=payloads: payloads[url]

        response = gateway.handle(WMATA_TRIP_UPDATES_PATH, {"cityID": ["washington-dc"], "stopIDs": ["70075"]})

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["entityCount"], 2)
        self.assertTrue(response.payload["updates"])
        self.assertTrue(all(update["tripID"] == "trip-1" for update in response.payload["updates"]))
        self.assertTrue(all(not update["tripID"].startswith("wmata:") for update in response.payload["updates"]))

    def test_vehicle_positions_drop_stale_and_unmatched_rows(self) -> None:
        now = 1000.0
        gateway = WMATAVehiclePositionsGateway(
            api_key="test-key",
            valid_registry=lambda: ({"trip-1"}, {"route-1"}, {"trip-1": "route-1"}, lambda value: value),
            clock=lambda: now,
        )
        gateway._transport = lambda url: vehicle_feed()

        response = gateway.handle(WMATA_VEHICLE_POSITIONS_PATH, {"cityID": ["washington-dc"]})

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["vehicleCount"], 2)
        self.assertEqual({item["mode"] for item in response.payload["vehicles"]}, {"bus", "rail"})
        self.assertTrue(all(not item["tripID"].startswith("wmata:") for item in response.payload["vehicles"]))


if __name__ == "__main__":
    unittest.main()
