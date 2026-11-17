"""Created by Anton Stremovskiy on 17.11.2026.

Regression tests for the static API activation gate executed inside the consumer.
"""

import io
import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from scripts import active_release_readiness as readiness


class StaticReadinessTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 17, tzinfo=ZoneInfo("Europe/Vienna"))
        self.end = self.start + timedelta(days=1)
        self.line_times = {"U1": self.start + timedelta(hours=10), "U3": self.start + timedelta(hours=11)}
        self.unscheduled_stops = set()
        self.missing_sql_lines = set()
        self.missing_api_lines = set()
        self.empty_api_stops = set()
        self.requests = []
        self.release_id = "fixture-release"

    def expectation(self, database_path, city, stop, limit, required_lines):
        self.assertEqual(database_path, "/data/current-release/departures.sqlite")
        scheduled = stop not in self.unscheduled_stops
        lines = {line: self.line_times[line].timestamp() for line in required_lines
                 if line not in self.missing_sql_lines} if scheduled else {}
        return self.start, self.end, "Europe/Vienna", scheduled, 0.001, lines

    def request(self, url, **kwargs):
        self.requests.append(url)
        path = urlsplit(url).path
        if path.endswith("/health"):
            payload = {"database": {"releaseID": self.release_id}}
        elif path.endswith(".json"):
            payload = [{"id": "fixture-stop"}]
        else:
            parameters = parse_qs(urlsplit(url).query)
            stop = parameters["stopID"][0]
            start = datetime.fromisoformat(parameters["from"][0])
            end = datetime.fromisoformat(parameters["to"][0])
            departures = [] if stop in self.empty_api_stops or stop in self.unscheduled_stops else [
                {"line": line, "when": value.isoformat()} for line, value in self.line_times.items()
                if line not in self.missing_api_lines and start <= value <= end
            ]
            payload = {"departures": departures}
        response = io.BytesIO(json.dumps(payload).encode())
        response.status = 200
        return response

    def run_probe(self):
        # Execute the real embedded HTTP gate; SQL expectation has separate integration
        # coverage in test_nightly_activation using an actual SQLite fixture.
        self.assertTrue(readiness.STATIC_PROBE.startswith(readiness.LEGACY_SMOKE_EXPECTATION))
        probe = readiness.STATIC_PROBE[len(readiness.LEGACY_SMOKE_EXPECTATION):]
        api = types.ModuleType("static_departures_api")
        api.departure_datetime = lambda row, zone: datetime.fromisoformat(row["when"])
        namespace = {"smoke_expectation": self.expectation, "datetime": datetime,
                     "timedelta": timedelta, "ZoneInfo": ZoneInfo}
        environment = {"DEPARTURES_DATABASE": "/data/current-release/departures.sqlite",
                       "HALTEWECKER_STATIC_DEPARTURES_PROVIDER_IDS": ""}
        with mock.patch.dict(sys.modules, {"static_departures_api": api}), \
                mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(sys, "argv", ["probe", "fixture-release", ""]), \
                mock.patch("urllib.request.urlopen", side_effect=self.request), mock.patch("builtins.print"):
            exec(compile(probe, "<static-readiness-probe>", "exec"), namespace)

    def test_success_requires_vienna_and_both_u1_u3_api_matches(self):
        self.run_probe()
        route_requests = [url for url in self.requests if "limit=100" in url]
        self.assertEqual(len(route_requests), 2)

    def test_missing_required_vienna_schedule_fails_for_each_stop(self):
        for stop in ("at:49:1876:0:1", "at:49:1320:5"):
            with self.subTest(stop=stop):
                self.unscheduled_stops = {stop}
                with self.assertRaisesRegex(RuntimeError, "missing required Vienna departures"):
                    self.run_probe()

    def test_missing_either_required_sql_line_fails(self):
        for missing in ({"U1"}, {"U3"}, {"U1", "U3"}):
            with self.subTest(missing=missing):
                self.missing_sql_lines = missing
                with self.assertRaisesRegex(RuntimeError, "missing required Vienna scheduled lines"):
                    self.run_probe()

    def test_empty_vienna_api_board_fails_even_with_valid_sql_schedule(self):
        self.empty_api_stops = {"at:49:1320:5"}
        with self.assertRaisesRegex(RuntimeError, "empty departures response"):
            self.run_probe()

    def test_missing_required_api_line_fails(self):
        self.missing_api_lines = {"U3"}
        with self.assertRaisesRegex(RuntimeError, "scheduled Wien line is missing from API board: U3"):
            self.run_probe()

    def test_unscheduled_optional_helsinki_stop_keeps_existing_behavior(self):
        self.unscheduled_stops = {"fi-hsl:1040401"}
        self.run_probe()

    def test_wrong_release_still_fails_before_departures(self):
        self.release_id = "stale-release"
        with self.assertRaisesRegex(RuntimeError, "runtime release mismatch"):
            self.run_probe()


if __name__ == "__main__":
    unittest.main()
