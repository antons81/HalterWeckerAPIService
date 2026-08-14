import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kyiv_gateway import (
    KYIV_VEHICLE_POSITIONS_PATH,
    KyivVehiclePositionsGateway,
)
from kyiv_radar_inference import (
    KyivDirectionInference,
    KyivRadarTopology,
    KyivVehicleSample,
)
from kyiv_radar_topology import build_radar_topology
from test_kyiv_gateway import (
    bytes_field,
    varint_field,
    vehicle_entity,
)


def topology_payload(*, shared_geometry: bool = False) -> dict[str, object]:
    forward = {
        "shapeID": "shape-forward",
        "lengthMeters": 2_000,
        "points": [
            [50.4501, 30.5180, 0],
            [50.4501, 30.5300, 850],
            [50.4501, 30.5420, 1_700],
        ],
    }
    reverse = {
        "shapeID": "shape-reverse",
        "lengthMeters": 2_000,
        "points": [
            [50.4501, 30.5420, 0],
            [50.4501, 30.5300, 850],
            [50.4501, 30.5180, 1_700],
        ],
    }
    if shared_geometry:
        reverse = {
            **forward,
            "shapeID": "shape-shared-reverse",
        }
    return {
        "schemaVersion": 1,
        "cityID": "kyiv",
        "routes": {
            "3_6": {
                "directions": [
                    {
                        "directionID": "0",
                        "destination": "Terminal forward",
                        "shapes": [forward],
                    },
                    {
                        "directionID": "1",
                        "destination": "Terminal reverse",
                        "shapes": [reverse],
                    },
                ],
            },
        },
    }


class KyivDirectionInferenceTests(unittest.TestCase):
    def test_first_sample_is_unknown_and_second_moving_sample_is_confident(self) -> None:
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload()),
            clock=lambda: 1000.0,
        )

        first = inference.observe(
            "vehicle-sequence",
            "3_6",
            KyivVehicleSample(1000, 50.4501, 30.5220),
        )
        second = inference.observe(
            "vehicle-sequence",
            "3_6",
            KyivVehicleSample(1060, 50.4501, 30.5260),
        )

        self.assertIsNone(first.direction_id)
        self.assertIsNone(first.destination)
        self.assertEqual(second.direction_id, "0")
        self.assertEqual(second.destination, "Terminal forward")
        self.assertGreaterEqual(second.confidence or 0, 0.58)

    def test_shared_geometry_does_not_guess_direction(self) -> None:
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload(shared_geometry=True)),
            clock=lambda: 1000.0,
        )
        inference.observe("vehicle-sequence", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        result = inference.observe("vehicle-sequence", "3_6", KyivVehicleSample(1060, 50.4501, 30.5260))

        self.assertIsNone(result.direction_id)
        self.assertIsNone(result.destination)

    def test_multiple_shapes_for_direction_are_aggregated(self) -> None:
        payload = topology_payload()
        directions = payload["routes"]["3_6"]["directions"]
        directions[0]["shapes"] = [
            {
                "shapeID": "irrelevant-variant",
                "lengthMeters": 1_000,
                "points": [
                    [50.60, 30.60, 0],
                    [50.60, 30.61, 1_000],
                ],
            },
            *directions[0]["shapes"],
        ]
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(payload),
            clock=lambda: 1000.0,
        )
        inference.observe("vehicle-sequence", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        result = inference.observe("vehicle-sequence", "3_6", KyivVehicleSample(1060, 50.4501, 30.5260))

        self.assertEqual(result.direction_id, "0")
        self.assertEqual(result.destination, "Terminal forward")

    def test_jitter_does_not_guess_direction(self) -> None:
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload()),
            clock=lambda: 1000.0,
        )
        result = inference.observe("vehicle-sequence", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        for timestamp, longitude in ((1060, 30.5221), (1120, 30.5222), (1180, 30.5221)):
            result = inference.observe(
                "vehicle-sequence",
                "3_6",
                KyivVehicleSample(timestamp, 50.4501, longitude),
            )

        self.assertIsNone(result.direction_id)
        self.assertIsNone(result.destination)

    def test_hysteresis_retains_direction_then_switches_after_two_strong_samples(self) -> None:
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload()),
            clock=lambda: 1000.0,
        )
        inference.observe("vehicle-sequence", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        forward = inference.observe("vehicle-sequence", "3_6", KyivVehicleSample(1060, 50.4501, 30.5260))
        retained = inference.observe("vehicle-sequence", "3_6", KyivVehicleSample(1120, 50.4501, 30.5220))
        switched = inference.observe("vehicle-sequence", "3_6", KyivVehicleSample(1180, 50.4501, 30.5185))

        self.assertEqual(forward.direction_id, "0")
        self.assertEqual(retained.direction_id, "0")
        self.assertEqual(switched.direction_id, "1")
        self.assertEqual(switched.destination, "Terminal reverse")

    def test_ttl_removes_history_and_requires_new_first_sample(self) -> None:
        now = [1000.0]
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload()),
            clock=lambda: now[0],
            history_ttl=10.0,
        )
        inference.observe("vehicle-sequence", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        self.assertEqual(inference.history_size(), 1)
        now[0] = 1011.0
        result = inference.observe("vehicle-sequence", "3_6", KyivVehicleSample(1060, 50.4501, 30.5260))

        self.assertIsNone(result.direction_id)
        self.assertEqual(inference.history_size(), 1)


class KyivTopologyBuildTests(unittest.TestCase):
    def test_builder_writes_cumulative_progress_and_terminal_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            archive_path = Path(temporary_directory) / "kyiv.zip"
            output = Path(temporary_directory) / "out"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "routes.txt",
                    "route_id,route_type\n3_6,3\n1_1,1\n",
                )
                archive.writestr(
                    "trips.txt",
                    "route_id,service_id,trip_id,direction_id,shape_id,trip_headsign\n"
                    "3_6,S,t0,0,s0,\n"
                    "3_6,S,t1,1,s1,\n"
                    "1_1,S,t2,0,s2,Ignored rail\n",
                )
                archive.writestr(
                    "stop_times.txt",
                    "trip_id,stop_id,stop_sequence\n"
                    "t0,a,1\n"
                    "t0,b,2\n"
                    "t1,c,1\n"
                    "t1,d,2\n"
                    "t2,e,1\n",
                )
                archive.writestr(
                    "stops.txt",
                    "stop_id,stop_name\na,Start\nb,Terminal A\n"
                    "c,Start reverse\nd,Terminal B\ne,Ignored\n",
                )
                archive.writestr(
                    "shapes.txt",
                    "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
                    "s0,50.45,30.52,1\ns0,50.45,30.53,2\n"
                    "s1,50.45,30.53,1\ns1,50.45,30.52,2\n"
                    "s2,50.45,30.50,1\ns2,50.45,30.51,2\n",
                )
                build_radar_topology(archive, [{"id": "kyiv"}], output)

            artifact = json.loads((output / "radar/kyiv.json").read_text())
            directions = artifact["routes"]["3_6"]["directions"]
            self.assertEqual(len(artifact["routes"]), 1)
            self.assertEqual({item["directionID"] for item in directions}, {"0", "1"})
            self.assertEqual(
                {item["destination"] for item in directions},
                {"Terminal A", "Terminal B"},
            )
            self.assertGreater(directions[0]["shapes"][0]["points"][-1][2], 0)


class KyivGatewayInferenceTests(unittest.TestCase):
    def test_inference_failure_never_hides_vehicle(self) -> None:
        gateway = KyivVehiclePositionsGateway(
            transport=lambda _url: bytes_field(
                1,
                varint_field(3, 1_000),
            ) + bytes_field(
                2,
                vehicle_entity(
                    entity_id="entity-valid",
                    vehicle_id="vehicle-valid",
                    route_id="3_6",
                    latitude=50.4501,
                    longitude=30.5234,
                ),
            ),
            valid_route_registry=lambda: {"3_6": "3"},
            topology=KyivRadarTopology.empty(),
            clock=lambda: 1_000.0,
        )

        response = gateway.handle(
            KYIV_VEHICLE_POSITIONS_PATH,
            {"cityID": ["kyiv"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["vehicleCount"], 1)
        self.assertIsNone(response.payload["vehicles"][0]["directionID"])


if __name__ == "__main__":
    unittest.main()
