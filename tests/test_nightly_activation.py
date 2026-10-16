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
    def test_smoke_expectation_uses_one_local_day_and_runtime_board_prewarm(self):
        import sqlite3
        import types
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "departures.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript("CREATE TABLE metadata(key TEXT,value TEXT); "
                "CREATE TABLE stop_times(raw_stop_id TEXT,trip_id TEXT,departure_seconds INTEGER,stop_sequence INTEGER); "
                "CREATE TABLE raw_stops(stop_id TEXT PRIMARY KEY,canonical_stop_id TEXT,parent_station TEXT,location_type INTEGER); "
                "CREATE TABLE trips(trip_id TEXT,service_id TEXT,route_id TEXT,city_id TEXT); "
                "CREATE TABLE routes(route_id TEXT,short_name TEXT,long_name TEXT); "
                "CREATE TABLE active_services(service_id TEXT,service_date TEXT);")
            today = datetime.now(ZoneInfo("Europe/Berlin")).date()
            connection.execute("INSERT INTO metadata VALUES ('validThrough',?)",
                               ((today + timedelta(days=13)).isoformat(),))
            connection.executemany("INSERT INTO trips VALUES (?,?,?,?)",
                                   [("trip", "service", "route-u3", "wien"),
                                    ("trip-u1", "service-u1", "route-u1", "wien"),
                                    ("foreign-u1", "service-foreign", "route-foreign-u1", "berlin")])
            connection.executemany("INSERT INTO routes VALUES (?,?,?)",
                                   [("route-u1", "U1", ""), ("route-u3", "U3", ""),
                                    ("route-foreign-u1", "U1", "")])
            connection.executemany("INSERT INTO active_services VALUES (?,?)",
                                   [("service", today.strftime("%Y%m%d")),
                                    ("service-u1", today.strftime("%Y%m%d")),
                                    ("service-foreign", today.strftime("%Y%m%d"))])
            connection.executemany(
                "INSERT INTO raw_stops VALUES (?,?,?,?)",
                [("stop", "stop", "", 1), ("stop-child", "stop", "stop", 0)],
            )
            connection.executemany(
                "INSERT INTO stop_times VALUES ('stop-child','trip',?,?)",
                [(-86400, sequence) for sequence in range(1000)] + [(43200, 1000)],
            )
            connection.execute("INSERT INTO stop_times VALUES ('stop-child','trip-u1',43260,1)")
            connection.execute("INSERT INTO stop_times VALUES ('stop-child','foreign-u1',36000,1)")
            connection.commit()
            connection.close()
            class FixtureDatabase:
                calls = []
                ownership_checks = []
                def __init__(self, path):
                    self.connection = sqlite3.connect(path)
                def city_departure_mode(self, city):
                    return "exact-stop-with-parent-fallback", "Europe/Berlin", "", ""
                def _connection(self):
                    return self.connection
                def _query_stop_ids(self, city, stop):
                    row = self.connection.execute(
                        "SELECT location_type FROM raw_stops WHERE stop_id=?", (stop,)
                    ).fetchone()
                    if row and row[0] == 1:
                        children = self.connection.execute(
                            "SELECT stop_id FROM raw_stops WHERE parent_station=? ORDER BY stop_id",
                            (stop,),
                        ).fetchall()
                        return (stop, *(child[0] for child in children))
                    return (stop,)
                def _canonical_stop_candidates(self, city, stop):
                    return (stop,)
                def _provider_stop_ownership_predicate(self, raw_stop_alias, trip_alias):
                    self.ownership_checks.append((raw_stop_alias, trip_alias))
                    return f"{trip_alias}.city_id=?"
                def board(self, city, stop, limit, from_date, to_date):
                    from services.gtfs_board_window import service_day_epoch
                    self.calls.append((city, stop, limit, from_date, to_date))
                    self.connection.create_function(
                        "readiness_service_epoch", 1,
                        lambda day: service_day_epoch(str(day), "Europe/Berlin"),
                        deterministic=True,
                    )
                    stop_ids = self._query_stop_ids(city, stop)
                    placeholders = ",".join("?" for _ in stop_ids)
                    return self.connection.execute(
                        "SELECT st.departure_seconds "
                        "FROM stop_times st JOIN raw_stops rs ON rs.stop_id=st.raw_stop_id "
                        "JOIN trips t ON t.trip_id=st.trip_id "
                        "JOIN active_services a ON a.service_id=t.service_id "
                        "WHERE st.raw_stop_id IN (" + placeholders + ") "
                        "AND readiness_service_epoch(a.service_date)+st.departure_seconds>=? "
                        "AND readiness_service_epoch(a.service_date)+st.departure_seconds<=? "
                        "ORDER BY readiness_service_epoch(a.service_date)+st.departure_seconds, "
                        "t.trip_id, st.stop_sequence LIMIT ?",
                        (*stop_ids, from_date.timestamp(), to_date.timestamp(), limit),
                    ).fetchall()
                def close(self):
                    self.connection.close()
            module = types.SimpleNamespace(Database=FixtureDatabase)
            scope = {}
            with mock.patch.dict(sys.modules, {"static_departures_api": module,
                                              "gtfs_board_window": __import__("services.gtfs_board_window", fromlist=["*"])}):
                exec(readiness.LEGACY_SMOKE_EXPECTATION, scope)
            start, end, timezone_name, scheduled, prewarm_seconds, scheduled_lines = scope["smoke_expectation"](
                str(path), "wien", "stop")
            self.assertEqual(timezone_name, "Europe/Berlin")
            self.assertTrue(scheduled)
            self.assertGreaterEqual(prewarm_seconds, 0)
            self.assertEqual(scheduled_lines, {})
            self.assertEqual(start.date(), today)
            self.assertEqual(end.date(), today + timedelta(days=1))
            self.assertEqual((start.hour, start.minute), (0, 0))
            self.assertEqual((end.hour, end.minute), (0, 0))
            self.assertIn(end.timestamp() - start.timestamp(), {23 * 3600, 24 * 3600, 25 * 3600})
            request_end = end - timedelta(microseconds=1)
            self.assertEqual(request_end.date(), today)
            self.assertLess(request_end.timestamp(), end.timestamp())
            self.assertEqual(FixtureDatabase.calls[-1],
                             ("wien", "stop", 3, start, request_end))
            _, _, _, _, _, scheduled_lines = scope["smoke_expectation"](
                str(path), "wien", "stop", required_lines=("U1", "U3"))
            self.assertEqual(set(scheduled_lines), {"U1", "U3"})
            from services.gtfs_board_window import service_day_epoch
            expected_wien_u1 = service_day_epoch(today.strftime("%Y%m%d"), "Europe/Berlin") + 43260
            self.assertEqual(scheduled_lines["U1"], expected_wien_u1)
            self.assertEqual(FixtureDatabase.calls[-3],
                             ("wien", "stop", 3, start, request_end))
            self.assertEqual(FixtureDatabase.calls[-2][2], 100)
            self.assertEqual(FixtureDatabase.calls[-1][2], 100)
            self.assertFalse(scope["smoke_expectation"](str(path), "wien", "not-served")[3])
            self.assertEqual(FixtureDatabase.ownership_checks,
                             [("rs", "t"), ("rs", "t"), ("rs", "t")])
            self.assertNotIn('metadata["validThrough"]', readiness.LEGACY_SMOKE_EXPECTATION)
            self.assertIn("database.board(", readiness.LEGACY_SMOKE_EXPECTATION)
            self.assertNotIn("_external_stop_time_ids", readiness.LEGACY_SMOKE_EXPECTATION)

    def test_smoke_window_respects_dst_local_calendar_days(self):
        import types
        from datetime import date, timedelta
        module = types.SimpleNamespace(Database=object)
        scope = {}
        with mock.patch.dict(sys.modules, {"static_departures_api": module,
                                          "gtfs_board_window": __import__("services.gtfs_board_window", fromlist=["*"])}):
            exec(readiness.LEGACY_SMOKE_EXPECTATION, scope)

        for local_date, expected_hours in ((date(2026, 3, 29), 23), (date(2026, 10, 25), 25)):
            start, end = scope["local_calendar_day_window"]("Europe/Berlin", local_date)
            self.assertEqual(start.date(), local_date)
            self.assertEqual(end.date(), local_date + timedelta(days=1))
            self.assertEqual((start.hour, start.minute), (0, 0))
            self.assertEqual((end.hour, end.minute), (0, 0))
            self.assertEqual(end.timestamp() - start.timestamp(), expected_hours * 3600)
            self.assertEqual(start.tzinfo, end.tzinfo)


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

    def test_full_sqlite_release_metadata_uses_its_stop_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "20261013T044307Z-399083"
            stop_data = release / "stop-data"
            stop_data.mkdir(parents=True)
            (release / "release-metadata.json").write_text(json.dumps({
                "releaseID": release.name,
                "stopManifestVersion": "2026-10-14",
            }))

            self.assertEqual(readiness.stop_data_root(str(release)), stop_data.resolve())

    def test_full_sqlite_release_metadata_must_match_its_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "20261013T044307Z-399083"
            (release / "stop-data").mkdir(parents=True)
            (release / "release-metadata.json").write_text('{"releaseID":"other-release"}')

            with self.assertRaisesRegex(RuntimeError, "ID does not match"):
                readiness.stop_data_root(str(release))
    def test_success_preserves_image_and_container(self):
        state = json.dumps([{"Image": "image", "Id": "container"}])
        with mock.patch.object(readiness, "docker", side_effect=[state, "", state]) as docker:
            readiness.check_route("route", "image", "container")
        self.assertEqual([call.args[0] for call in docker.call_args_list], ["inspect", "exec", "inspect"])

    def test_shared_route_requires_read_only_common_database_configuration(self):
        state = json.dumps([{
            "Image": "image",
            "Id": "container",
            "Config": {"Env": [
                "ROUTERECALL_PROVIDER_RUNTIME=0",
                "ROUTERECALL_DATABASE=/data/haltewecker/current-release/departures.sqlite",
                "ROUTERECALL_RELEASE_POINTER=/data/haltewecker/current-release",
            ]},
        }])
        with mock.patch.object(readiness, "docker", side_effect=[state, "", state]) as docker:
            readiness.check_shared_route("route", "image", "container")
        self.assertEqual([call.args[0] for call in docker.call_args_list], ["inspect", "exec", "inspect"])

    def test_shared_route_rejects_provider_runtime_before_http(self):
        state = json.dumps([{
            "Image": "image",
            "Id": "container",
            "Config": {"Env": ["ROUTERECALL_PROVIDER_RUNTIME=1"]},
        }])
        with mock.patch.object(readiness, "docker", return_value=state) as docker:
            with self.assertRaisesRegex(RuntimeError, "provider-shard runtime"):
                readiness.check_shared_route("route", "image", "container")
        docker.assert_called_once()

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
        for value in (
            '"duisburg", "dusseldorf"',
            '/static-departures/health',
            '/static-departures/board',
            'local_calendar_day_window(timezone_name)',
            'request_end = end - timedelta(microseconds=1)',
            '"wien", "at:49:1876:0:1"',
            '"wien", "at:49:1320:5"',
            '"helsinki", "fi-hsl:1040401"',
            'with urlopen(root + path, timeout=15)',
            'response.status != 200',
            'not isinstance(departures, list)',
            '(scheduled and not departures)',
            'empty departures response:',
            'epochs != sorted(epochs)',
            'not start.timestamp() <= value < end.timestamp()',
            'database.board(',
            'sql_prewarm_seconds=%.3f',
            'http_seconds=%.3f',
            'row.get("line")',
            'required_lines = ("U1", "U3") if city == "wien" and stop == "at:49:1320:5" else ()',
            'MIN({BOARD_EPOCH_SQL})',
            'city=wien scheduled_lines=%s',
            'scheduled Wien line is missing from API board:',
            'api_match=True',
            'limit=100,',
            'transit-radar-cities.json',
        ):
            self.assertIn(value, readiness.STATIC_PROBE)
        source_check = readiness.STATIC_PROBE.index(
            "start, end, zone, scheduled, sql_prewarm_seconds, scheduled_lines = smoke_expectation(")
        http_board = readiness.STATIC_PROBE.index(
            'payload = get("/static-departures/board?"')
        self.assertLess(source_check, http_board)


class NightlyActivationTests(unittest.TestCase):
    def test_rollback_link_cleanup_removes_only_links_and_keeps_release_data(self):
        source = PIPELINE.read_text()
        start = source.index("remove_persistent_rollback_links() {")
        end = source.index("\nactivate_incremental_production() {", start)
        cleanup = source[start:end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "releases/old"
            stop_data = release / "stop-data"
            stop_data.mkdir(parents=True)
            (release / "departures.sqlite").write_bytes(b"release-data")
            (root / "previous").mkdir()
            (root / "rollback").symlink_to(release)
            (root / "previous/stop-data").symlink_to(stop_data)
            harness = f'''\
set -euo pipefail
DATA_ROOT={str(root)!r}
PREVIOUS="$DATA_ROOT/previous/stop-data"
'''
            result = subprocess.run(
                ["bash", "-c", harness + cleanup + "\nremove_persistent_rollback_links"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(os.path.lexists(root / "rollback"))
            self.assertFalse(os.path.lexists(root / "previous/stop-data"))
            self.assertEqual((release / "departures.sqlite").read_bytes(), b"release-data")

    def test_rollback_link_cleanup_refuses_non_symlink_paths_without_partial_removal(self):
        source = PIPELINE.read_text()
        start = source.index("remove_persistent_rollback_links() {")
        end = source.index("\nactivate_incremental_production() {", start)
        cleanup = source[start:end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "releases/old"
            release.mkdir(parents=True)
            (root / "previous").mkdir()
            (root / "rollback").symlink_to(release)
            (root / "previous/stop-data").write_text("user data", encoding="utf-8")
            harness = f'''\
set -euo pipefail
DATA_ROOT={str(root)!r}
PREVIOUS="$DATA_ROOT/previous/stop-data"
'''
            result = subprocess.run(
                ["bash", "-c", harness + cleanup + "\nremove_persistent_rollback_links"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((root / "rollback").is_symlink())
            self.assertEqual((root / "previous/stop-data").read_text(encoding="utf-8"), "user data")

    def test_full_sqlite_commit_cleans_rollback_links_instead_of_publishing_previous(self):
        source = PIPELINE.read_text()
        commit_start = source.index("# Publish the handoff only after the release")
        commit_end = source.index('\necho "[StopData] release=$RELEASE_ID stage=commit duration=', commit_start)
        commit = source[commit_start:commit_end]
        self.assertIn("remove_persistent_rollback_links", commit)
        self.assertNotIn('ln -s "../releases/${OLD_RELEASE_TARGET#releases/}/stop-data"', commit)

    def test_route_reconfiguration_uses_preflight_image_and_read_only_sqlite(self):
        source = PIPELINE.read_text()
        start = source.index("route_recall_activation() {")
        end = source.index("\nhaltewecker_activation_readiness() {", start)
        functions = source[start:end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / "routerecall-api.compose.yml"
            compose.write_text("services:\n  routerecall-api:\n    image: route\n")
            harness = r'''
set -eu
REPO="$ROOT"
RELEASE_ID=candidate
ROUTERECALL_COMPOSE_FILE="$ROOT/routerecall-api.compose.yml"
ROUTERECALL_ACTIVATION_IMAGE=sha256:verified
ROUTERECALL_ACTIVATION_CONTAINER_ID=old-container
ROUTERECALL_ACTIVE_IMAGE=sha256:verified
ROUTERECALL_ACTIVE_CONTAINER_ID=old-container
ROUTERECALL_ORIGINAL_SHARED_DATABASE=0
ROUTERECALL_ACTIVE_SHARED_DATABASE=0
ROUTERECALL_PREVIOUS_CONTAINER_PRESERVED=0
ROUTERECALL_READINESS_TIMEOUT_SECONDS=0
HALTEWECKER_ACTIVATION_DRY_RUN=0
docker() {
  printf '%s env_image=%s provider=%s db=%s\n' "$*" "${ROUTERECALL_IMAGE_REF:-}" \
    "${ROUTERECALL_PROVIDER_RUNTIME:-}" "${ROUTERECALL_DATABASE:-}" >> "$ROOT/docker.log"
  case "$1" in
    inspect)
      if [ "$#" -eq 2 ] && [[ "$2" == *rollback* ]]; then return 1; fi
      case "$*" in
        *"{{.Config.Image}}"*) echo routerecall-api:prod;;
        *"{{.Image}}"*) echo sha256:verified;;
        *"{{.Id}}"*) if [ "${ROUTE_NEW:-0}" = 1 ]; then echo new-container; else echo old-container; fi;;
      esac
      ;;
    image)
      echo sha256:verified
      ;;
    rename|stop)
      ;;
    compose)
      [ "${COMPOSE_FAIL:-0}" = 0 ] || return 1
      ROUTE_NEW=1
      ;;
    *) return 64;;
  esac
}
python3() {
  case "$*" in
    *shared-route*|*route*) echo "$*" >> "$ROOT/readiness.log"; return "${READINESS_FAIL:-0}";;
    *) return 64;;
  esac
}
'''
            result = subprocess.run(
                ["bash", "-c", harness + functions + "\nactivate_route_recall_shared_sqlite"],
                env=dict(os.environ, ROOT=str(root)),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root / "docker.log").read_text()
            self.assertIn("rename routerecall-api routerecall-api-rollback-candidate", calls)
            self.assertIn("stop --time 10 routerecall-api-rollback-candidate", calls)
            self.assertIn("--no-build --pull never --no-deps --force-recreate routerecall-api", calls)
            self.assertIn("env_image=routerecall-api:prod provider=0 db=/data/haltewecker/current-release/departures.sqlite", calls)
            self.assertIn("shared-route", (root / "readiness.log").read_text())

    def activate(self, *, route_fail=False, static_fail=False, image_change=False, pipeline_fail=False, root_change=False,
                 coverage_fail=False, rollback_fail=False, dry_run=False, source_freshness_fail=False,
                 existing_rollback=False, existing_previous=False):
        source = PIPELINE.read_text()
        freshness_start = source.index("validate_incremental_source_freshness() {")
        freshness = source[freshness_start:source.index("\nvalidate_incremental_consumers() {", freshness_start)]
        activation_start = source.index("route_recall_activation() {")
        activation = source[activation_start:source.index('\ncd "$REPO"', activation_start)]
        functions = freshness + "\n" + activation
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "previous").mkdir()
            (root / "previous/release.json").write_text('{"releaseID":"previous"}')
            (root / "current-release").symlink_to("previous")
            (root / "current").symlink_to("current-release/stop-data")
            if existing_rollback:
                (root / "rollback").symlink_to("saved-rollback")
            if existing_previous:
                (root / "previous/stop-data").symlink_to("saved-previous")
            (root / "image").write_text("image-original")
            runner = root / "static-runner"
            runner.write_text('#!/bin/bash\nexit "${PIPELINE_FAIL:-0}"\n')
            runner.chmod(0o755)
            harness = r'''
set -eu
REPO="$ROOT"
CURRENT_RELEASE="$ROOT/current-release"
CURRENT="$ROOT/current"
DEPARTURES_CURRENT="$ROOT/departures-current.sqlite"
DATA_ROOT="$ROOT"
PREVIOUS="$ROOT/previous/stop-data"
RELEASE_ID=candidate
INCREMENTAL_RELEASE_DIR="$ROOT/candidate"
ARTIFACTS_JSON="$ROOT/gtfs-artifacts.json"
HALTEWECKER_RUNTIME_PROVIDER_IDS=israel-mot
STATIC_ACTIVATION_IMAGE=image-original
STATIC_DEPARTURES_PIPELINE="$ROOT/static-runner"
ROUTERECALL_READINESS_TIMEOUT_SECONDS=0
PREVIOUS_DEPARTURES_POINTER_PRESENT=0
PREVIOUS_DEPARTURES_POINTER_TARGET=""
PREVIOUS_ROLLBACK_POINTER_PRESENT=0
PREVIOUS_ROLLBACK_POINTER_TARGET=""
PREVIOUS_STATIC_RUNTIME_MODE=legacy
replace_link() { "$REAL_PYTHON" -c 'import os,sys; os.symlink(sys.argv[2],sys.argv[1]+".next"); os.replace(sys.argv[1]+".next",sys.argv[1])' "$1" "$2"; }
validate_incremental_candidate() { return 0; }
validate_incremental_consumers() { [[ "$COVERAGE_FAIL" == 0 ]]; }
docker() {
  echo "$*" >> "$ROOT/docker-calls"
  if [[ "$*" == *"{{range .Config.Env}}"* ]]; then
    printf 'HALTEWECKER_STATIC_DEPARTURES_RUNTIME_MODE=hybrid\nHALTEWECKER_STATIC_DEPARTURES_HYBRID_RUNTIME=1\n'
    return 0
  fi
  if [[ "$*" == *"{{.Image}}"* ]]; then cat "$ROOT/image"; return 0; fi
  if [[ "$*" == *"{{.Id}}"* ]]; then echo original-container; return 0; fi
  return 1
}
python3() {
  if [[ "$1" == "-" ]]; then
    echo source-freshness >> "$ROOT/probes"
    [[ "$SOURCE_FRESHNESS_FAIL" == 0 ]]
    return
  fi
  if [[ "$1" == -c ]]; then "$REAL_PYTHON" "$@"; return; fi
  echo "$*" >> "$ROOT/probes"
  local target
  target=$(readlink "$CURRENT_RELEASE")
  case "$2" in
    stop-data-root) if [[ "$*" == *"/candidate"* && "$ROOT_CHANGED" == 1 ]]; then echo new-root; else echo old-root; fi;;
    reload-route) echo 'restart routerecall-api' >> "$ROOT/docker-calls";;
    route) [[ "$target" == previous || "$ROUTE_FAIL" == 0 ]] && [[ $(cat "$ROOT/image") == image-original ]];;
    restore-route) echo image-original > "$ROOT/image"; if [[ "$*" == *"--reload"* ]]; then echo 'restart routerecall-api' >> "$ROOT/docker-calls"; fi;;
    haltewecker) if [[ "$target" == previous ]]; then [[ "$ROLLBACK_FAIL" == 0 ]]; else [[ "$STATIC_FAIL" == 0 ]]; fi;;
    *) return 99;;
  esac
}
'''
            # This harness executes activation functions only, never a build pipeline.
            if image_change:
                runner.write_text('#!/bin/bash\necho image-changed > "$ROOT/image"\nexit 0\n')
            env = dict(os.environ, ROOT=str(root), REAL_PYTHON=sys.executable,
                       ROUTE_FAIL=str(int(route_fail)), STATIC_FAIL=str(int(static_fail)),
                       PIPELINE_FAIL=str(int(pipeline_fail)), ROOT_CHANGED=str(int(root_change)),
                       COVERAGE_FAIL=str(int(coverage_fail)), ROLLBACK_FAIL=str(int(rollback_fail)),
                       SOURCE_FRESHNESS_FAIL=str(int(source_freshness_fail)),
                       HALTEWECKER_ACTIVATION_DRY_RUN=str(int(dry_run)))
            result = subprocess.run(["bash", "-c", harness + functions + "\nactivate_incremental_production"],
                                    env=env, capture_output=True, text=True)
            target = os.readlink(root / "current-release")
            rollback_pointer = root / "rollback"
            rollback_target = os.readlink(rollback_pointer) if rollback_pointer.is_symlink() else None
            previous_pointer = root / "previous/stop-data"
            previous_target = os.readlink(previous_pointer) if previous_pointer.is_symlink() else None
            calls = (root / "docker-calls").read_text() if (root / "docker-calls").exists() else ""
            probes = (root / "probes").read_text() if (root / "probes").exists() else ""
            image = (root / "image").read_text().strip()
        return result, target, calls, probes, image, rollback_target, previous_target

    def test_incomplete_coverage_blocks_switch_even_when_artifact_validation_passes(self):
        result, target, calls, _, _, _, _ = self.activate(coverage_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertEqual(calls, "")

    def test_unhealthy_rollback_blocks_switch(self):
        result, target, calls, _, _, _, _ = self.activate(rollback_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertNotIn("restart", calls)

    def test_expired_source_blocks_activation_before_any_production_mutation(self):
        result, target, calls, probes, _, _, _ = self.activate(source_freshness_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertEqual(calls, "")
        self.assertIn("source-freshness", probes)

    def test_activation_dry_run_never_switches_or_restarts(self):
        result, target, calls, _, _, rollback_target, _ = self.activate(dry_run=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target, "previous")
        self.assertEqual(calls, "")
        self.assertIsNone(rollback_target)

    def test_success_never_builds_or_restarts_route(self):
        result, target, calls, _, image, rollback_target, previous_target = self.activate(
            existing_rollback=True, existing_previous=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target, "releases/incremental/candidate")
        self.assertEqual(image, "image-original")
        self.assertIsNone(rollback_target)
        self.assertIsNone(previous_target)
        self.assertIn("rollback=NOT_RETAINED", result.stdout)
        for forbidden in ("build", "compose", "restart", "pull", "rename"):
            self.assertNotIn(forbidden, calls)

    def test_route_failure_rolls_back_and_repeats_readiness(self):
        result, target, calls, probes, _, rollback_target, _ = self.activate(route_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertIn("restore-route", probes)
        self.assertIn("--release-id previous", probes)
        self.assertNotIn("build", calls)
        self.assertIsNone(rollback_target)

    def test_failed_activation_restores_previous_rollback_pointer(self):
        result, target, _, _, _, rollback_target, previous_target = self.activate(
            route_fail=True, existing_rollback=True, existing_previous=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertEqual(rollback_target, "saved-rollback")
        self.assertEqual(previous_target, "saved-previous")

    def test_changed_stop_data_root_restarts_same_image_only(self):
        result, target, calls, probes, image, rollback_target, _ = self.activate(root_change=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target, "releases/incremental/candidate")
        self.assertEqual(image, "image-original")
        self.assertIsNone(rollback_target)
        self.assertIn("reload-route", probes)
        self.assertEqual(calls.count("restart routerecall-api"), 1)
        self.assertNotIn("build", calls)

    def test_changed_root_failure_reloads_previous_release_without_build(self):
        result, target, calls, probes, image, rollback_target, _ = self.activate(root_change=True, route_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertEqual(image, "image-original")
        self.assertIsNone(rollback_target)
        self.assertIn("--reload", probes)
        self.assertEqual(calls.count("restart routerecall-api"), 2)
        self.assertNotIn("build", calls)

    def test_static_gate_failure_rolls_back(self):
        result, target, _, probes, _, _, _ = self.activate(static_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertIn("--release-id previous", probes)

    def test_static_pipeline_failure_rolls_back(self):
        result, target, _, probes, _, _, _ = self.activate(pipeline_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertIn("restore-route", probes)

    def test_changed_image_fails_and_restores_previous_state(self):
        result, target, calls, _, image, rollback_target, _ = self.activate(image_change=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target, "previous")
        self.assertEqual(image, "image-original")
        self.assertNotIn("build", calls)
        self.assertIsNone(rollback_target)


if __name__ == "__main__":
    unittest.main()
