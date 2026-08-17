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
                        "variantID": "variant-forward",
                        "directionID": "0",
                        "destination": "Terminal forward",
                        "tripIDs": ["trip-forward"],
                        "stopIDs": ["stop-start", "stop-forward"],
                        "shapes": [forward],
                    },
                    {
                        "variantID": "variant-reverse",
                        "directionID": "1",
                        "destination": "Terminal reverse",
                        "tripIDs": ["trip-reverse"],
                        "stopIDs": ["stop-terminal", "stop-start"],
                        "shapes": [reverse],
                    },
                ],
            },
        },
    }


class KyivDirectionInferenceTests(unittest.TestCase):
    def test_exact_trip_identity_selects_variant_before_geometry(self) -> None:
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload()),
            clock=lambda: 1000.0,
        )
        result = inference.observe(
            "vehicle-trip",
            "3_6",
            KyivVehicleSample(1000, 50.4501, 30.5220, trip_id="trip-forward"),
        )
        self.assertEqual(result.variant_id, "variant-forward")
        self.assertEqual(result.destination, "Terminal forward")
        self.assertEqual(result.evidence_source, "trip-id")

    def test_stop_identity_selects_depot_terminal_without_filtering_it(self) -> None:
        payload = topology_payload()
        payload["routes"]["3_6"]["directions"][0]["destination"] = "ТРЕД"
        payload["routes"]["3_6"]["directions"][0]["stopIDs"] = ["stop-start", "depot-stop"]
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(payload),
            clock=lambda: 1000.0,
        )
        result = inference.observe(
            "vehicle-depot",
            "3_6",
            KyivVehicleSample(1000, 50.4501, 30.5220, stop_id="depot-stop"),
        )
        self.assertEqual(result.destination, "ТРЕД")
        self.assertEqual(result.evidence_source, "stop-id")

    def test_bearing_rejects_opposite_shape(self) -> None:
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload()),
            clock=lambda: 1000.0,
        )
        inference.observe("vehicle-bearing", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        result = inference.observe(
            "vehicle-bearing",
            "3_6",
            KyivVehicleSample(1060, 50.4501, 30.5260, bearing=90.0),
        )
        self.assertEqual(result.variant_id, "variant-forward")

    def test_route_change_drops_confirmed_variant(self) -> None:
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload()),
            clock=lambda: 1000.0,
        )
        first = inference.observe(
            "vehicle-route-change",
            "3_6",
            KyivVehicleSample(1000, 50.4501, 30.5220, trip_id="trip-forward"),
        )
        changed = inference.observe(
            "vehicle-route-change",
            "unknown-route",
            KyivVehicleSample(1060, 50.4501, 30.5260),
        )
        self.assertEqual(first.variant_id, "variant-forward")
        self.assertIsNone(changed.variant_id)
        self.assertIsNone(changed.destination)
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

    def test_same_direction_variants_with_equal_distance_are_ambiguous(self) -> None:
        payload = topology_payload()
        duplicate = dict(payload["routes"]["3_6"]["directions"][0])
        duplicate["variantID"] = "variant-forward-duplicate"
        duplicate["destination"] = "Another terminal"
        payload["routes"]["3_6"]["directions"].append(duplicate)
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(payload),
            clock=lambda: 1000.0,
        )
        inference.observe("vehicle-ambiguous", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        result = inference.observe("vehicle-ambiguous", "3_6", KyivVehicleSample(1060, 50.4501, 30.5260))
        self.assertIsNone(result.variant_id)
        self.assertIsNone(result.destination)

    def test_same_destination_variants_do_not_block_family_decision(self) -> None:
        payload = topology_payload()
        forward = payload["routes"]["3_6"]["directions"][0]
        forward["terminalStopID"] = "terminal-forward"
        duplicate = dict(forward)
        duplicate["variantID"] = "variant-forward-duplicate"
        duplicate["tripIDs"] = ["trip-forward-duplicate"]
        payload["routes"]["3_6"]["directions"].append(duplicate)

        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(payload),
            clock=lambda: 1000.0,
        )
        inference.observe("vehicle-family", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        result = inference.observe(
            "vehicle-family",
            "3_6",
            KyivVehicleSample(1060, 50.4501, 30.5260),
        )

        self.assertEqual(result.destination, "Terminal forward")
        self.assertIsNone(result.variant_id)

    def test_same_display_name_with_different_terminal_ids_remains_ambiguous(self) -> None:
        payload = topology_payload()
        forward = payload["routes"]["3_6"]["directions"][0]
        reverse = payload["routes"]["3_6"]["directions"][1]
        forward["destination"] = reverse["destination"] = "Same display name"
        forward["terminalStopID"] = "terminal-forward"
        reverse["terminalStopID"] = "terminal-reverse"
        reverse["shapes"] = forward["shapes"]

        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(payload),
            clock=lambda: 1000.0,
        )
        inference.observe("vehicle-terminal-name", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        result = inference.observe(
            "vehicle-terminal-name",
            "3_6",
            KyivVehicleSample(1060, 50.4501, 30.5260),
        )

        self.assertIsNone(result.destination)

    def test_depot_family_is_not_filtered(self) -> None:
        payload = topology_payload()
        forward = payload["routes"]["3_6"]["directions"][0]
        forward["destination"] = "ТРЕД депо"
        forward["terminalStopID"] = "tred-terminal"
        duplicate = dict(forward)
        duplicate["variantID"] = "variant-tred-duplicate"
        duplicate["tripIDs"] = ["trip-tred-duplicate"]
        payload["routes"]["3_6"]["directions"].append(duplicate)

        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(payload),
            clock=lambda: 1000.0,
        )
        inference.observe("vehicle-tred", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        result = inference.observe(
            "vehicle-tred",
            "3_6",
            KyivVehicleSample(1060, 50.4501, 30.5260),
        )

        self.assertEqual(result.destination, "ТРЕД депо")

    def test_far_position_and_non_monotonic_timestamp_are_unknown(self) -> None:
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload()),
            clock=lambda: 1000.0,
        )
        inference.observe("vehicle-far", "3_6", KyivVehicleSample(1000, 51.0, 31.0))
        far = inference.observe("vehicle-far", "3_6", KyivVehicleSample(1060, 51.0, 31.01))
        self.assertIsNone(far.destination)
        non_monotonic = inference.observe("vehicle-far", "3_6", KyivVehicleSample(1050, 50.4501, 30.5260))
        self.assertIsNone(non_monotonic.destination)

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
        self.assertIsNone(retained.direction_id)
        self.assertIsNone(retained.destination)
        self.assertEqual(retained.reason, "pending-switch")
        self.assertEqual(switched.direction_id, "1")
        self.assertEqual(switched.destination, "Terminal reverse")

    def test_weak_sample_resets_alternate_pending_family(self) -> None:
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload()),
            clock=lambda: 1000.0,
        )
        inference.observe("vehicle-pending-reset", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        inference.observe("vehicle-pending-reset", "3_6", KyivVehicleSample(1060, 50.4501, 30.5260))
        first_alternate = inference.observe(
            "vehicle-pending-reset",
            "3_6",
            KyivVehicleSample(1120, 50.4501, 30.5220),
        )
        weak = inference.observe(
            "vehicle-pending-reset",
            "3_6",
            KyivVehicleSample(1180, 50.4501, 30.5221),
        )
        second_alternate = inference.observe(
            "vehicle-pending-reset",
            "3_6",
            KyivVehicleSample(1240, 50.4501, 30.5185),
        )

        self.assertEqual(first_alternate.reason, "pending-switch")
        self.assertIsNone(first_alternate.destination)
        self.assertIsNone(weak.destination)
        self.assertEqual(second_alternate.reason, "pending-switch")
        self.assertIsNone(second_alternate.destination)
        self.assertEqual(
            inference._states["vehicle-pending-reset"].pending_count,
            1,
        )

    def test_stationary_sample_resets_alternate_pending_family(self) -> None:
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload()),
            clock=lambda: 1000.0,
        )
        inference.observe("vehicle-stationary-reset", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        inference.observe("vehicle-stationary-reset", "3_6", KyivVehicleSample(1060, 50.4501, 30.5260))
        first_alternate = inference.observe(
            "vehicle-stationary-reset",
            "3_6",
            KyivVehicleSample(1120, 50.4501, 30.5220),
        )
        stationary = inference.observe(
            "vehicle-stationary-reset",
            "3_6",
            KyivVehicleSample(1180, 50.4501, 30.5220),
        )
        second_alternate = inference.observe(
            "vehicle-stationary-reset",
            "3_6",
            KyivVehicleSample(1240, 50.4501, 30.5185),
        )

        self.assertEqual(first_alternate.reason, "pending-switch")
        self.assertIsNone(stationary.destination)
        self.assertEqual(second_alternate.reason, "pending-switch")
        self.assertIsNone(second_alternate.destination)
        self.assertEqual(
            inference._states["vehicle-stationary-reset"].pending_count,
            1,
        )

    def test_confirmed_family_clears_pending_alternate(self) -> None:
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload()),
            clock=lambda: 1000.0,
        )
        inference.observe("vehicle-return", "3_6", KyivVehicleSample(1000, 50.4501, 30.5220))
        inference.observe("vehicle-return", "3_6", KyivVehicleSample(1060, 50.4501, 30.5260))
        alternate = inference.observe(
            "vehicle-return",
            "3_6",
            KyivVehicleSample(1120, 50.4501, 30.5220),
        )
        confirmed = inference.observe(
            "vehicle-return",
            "3_6",
            KyivVehicleSample(1180, 50.4501, 30.5260),
        )

        self.assertEqual(alternate.reason, "pending-switch")
        self.assertEqual(confirmed.destination, "Terminal forward")
        self.assertEqual(confirmed.reason, "stable")
        self.assertEqual(inference._states["vehicle-return"].pending_count, 0)

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

    def test_stale_confirmed_family_is_not_exposed_without_fresh_evidence(self) -> None:
        inference = KyivDirectionInference(
            KyivRadarTopology.from_payload(topology_payload()),
            clock=lambda: 1000.0,
        )
        inference.observe(
            "vehicle-stale",
            "3_6",
            KyivVehicleSample(1000, 50.4501, 30.5220),
        )
        confident = inference.observe(
            "vehicle-stale",
            "3_6",
            KyivVehicleSample(1060, 50.4501, 30.5260),
        )
        stale = inference.observe(
            "vehicle-stale",
            "3_6",
            KyivVehicleSample(1120, 51.0, 31.0),
        )

        self.assertEqual(confident.destination, "Terminal forward")
        self.assertIsNone(stale.destination)
        self.assertIsNone(stale.variant_id)


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
            self.assertEqual(artifact["schemaVersion"], 2)
            directions = artifact["routes"]["3_6"]["directions"]
            self.assertEqual(len(artifact["routes"]), 1)
            self.assertEqual({item["directionID"] for item in directions}, {"0", "1"})
            self.assertEqual(
                {item["destination"] for item in directions},
                {"Terminal A", "Terminal B"},
            )
            self.assertTrue(all(item["variantID"] for item in directions))
            self.assertEqual(
                {item["tripIDs"][0] for item in directions},
                {"t0", "t1"},
            )
            self.assertEqual(
                {item["terminalStopID"] for item in directions},
                {"b", "d"},
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
