"""Board regressions for SQL limits, GTFS overflow, DST and indexed joins.

Created by Anton on 2026-10-10.
"""

import sqlite3
import io
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gtfs_board_window import departure_instant, service_day_epoch
from static_departures_api import Database, departure_datetime
from static_departures_runtime import ProviderMode, ProviderSnapshot
from test_static_departures_api import StaticDeparturesHTTPServer, write_database

CITY = "bad-homburg-v-d-hoehe-06434001"
STOP = "11746"


class BoardFixture:
    def __init__(self, root: Path, rows: list[tuple[str, str, str]], zone: str = "Europe/Berlin"):
        self.path = root / "legacy.sqlite"
        write_database(self.path, "board-regression")
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript("""
                DELETE FROM stop_times; DELETE FROM trips; DELETE FROM active_services;
                DELETE FROM city_stops;
                CREATE TABLE city_departure_modes (
                    city_id TEXT PRIMARY KEY, mode TEXT, timezone TEXT,
                    stop_id_prefix TEXT, identifier_prefix TEXT
                );
                CREATE INDEX raw_stops_by_canonical ON raw_stops(canonical_stop_id, stop_id);
                CREATE INDEX stop_times_by_stop_departure
                    ON stop_times(raw_stop_id, departure_seconds, trip_id, stop_sequence);
            """)
            connection.execute("UPDATE raw_stops SET stop_id=?,canonical_stop_id=? WHERE stop_id='stop-parent'", (STOP, STOP))
            connection.execute("UPDATE raw_stops SET parent_station=?,canonical_stop_id=? WHERE stop_id='stop-platform'", (STOP, STOP))
            connection.execute("INSERT INTO city_stops VALUES (?,?)", (CITY, STOP))
            connection.execute("INSERT INTO city_departure_modes VALUES (?,?,?,?,?)", (CITY, "canonical", zone, "", ""))
            for trip, day, clock in rows:
                hour, minute, second = map(int, clock.split(":"))
                connection.execute("INSERT INTO trips VALUES (?,?,?,?,?,?)", (trip, trip, "route-1", "Bad Homburg", "0", "terminal"))
                connection.execute("INSERT INTO active_services VALUES (?,?)", (trip, day))
                connection.execute("INSERT INTO stop_times VALUES (?,?,?,?,?)", (trip, "stop-platform", clock, hour * 3600 + minute * 60 + second, 1))
            connection.commit()
        temporal = root / "temporal.sqlite"
        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(temporal)) as target:
            target.execute("CREATE TABLE active_services(service_id TEXT, service_date TEXT, PRIMARY KEY(service_id,service_date))")
            target.executemany("INSERT INTO active_services VALUES (?,?)", source.execute("SELECT * FROM active_services"))
            target.commit()
        mode = ProviderMode("germany", CITY, "canonical", zone, "", "")
        catalog = SimpleNamespace(providers_for_city=lambda city: ("germany",), provider_mode=lambda city, provider: mode)
        reference = SimpleNamespace(structural=SimpleNamespace(database_path=self.path), temporal=SimpleNamespace(database_path=temporal, valid_from="2026-01-01", valid_through="2026-12-31"))
        snapshot = SimpleNamespace(manifest=SimpleNamespace(providers={"germany": reference}), catalog=catalog,
                                   trace_queries=False, connection_count=1, peak_provider_connections=0,
                                   _active_provider_ids=set(), release_id="board-regression")
        self.legacy = Database(str(self.path), ttl=0)
        self.shard = ProviderSnapshot(snapshot, "germany")
        self.zone = zone

    def close(self):
        self.legacy.close()
        self.shard.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def check(self, case, start, end, expected, limit=3):
        for name, backend in (("legacy", self.legacy), ("shard", self.shard)):
            with case.subTest(backend=name):
                values = backend.board(CITY, STOP, limit, start, end)
                case.assertEqual([row["tripID"] for row in values], expected)
                for row in values:
                    epoch = departure_datetime(row, self.zone).timestamp()
                    if start is not None:
                        case.assertGreaterEqual(epoch, start.timestamp())
                    if end is not None:
                        case.assertLessEqual(epoch, end.timestamp())


def instant(value):
    return datetime.fromisoformat(value)


class GTFSBoardWindowTests(unittest.TestCase):
    def test_bad_homburg_11746_filters_more_than_1000_previous_day_rows_before_limit(self):
        rows = [(f"old-{index:04d}", "20261009", "08:00:00") for index in range(1001)]
        rows += [("current", "20261010", "08:00:00"), ("later", "20261010", "09:00:00")]
        with tempfile.TemporaryDirectory() as directory, BoardFixture(Path(directory), rows) as fixture:
            start, end = instant("2026-10-10T07:00:00+02:00"), instant("2026-10-10T08:30:00+02:00")
            fixture.check(self, start, end, ["current"], limit=1)
            with StaticDeparturesHTTPServer(fixture.path) as api:
                query = urlencode(dict(cityID=CITY, stopID=STOP, limit=1, **{"from": start.isoformat(), "to": end.isoformat()}))
                payload = api.get("/static-departures/board?" + query)
                self.assertEqual([row["tripID"] for row in payload["departures"]], ["current"])
                self.assertEqual(payload["departures"][0]["serviceDate"], "2026-10-10")

    def test_overflow_and_service_dates_sort_by_absolute_departure(self):
        rows = [("overflow", "20261009", "25:00:00"), ("earlier", "20261010", "00:30:00"), ("multiday", "20261008", "50:00:00")]
        with tempfile.TemporaryDirectory() as directory, BoardFixture(Path(directory), rows) as fixture:
            fixture.check(self, instant("2026-10-10T00:00:00+02:00"), instant("2026-10-10T03:00:00+02:00"), ["earlier", "overflow", "multiday"])
            fixture.check(self, None, None, ["earlier", "overflow", "multiday"])

    def test_upper_only_lower_only_inclusive_and_microsecond_boundaries(self):
        rows = [("early", "20261010", "07:00:00"), ("exact", "20261010", "08:00:00"), ("late", "20261010", "09:00:00")]
        with tempfile.TemporaryDirectory() as directory, BoardFixture(Path(directory), rows) as fixture:
            boundary = instant("2026-10-10T08:00:00+02:00")
            fixture.check(self, boundary, boundary, ["exact"])
            fixture.check(self, boundary, None, ["exact", "late"])
            fixture.check(self, None, boundary, ["early", "exact"])
            fixture.check(self, instant("2026-10-10T08:00:00.000001+02:00"), None, ["late"])
            fixture.check(self, instant("2026-10-10T09:00:00+02:00"), instant("2026-10-10T07:00:00+02:00"), [])

    def test_utc_boundaries_resolve_provider_timezone(self):
        rows = [("new-york", "20261010", "00:15:00")]
        with tempfile.TemporaryDirectory() as directory, BoardFixture(Path(directory), rows, "America/New_York") as fixture:
            fixture.check(self, instant("2026-10-10T04:10:00+00:00"), instant("2026-10-10T04:20:00+00:00"), ["new-york"])

    def test_dst_spring_origin_can_precede_service_calendar_date(self):
        rows = [("before-midnight", "20260329", "00:30:00"), ("after-gap", "20260329", "03:15:00")]
        with tempfile.TemporaryDirectory() as directory, BoardFixture(Path(directory), rows) as fixture:
            fixture.check(self, instant("2026-03-28T23:20:00+01:00"), instant("2026-03-28T23:40:00+01:00"), ["before-midnight"])
            fixture.check(self, instant("2026-03-29T03:10:00+02:00"), instant("2026-03-29T03:20:00+02:00"), ["after-gap"])

    def test_dst_fall_repeated_hour_uses_distinct_absolute_instants(self):
        rows = [("first-fold", "20261025", "01:30:00"), ("second-fold", "20261025", "02:30:00")]
        with tempfile.TemporaryDirectory() as directory, BoardFixture(Path(directory), rows) as fixture:
            fixture.check(self, instant("2026-10-25T00:20:00+00:00"), instant("2026-10-25T00:40:00+00:00"), ["first-fold"])
            fixture.check(self, instant("2026-10-25T01:20:00+00:00"), instant("2026-10-25T01:40:00+00:00"), ["second-fold"])
            with StaticDeparturesHTTPServer(fixture.path) as api:
                query = urlencode(dict(cityID=CITY, stopID=STOP, **{"from": "2026-10-25T01:20:00Z", "to": "2026-10-25T01:40:00Z"}))
                self.assertEqual([row["tripID"] for row in api.get("/static-departures/board?" + query)["departures"]], ["second-fold"])

    def test_noon_origin_and_overflow_retain_real_dst_offset(self):
        self.assertEqual(departure_instant("20261025", "01:30:00", "Europe/Berlin").isoformat(), "2026-10-25T02:30:00+02:00")
        self.assertEqual(departure_instant("20261025", "02:30:00", "Europe/Berlin").isoformat(), "2026-10-25T02:30:00+01:00")
        self.assertEqual(departure_instant("20260328", "26:30:00", "Europe/Berlin").isoformat(), "2026-03-29T03:30:00+02:00")
        self.assertLessEqual(service_day_epoch.cache_info().maxsize, 4096)

    def test_board_sql_uses_stop_and_calendar_indexes(self):
        rows = [(f"trip-{index}", "20261010", "08:00:00") for index in range(1010)]
        with tempfile.TemporaryDirectory() as directory, BoardFixture(Path(directory), rows) as fixture:
            for backend in (fixture.legacy, fixture.shard):
                connection = backend._connection()
                statements = []
                connection.set_trace_callback(statements.append)
                backend.board(CITY, STOP, 1, instant("2026-10-10T07:00:00+02:00"), None)
                connection.set_trace_callback(None)
                sql = next(value for value in statements if "SELECT a.service_date,s.departure_time" in value)
                plan = [str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + sql)]
                self.assertTrue(any("raw_stops_by_canonical" in line for line in plan), plan)
                self.assertTrue(any("stop_times_by_stop_departure" in line for line in plan), plan)
                self.assertTrue(any("SEARCH a USING" in line for line in plan), plan)
                self.assertFalse(any(line.startswith(("SCAN s", "SCAN rs", "SCAN a")) for line in plan), plan)

    def test_parallel_board_reads_preserve_each_window_and_close_connections(self):
        rows = [("early", "20261010", "07:00:00"), ("late", "20261010", "09:00:00")]
        with tempfile.TemporaryDirectory() as directory, BoardFixture(Path(directory), rows) as fixture:
            def query(index):
                backend = fixture.legacy if index % 2 else fixture.shard
                clock, expected = ("07", "early") if index % 3 else ("09", "late")
                boundary = instant(f"2026-10-10T{clock}:00:00+02:00")
                self.assertEqual([row["tripID"] for row in backend.board(CITY, STOP, 1, boundary, boundary)], [expected])
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(query, range(40)))
            fixture.shard.close()
            self.assertEqual(fixture.shard._connections, {})

    def test_full_city_probe_rejects_departures_outside_requested_interval(self):
        self._check_probe_failure([
            {"serviceDate": "2026-10-09", "scheduledTime": "08:00:00"}
        ], "outside candidate board interval")

    def test_full_city_probe_rejects_nonchronological_departures(self):
        self._check_probe_failure([
            {"serviceDate": "2026-10-10", "scheduledTime": "09:00:00"},
            {"serviceDate": "2026-10-10", "scheduledTime": "08:00:00"}
        ], "not chronological")

    def _check_probe_failure(self, departures, message):
        from validate_shared_release_consumers import STATIC_CITY_PROBE
        class Response(io.BytesIO):
            status = 200
        plan = {"checks": [{"cityID": CITY, "stopID": STOP, "from": "2026-10-10T00:00:00", "to": "2026-10-10T23:59:59", "hasSchedule": True}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan))
            with patch.object(sys, "argv", ["probe", str(path)]), patch("urllib.request.urlopen", side_effect=lambda *_a, **_kw: Response(json.dumps({"departures": departures}).encode())), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, message):
                    exec(compile(STATIC_CITY_PROBE, "city-probe", "exec"), {})
