"""Consumer preflight must fail before any production pointer mutation."""

import json
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts.validate_shared_release_consumers import CONTROL_FLOW_PROBE, STATIC_CITY_PROBE, parse_probe_result, image_runtime_contract, image_supported_providers, main, preflight, provider_ids, validate_release_pair
from services.release_activation_requirements import ActivationRequirementError


class ConsumerPreflightTests(unittest.TestCase):
    def test_control_flows_require_public_stop_board_and_trip_stops(self):
        plan = {"checks": [{"cityID": "wuppertal", "stopID": "10031", "hasSchedule": True,
                            "from": "2026-10-11T00:00:00", "to": "2026-10-24T23:59:59"}]}
        broken = False
        def response(url, **kwargs):
            path = urlparse(url).path
            if path.startswith("/static-stop-data/"):
                payload = [{"id": "10031", "latitude": 51.2, "longitude": 7.1}]
            elif path.endswith("/trip"):
                payload = {"stops": [] if broken else [{"stopID": "10031"}]}
            else:
                payload = {"departures": [{"tripID": "trip", "serviceDate": "2026-10-11"}]}
            result = io.StringIO(json.dumps(payload))
            result.status = 200
            return result
        with patch.object(sys, "argv", ["probe", "plan.json"]), \
             patch("builtins.open", side_effect=lambda *args: io.StringIO(json.dumps(plan))), \
             patch("urllib.request.urlopen", side_effect=response), patch("builtins.print") as output:
            exec(CONTROL_FLOW_PROBE, {})
            self.assertEqual(parse_probe_result(output.call_args.args[0])["controls"]["wuppertal"]["tripStops"], 1)
            broken = True
            with self.assertRaisesRegex(RuntimeError, "trip details lost stops"):
                exec(CONTROL_FLOW_PROBE, {})


    def test_info_logs_do_not_corrupt_explicit_probe_result(self):
        output = 'INFO legacy fallback connected\n{"diagnostic": true}\nHALTEWECKER_PREFLIGHT_RESULT={"status":"PASS"}\n'
        self.assertEqual(parse_probe_result(output), {"status": "PASS"})

    def test_missing_duplicate_or_failed_result_is_rejected(self):
        for output in ('INFO connected', 'HALTEWECKER_PREFLIGHT_RESULT={"status":"FAIL"}',
                       'HALTEWECKER_PREFLIGHT_RESULT={"status":"PASS"}\n' * 2):
            with self.subTest(output=output), self.assertRaises(ValueError):
                parse_probe_result(output)

    def test_runtime_image_mismatch_fails_even_if_provider_capabilities_match(self):
        repository = Path(__file__).resolve().parents[1]
        with patch("scripts.validate_shared_release_consumers.docker_json", return_value={}):
            with self.assertRaisesRegex(ValueError, "runtime version mismatch"):
                image_runtime_contract("sha256:stale", repository)

    def test_matching_runtime_image_contract_passes_without_network(self):
        import hashlib
        repository = Path(__file__).resolve().parents[1]
        def inspect(*args):
            self.assertIn("none", args)
            return {name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
                    for name in json.loads(args[-1])}
        with patch("scripts.validate_shared_release_consumers.docker_json", side_effect=inspect):
            self.assertEqual(len(image_runtime_contract("sha256:matching", repository)), 8)

    def test_reference_only_emits_no_candidate_acceptance(self):
        arguments = ["validator", "--reference-only", "--release", "candidate", "--rollback-release", "rollback",
                     "--build-providers", "germany,israel-mot", "--runtime-providers", "israel-mot"]
        with patch.object(sys, "argv", arguments), \
             patch("scripts.validate_shared_release_consumers.preflight", return_value={"coveragePlan": {}, "staticImage": "tested"}) as reference, \
             patch("scripts.validate_shared_release_consumers.validate_release_pair") as pair, \
             patch("builtins.print") as output:
            main()
        reference.assert_called_once()
        pair.assert_not_called()
        self.assertNotIn("candidate", json.loads(output.call_args.args[0]))

    def test_http_probe_checks_all_planned_cities_and_rejects_nonpilot_failure(self):
        cities = ("wien", "helsinki", "bochum", "berlin", "oslo", "stockholm", "toronto", "chicago", "non-pilot-city")
        plan = {"releaseID": "candidate", "supportedCities": len(cities), "apiCities": len(cities), "packageCities": 0,
                "smokeChecks": [], "packageChecks": [], "checks": [{"cityID": city, "stopID": "stop", "hasSchedule": True,
                    "backend": "fallback", "from": "2026-10-10T00:00:00", "to": "2026-10-11T23:59:59"} for city in cities]}
        requests = []
        fail = False
        def response(url, **_kwargs):
            parsed = urlparse(url)
            self.assertEqual(parsed.netloc, "127.0.0.1:8080")
            city = parse_qs(parsed.query)["cityID"][0]
            requests.append(city)
            result = io.StringIO(json.dumps({"departures": [] if fail and city == "non-pilot-city" else [{"tripID": "trip", "serviceDate": "2026-10-10", "scheduledTime": "08:00:00"}]}))
            result.status = 200
            return result
        with patch.object(sys, "argv", ["probe", "plan.json"]), \
             patch("builtins.open", side_effect=lambda *_args: io.StringIO(json.dumps(plan))), \
             patch("urllib.request.urlopen", side_effect=response), patch("builtins.print"):
            exec(compile(STATIC_CITY_PROBE, "candidate HTTP probe", "exec"), {})
            self.assertEqual(set(requests), set(cities))
            fail = True
            with self.assertRaisesRegex(RuntimeError, "lost scheduled departures: non-pilot-city"):
                exec(compile(STATIC_CITY_PROBE, "candidate HTTP probe", "exec"), {})

    def test_fixed_smoke_stop_can_have_no_services_without_hiding_city_loss(self):
        cities = ("wien", "helsinki", "bochum", "berlin")
        plan = {"releaseID": "candidate", "supportedCities": 4, "apiCities": 4,
                "packageCities": 0, "smokeChecks": [], "packageChecks": [], "checks": [
                    {"cityID": city, "stopID": "scheduled-stop", "hasSchedule": True,
                     "backend": "fallback", "from": "2026-10-10T00:00:00",
                     "to": "2026-10-11T23:59:59"} for city in cities]}
        plan["smokeChecks"] = [{"cityID": "wien", "stopID": "at:49:1320:5", "hasSchedule": False}]
        def response(url, **kwargs):
            stop = parse_qs(urlparse(url).query)["stopID"][0]
            rows = [{"serviceDate": "2026-10-10", "scheduledTime": "08:00:00"}] if stop == "scheduled-stop" else []
            result = io.StringIO(json.dumps({"departures": rows}))
            result.status = 200
            return result
        with patch.object(sys, "argv", ["probe", "plan.json"]), \
             patch("builtins.open", side_effect=lambda *args: io.StringIO(json.dumps(plan))), \
             patch("urllib.request.urlopen", side_effect=response), patch("builtins.print") as output:
            exec(compile(STATIC_CITY_PROBE, "candidate HTTP probe", "exec"), {})
            self.assertEqual(parse_probe_result(output.call_args.args[0])["status"], "PASS")
            plan["smokeChecks"][0]["hasSchedule"] = True
            with self.assertRaisesRegex(RuntimeError, "lost scheduled smoke departures"):
                exec(compile(STATIC_CITY_PROBE, "candidate HTTP probe", "exec"), {})

    def test_broken_rollback_rejects_candidate_before_candidate_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "release.json").write_text('{"providers":{"germany":{}}}')
            with patch("scripts.validate_shared_release_consumers.preflight", side_effect=ValueError("rollback missing fallback")) as call:
                with self.assertRaisesRegex(ValueError, "rollback missing fallback"):
                    validate_release_pair(root, root, ("germany",), (), "static", "route")
                self.assertEqual(call.call_count, 1)

    def test_pair_validates_explicit_candidate_and_rollback_with_same_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rollback = root / "rollback"
            rollback.mkdir()
            (rollback / "release.json").write_text('{"providers":{"germany":{}}}')
            plan = {"checks": [{"cityID": "helsinki", "hasSchedule": True}]}
            reports = [{"staticImage": "sha256:tested", "routeImage": "sha256:route", "coveragePlan": plan} for _ in range(2)]
            with patch("scripts.validate_shared_release_consumers.preflight", side_effect=reports) as call:
                result = validate_release_pair(root / "candidate", rollback, ("germany",), (), "static", "route", static_image="sha256:tested")
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(call.call_args_list[0].args[0], rollback.resolve())
            self.assertIs(call.call_args_list[0].kwargs["require_current_schedules"], False)
            self.assertEqual(call.call_args_list[1].args[0], root / "candidate")
            self.assertNotIn("require_current_schedules", call.call_args_list[1].kwargs)
            self.assertEqual(call.call_args_list[1].kwargs["reference"], rollback.resolve())
            self.assertEqual(call.call_args_list[1].kwargs["static_image"], "sha256:tested")

    def test_pair_rejects_schedule_loss_even_when_both_other_preflights_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "release.json").write_text('{"providers":{"germany":{}}}')
            reports = [{"staticImage": "image", "routeImage": "route", "coveragePlan": {"checks": [{"cityID": "helsinki", "hasSchedule": available}]}} for available in (True, False)]
            with patch("scripts.validate_shared_release_consumers.preflight", side_effect=reports):
                with self.assertRaisesRegex(ValueError, "lost scheduled cities: helsinki"):
                    validate_release_pair(root, root, ("germany",), (), "static", "route")
    def test_provider_list_normalizes_whitespace_and_duplicates(self):
        self.assertEqual(provider_ids(" israel-mot, germany,israel-mot, "), ("israel-mot", "germany"))

    def test_image_capability_probe_uses_exact_image_without_network(self):
        with patch("scripts.validate_shared_release_consumers.docker_json", return_value=["israel-mot"]) as call:
            supported = image_supported_providers("sha256:current", ("israel-mot", "germany"))
        self.assertEqual(supported, ("israel-mot",))
        arguments = call.call_args.args
        self.assertIn("sha256:current", arguments)
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertIn("r.provider_capability", arguments[-2])

    def test_unsupported_runtime_fails_before_starting_consumers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "release.json").write_text(json.dumps({"providers": {"germany": {}, "israel-mot": {}}}))
            with patch("scripts.validate_shared_release_consumers.docker_json", return_value=[{"Image": "current"}]), \
                 patch("scripts.validate_shared_release_consumers.image_supported_providers", return_value=("israel-mot",)), \
                 patch("scripts.validate_shared_release_consumers.validate_shared_release") as validation, \
                 patch("scripts.validate_shared_release_consumers.subprocess.run") as containers:
                with self.assertRaisesRegex(ActivationRequirementError, "unsupported runtime providers: germany"):
                    preflight(root, ("germany", "israel-mot"), ("germany",), "static", "route")
                validation.assert_not_called()
                containers.assert_not_called()

    def test_consumer_preflight_precedes_pointer_switch(self):
        script = (Path(__file__).resolve().parents[1] / "scripts/run_stop_data_pipeline.sh").read_text()
        activation = script.split("activate_incremental_production() {", 1)[1]
        self.assertLess(activation.index("validate_incremental_consumers || return 1"),
                        activation.index('replace_link "$CURRENT_RELEASE" "$candidate_target"'))


if __name__ == "__main__":
    unittest.main()
