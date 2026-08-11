import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from unittest.mock import patch

from bay_area_gateway import BayAreaVehiclePosition
from gtfsrt_gateway import RealtimeUpdate
from mta_ny_gateway import (
    MtaNYBusVehiclePositionsGateway,
    MtaNYTripUpdatesGateway,
    _Registry,
    _resolve_subway_trip,
    _trip_index,
    NYCT_BUS_NAMESPACE,
    NYCT_BUS_PROVIDER_ID,
    SUBWAY_NAMESPACE,
    SUBWAY_PROVIDER_ID,
)


class MtaNYGatewayTests(unittest.TestCase):
    def setUp(self):
        self.registries = {
            SUBWAY_PROVIDER_ID: _Registry(
                trips={f"{SUBWAY_NAMESPACE}A..N01"},
                routes={f"{SUBWAY_NAMESPACE}1"},
                stops={f"{SUBWAY_NAMESPACE}101"},
                route_by_trip={f"{SUBWAY_NAMESPACE}A..N01": f"{SUBWAY_NAMESPACE}1"},
            ),
            NYCT_BUS_PROVIDER_ID: _Registry(
                trips={f"{NYCT_BUS_NAMESPACE}bus-trip"},
                routes={f"{NYCT_BUS_NAMESPACE}M1"},
                stops={f"{NYCT_BUS_NAMESPACE}100"},
                route_by_trip={f"{NYCT_BUS_NAMESPACE}bus-trip": f"{NYCT_BUS_NAMESPACE}M1"},
            ),
            "mta-ny-mta-bus": _Registry(
                trips=set(),
                routes=set(),
                stops=set(),
                route_by_trip={},
            ),
        }

    def test_subway_suffix_join_is_unique_and_not_fake_exact_join(self):
        index = _trip_index(self.registries)
        self.assertIsNone(index.exact.get("N01"))
        self.assertEqual(
            _resolve_subway_trip("N01", index),
            f"{SUBWAY_NAMESPACE}A..N01",
        )

    def test_trip_updates_require_static_trip_route_and_stop_membership(self):
        update = RealtimeUpdate(
            trip_id="N01",
            route_id="1",
            direction_id=None,
            stop_id="101",
            stop_sequence=3,
            effective_time=1,
            delay_seconds=60,
            is_cancelled=False,
        )
        with patch("mta_ny_gateway._fetch", return_value=b"payload"), \
             patch("mta_ny_gateway.parse_trip_updates", return_value=(1, 1, (update,))), \
             patch("mta_ny_gateway.api_key_from_environment", return_value="key"):
            gateway = MtaNYTripUpdatesGateway(lambda: self.registries, lambda: "key")
            response = gateway.handle("/mta-ny/realtime/trip-updates", {})
        self.assertEqual(response.status, 200)
        self.assertGreaterEqual(response.payload["updateCount"], 1)
        self.assertEqual(response.payload["updates"][0]["tripID"], "A..N01")
        self.assertEqual(response.payload["updates"][0]["stopID"], "101")

    def test_vehicle_positions_emit_only_genuine_joined_positions(self):
        vehicles = (
            BayAreaVehiclePosition(
                vehicle_id="bus-1",
                trip_id="bus-trip",
                route_id="M1",
                direction_id="0",
                stop_id="100",
                stop_sequence=2,
                latitude=40.7,
                longitude=-74.0,
                bearing=90.0,
                speed=4.0,
                timestamp=1,
            ),
            BayAreaVehiclePosition(
                vehicle_id="unknown",
                trip_id="unknown-trip",
                route_id="M1",
                direction_id=None,
                stop_id=None,
                stop_sequence=None,
                latitude=40.7,
                longitude=-74.0,
                bearing=None,
                speed=None,
                timestamp=1,
            ),
        )
        with patch("mta_ny_gateway._fetch", return_value=b"payload"), \
             patch("mta_ny_gateway.parse_vehicle_positions", return_value=(1, 2, vehicles)):
            gateway = MtaNYBusVehiclePositionsGateway(lambda: self.registries, lambda: "key")
            response = gateway.handle("/mta-ny/realtime/bus-vehicle-positions", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["entityCount"], 2)
        self.assertEqual(response.payload["vehicleCount"], 1)
        self.assertEqual(response.payload["vehicles"][0]["vehicleID"], "bus-1")


if __name__ == "__main__":
    unittest.main()
