import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import active_release_readiness as readiness


PIPELINE = Path(__file__).resolve().parents[1] / "scripts/run_stop_data_pipeline.sh"


class RouteReadinessTests(unittest.TestCase):
    def test_required_reload_restarts_exact_container_without_build(self):
        state = json.dumps([{"Image": "image", "Id": "container"}])
        with mock.patch.object(readiness, "docker", side_effect=[state, "", state]) as docker:
            readiness.reload_route("route", "image", "container")
        self.assertEqual([call.args[0] for call in docker.call_args_list], ["inspect", "restart", "inspect"])

    def test_reused_stop_data_root_does_not_require_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shared").mkdir()
            for name in ("old", "new"):
                (root / name).mkdir()
                (root / name / "release.json").write_text('{"stopData":{"path":"../shared"}}')
            self.assertEqual(readiness.stop_data_root(str(root / "old")),
                             readiness.stop_data_root(str(root / "new")))
    def test_success_preserves_image_and_container(self):
        state = json.dumps([{"Image": "image", "Id": "container"}])
        with mock.patch.object(readiness, "docker", side_effect=[state, "", state]) as docker:
            readiness.check_route("route", "image", "container")
        self.assertEqual([call.args[0] for call in docker.call_args_list], ["inspect", "exec", "inspect"])

    def test_image_change_is_rejected_before_http(self):
        with mock.patch.object(readiness, "docker", return_value=json.dumps([{"Image": "changed", "Id": "container"}])) as docker:
            with self.assertRaisesRegex(RuntimeError, "image changed"):
                readiness.check_route("route", "image", "container")
        self.assertEqual(docker.call_count, 1)

    def test_image_change_after_http_is_rejected(self):
        states = [json.dumps([{"Image": "image", "Id": "container"}]), "",
                  json.dumps([{"Image": "changed", "Id": "container"}])]
        with mock.patch.object(readiness, "docker", side_effect=states):
            with self.assertRaises(RuntimeError):
                readiness.check_route("route", "image", "container")

    def test_http_failure_propagates_without_rebuild(self):
        with mock.patch.object(readiness, "docker", side_effect=[json.dumps([{"Image": "image", "Id": "container"}]), RuntimeError("HTTP 500")]) as docker:
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                readiness.check_route("route", "image", "container")
        self.assertNotIn("build", [call.args[0] for call in docker.call_args_list])

    def test_unchanged_rollback_never_restarts_route(self):
        state = json.dumps([{"Image": "image", "Id": "container"}])
        with mock.patch.object(readiness, "docker", return_value=state) as docker:
            readiness.restore_route("route", "image", "container")
        self.assertTrue(all(call.args[0] == "inspect" for call in docker.call_args_list))

    def test_changed_container_restores_exact_original_without_build(self):
        original = json.dumps([{"Image": "image", "Id": "original"}])
        changed = json.dumps([{"Image": "other", "Id": "changed"}])
        with mock.patch.object(readiness, "docker", side_effect=[original, changed, "", "", "", "", original]) as docker:
            readiness.restore_route("route", "image", "original")
        self.assertEqual([call.args[0] for call in docker.call_args_list],
                         ["inspect", "inspect", "stop", "rename", "rename", "start", "inspect"])

    def test_missing_name_restores_captured_container_without_build(self):
        original = json.dumps([{"Image": "image", "Id": "original"}])
        missing = subprocess.CalledProcessError(1, ["docker", "inspect", "route"])
        with mock.patch.object(readiness, "docker", side_effect=[original, missing, "", "", original]) as docker:
            readiness.restore_route("route", "image", "original")
        self.assertEqual([call.args[0] for call in docker.call_args_list],
                         ["inspect", "inspect", "rename", "start", "inspect"])

    def test_deleted_original_blocks_recovery_without_image_substitution(self):
        missing = subprocess.CalledProcessError(1, ["docker", "inspect", "original"])
        with mock.patch.object(readiness, "docker", side_effect=missing) as docker:
            with self.assertRaises(subprocess.CalledProcessError):
                readiness.restore_route("route", "image", "original")
        self.assertEqual(docker.call_count, 1)

    def test_probe_checks_required_lines_and_packages(self):
        for value in ('"berlin", "100"', '"wuppertal", "635"', '"germany"', '/patterns/'):
            self.assertIn(value, readiness.ROUTE_PROBE)
        for value in ('"duisburg", "dusseldorf"', '/static-departures/health', '/static-departures/board', 'transit-radar-cities.json'):
            self.assertIn(value, readiness.STATIC_PROBE)


class NightlyActivationTests(unittest.TestCase):
    def activate(self, *, route_fail=False, static_fail=False, image_change=False, pipeline_fail=False, root_change=False):
        source = PIPELINE.read_text()
        functions = source[source.index("route_recall_activation() {"):source.index('\ncd "$REPO"', source.index("route_recall_activation() {"))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "previous").mkdir()
            (root / "previous/release.json").write_text('{"releaseID":"previous"}')
            (root / "current-release").symlink_to("previous")
            (root / "current").symlink_to("current-release/stop-data")
            (root / "image").write_text("image-original")
            runner = root / "static-runner"
            runner.write_text('#!/bin/bash\nexit "${PIPELINE_FAIL:-0}"\n')
            runner.chmod(0o755)
            harness = r'''
set -eu
REPO="$ROOT"
CURRENT_RELEASE="$ROOT/current-release"
CURRENT="$ROOT/current"
DATA_ROOT="$ROOT"
RELEASE_ID=candidate
INCREMENTAL_RELEASE_DIR="$ROOT/candidate"
HALTEWECKER_RUNTIME_PROVIDER_IDS=israel-mot
STATIC_DEPARTURES_PIPELINE="$ROOT/static-runner"
ROUTERECALL_READINESS_TIMEOUT_SECONDS=0
replace_link() { "$REAL_PYTHON" -c 'import os,sys; os.symlink(sys.argv[2],sys.argv[1]+".next"); os.replace(sys.argv[1]+".next",sys.argv[1])' "$1" "$2"; }
validate_incremental_candidate() { return 0; }
validate_incremental_consumers() { return 0; }
docker() {
  echo "$*" >> "$ROOT/docker-calls"
  if [[ "$*" == *"{{.Image}}"* ]]; then cat "$ROOT/image"; return 0; fi
  if [[ "$*" == *"{{.Id}}"* ]]; then echo original-container; return 0; fi
  return 1
}
python3() {
  if [[ "$1" == -c ]]; then "$REAL_PYTHON" "$@"; return; fi
  echo "$*" >> "$ROOT/probes"
  local target
  target=$(readlink "$CURRENT_RELEASE")
  case "$2" in
    stop-data-root) if [[ "$*" == *"/candidate"* && "$ROOT_CHANGED" == 1 ]]; then echo new-root; else echo old-root; fi;;
    reload-route) echo 'restart routerecall-api' >> "$ROOT/docker-calls";;
    route) [[ "$target" == previous || "$ROUTE_FAIL" == 0 ]] && [[ $(cat "$ROOT/image") == image-original ]];;
    restore-route) echo image-original > "$ROOT/image"; if [[ "$*" == *"--reload"* ]]; then echo 'restart routerecall-api' >> "$ROOT/docker-calls"; fi;;
    haltewecker) [[ "$target" == previous || "$STATIC_FAIL" == 0 ]];;
    *) return 99;;
  esac
}
'''
            # This harness executes activation functions only, never a build pipeline.
            if image_change:
                runner.write_text('#!/bin/bash\necho image-changed > "$ROOT/image"\nexit 0\n')
            env = dict(os.environ, ROOT=str(root), REAL_PYTHON=sys.executable,
                       ROUTE_FAIL=str(int(route_fail)), STATIC_FAIL=str(int(static_fail)),
                       PIPELINE_FAIL=str(int(pipeline_fail)), ROOT_CHANGED=str(int(root_change)))
            result = subprocess.run(["bash", "-c", harness + functions + "\nactivate_incremental_production"],
                                    env=env, capture_output=True, text=True)
            target = os.readlink(root / "current-release")
            calls = (root / "docker-calls").read_text()
            probes = (root / "probes").read_text() if (root / "probes").exists() else ""
            image = (root / "image").read_text().strip()
        return result, target, calls, probes, image

    def test_success_never_builds_or_restarts_route(self):
        result, target, calls, _, image = self.activate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target, "releases/incremental/candidate")
        self.assertEqual(image, "image-original")
        for forbidden in ("build", "compose", "restart", "pull", "rename"):
            self.assertNotIn(forbidden, calls)

    def test_route_failure_rolls_back_and_repeats_readiness(self):
        result, target, calls, probes, _ = self.activate(route_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertIn("restore-route", probes)
        self.assertIn("--release-id previous", probes)
        self.assertNotIn("build", calls)

    def test_changed_stop_data_root_restarts_same_image_only(self):
        result, target, calls, probes, image = self.activate(root_change=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target, "releases/incremental/candidate")
        self.assertEqual(image, "image-original")
        self.assertIn("reload-route", probes)
        self.assertEqual(calls.count("restart routerecall-api"), 1)
        self.assertNotIn("build", calls)

    def test_changed_root_failure_reloads_previous_release_without_build(self):
        result, target, calls, probes, image = self.activate(root_change=True, route_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertEqual(image, "image-original")
        self.assertIn("--reload", probes)
        self.assertEqual(calls.count("restart routerecall-api"), 2)
        self.assertNotIn("build", calls)

    def test_static_gate_failure_rolls_back(self):
        result, target, _, probes, _ = self.activate(static_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertIn("--release-id previous", probes)

    def test_static_pipeline_failure_rolls_back(self):
        result, target, _, probes, _ = self.activate(pipeline_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertIn("restore-route", probes)

    def test_changed_image_fails_and_restores_previous_state(self):
        result, target, calls, _, image = self.activate(image_change=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertEqual(image, "image-original")
        self.assertNotIn("build", calls)


if __name__ == "__main__":
    unittest.main()
