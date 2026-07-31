import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_stop_packages import load_gtfs_archive  # noqa: E402
from external_gtfs import (  # noqa: E402
    build_external_departure_index,
    build_external_lines,
    build_external_route_index,
    build_external_stop_packages,
)


ABBORRSJON = "11706"
PLATFORM_1 = "9022050011706001"
PLATFORM_2 = "9022050011706002"
ENTRANCE = "9022050011706003"
ORPHAN = "902200109999001"
BROKEN_ORPHAN = "902200109999002"


def _abborrsjon_zip(path: Path) -> Path:
    """Feed centred on Abborrsjön with a parent station, two platforms,
    an entrance, an orphan platform, and an orphan whose parent is missing."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type,platform_code\n"
            f"{ABBORRSJON},Abborrsjön,59.0,18.0,,1,\n"
            f"{PLATFORM_1},Abborrsjön spår 1,59.001,18.001,{ABBORRSJON},0,1\n"
            f"{PLATFORM_2},Abborrsjön spår 2,58.999,18.002,{ABBORRSJON},0,2\n"
            f"{ENTRANCE},Abborrsjön ingång,59.001,18.000,{ABBORRSJON},2,\n"
            f"{ORPHAN},Orphan Platform,59.1,18.1,,0,\n"
            f"{BROKEN_ORPHAN},Broken Orphan,59.2,18.2,999999,0,\n"
        )
        archive.writestr(
            "routes.txt",
            "route_id,route_short_name,route_long_name,route_type\n"
            "R1,28,,0\n"
            "R2,29,,1\n"
        )
        archive.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,trip_headsign,direction_id\n"
            "R1,S1,T1,Towards Stockholm,0\n"
            "R2,S1,T2,Towards Stockholm,0\n"
        )
        archive.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            f"T1,07:55:00,08:00:00,{PLATFORM_1},1\n"
            f"T1,07:55:00,08:00:00,{PLATFORM_1},1\n"
            f"T1,08:15:00,08:15:00,{ORPHAN},2\n"
            f"T2,08:55:00,09:00:00,{PLATFORM_2},1\n"
            f"T2,09:10:00,09:10:00,{BROKEN_ORPHAN},2\n"
        )
        archive.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
            "start_date,end_date\n"
            "S1,1,1,1,1,1,1,1,20200101,20301231\n"
        )
    return path


CITY = {
    "id": "stockholm",
    "name": "Stockholm",
    "aliases": [],
    "latitude": 59.05,
    "longitude": 18.05,
    "radiusMeters": 30000,
    "packageMode": "external",
}


class ExternalStopHierarchyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive_path = _abborrsjon_zip(self.root / "se.zip")
        self.out = self.root / "out"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _build(self) -> dict:
        with zipfile.ZipFile(self.archive_path) as archive:
            manifest, package_stops = build_external_stop_packages(
                archive, [CITY], self.out, stop_id_mode="exact"
            )
            build_external_route_index(archive, [CITY], self.out)
            build_external_departure_index(
                archive, [CITY], self.out, timezone_name="Europe/Stockholm"
            )
            lines = build_external_lines(archive, package_stops)
        return {
            "manifest": manifest,
            "package_stops": package_stops,
            "lines": lines,
        }

    def test_abborrsjon_is_one_public_stop_not_three(self) -> None:
        result = self._build()
        ids = {stop["id"] for stop in result["package_stops"]["stockholm"]}
        self.assertIn(ABBORRSJON, ids)
        self.assertNotIn(PLATFORM_1, ids)
        self.assertNotIn(PLATFORM_2, ids)
        abborrsjon = [
            stop for stop in result["package_stops"]["stockholm"]
            if stop["id"] == ABBORRSJON
        ]
        self.assertEqual(len(abborrsjon), 1)
        self.assertEqual(abborrsjon[0]["name"], "Abborrsjön")
        self.assertEqual(result["manifest"][0]["stopCount"], 3)

    def test_both_platform_departures_under_parent(self) -> None:
        result = self._build()
        departures = json.loads(
            (self.out / "departures" / "stockholm.json").read_text(encoding="utf-8")
        )
        board = departures["stops"][ABBORRSJON]
        by_trip = {item["t"]: item for item in board}
        t1 = by_trip["T1"]
        self.assertEqual(t1["r"], "R1")
        self.assertEqual(t1["p"], "08:00:00")
        self.assertEqual(t1["h"], "Towards Stockholm")
        self.assertEqual(t1["s"], PLATFORM_1)
        self.assertEqual(t1["platform"], "1")
        t2 = by_trip["T2"]
        self.assertEqual(t2["r"], "R2")
        self.assertEqual(t2["p"], "09:00:00")
        self.assertEqual(t2["s"], PLATFORM_2)
        self.assertEqual(t2["platform"], "2")
        t1_rows = [item for item in board if item["t"] == "T1"]
        self.assertEqual(len(t1_rows), 1)

    def test_child_ids_available_for_realtime_mapping(self) -> None:
        result = self._build()
        departures = json.loads(
            (self.out / "departures" / "stockholm.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            departures["platforms"][ABBORRSJON],
            [PLATFORM_1, PLATFORM_2],
        )
        self.assertEqual(
            departures["platforms"].get(ORPHAN),
            None,
        )

    def test_orphan_platforms_remain_selectable(self) -> None:
        result = self._build()
        ids = {stop["id"] for stop in result["package_stops"]["stockholm"]}
        self.assertIn(ORPHAN, ids)
        self.assertIn(BROKEN_ORPHAN, ids)
        departures = json.loads(
            (self.out / "departures" / "stockholm.json").read_text(encoding="utf-8")
        )
        orphan_board = departures["stops"][ORPHAN]
        self.assertEqual(orphan_board[0]["t"], "T1")
        self.assertNotIn("s", orphan_board[0])
        self.assertNotIn("platform", orphan_board[0])
        broken_board = departures["stops"][BROKEN_ORPHAN]
        self.assertEqual(broken_board[0]["t"], "T2")

    def test_entrances_are_excluded(self) -> None:
        result = self._build()
        ids = {stop["id"] for stop in result["package_stops"]["stockholm"]}
        self.assertNotIn(ENTRANCE, ids)
        departures = json.loads(
            (self.out / "departures" / "stockholm.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(ENTRANCE, departures["stops"])

    def test_all_departure_stop_references_exist_in_package(self) -> None:
        result = self._build()
        package_ids = {
            stop["id"] for stop in result["package_stops"]["stockholm"]
        }
        departures = json.loads(
            (self.out / "departures" / "stockholm.json").read_text(encoding="utf-8")
        )
        self.assertTrue(departures["stops"].keys() <= package_ids)
        for stop_id, board in departures["stops"].items():
            for item in board:
                self.assertIn(stop_id, package_ids)
                self.assertNotEqual(item.get("s"), stop_id)
        self.assertTrue(departures["platforms"].keys() <= package_ids)

    def test_exact_duplicate_rows_are_removed_globally(self) -> None:
        result = self._build()
        departures = json.loads(
            (self.out / "departures" / "stockholm.json").read_text(encoding="utf-8")
        )
        seen: set[tuple] = set()
        duplicates: list[tuple] = []
        for board in departures["stops"].values():
            for item in board:
                identity = tuple(sorted(item.items()))
                if identity in seen:
                    duplicates.append(item)
                seen.add(identity)
        self.assertEqual(duplicates, [])
        t1_rows = [
            item for item in departures["stops"][ABBORRSJON]
            if item["t"] == "T1"
        ]
        self.assertEqual(len(t1_rows), 1)

    def test_all_route_references_remain_valid(self) -> None:
        result = self._build()
        departures = json.loads(
            (self.out / "departures" / "stockholm.json").read_text(encoding="utf-8")
        )
        routes = json.loads(
            (self.out / "routes" / "stockholm.json").read_text(encoding="utf-8")
        )
        departure_route_ids = {
            item["r"]
            for board in departures["stops"].values()
            for item in board
        }
        self.assertIn("R1", departure_route_ids)
        self.assertIn("R2", departure_route_ids)
        self.assertTrue(departure_route_ids <= routes.keys())
        for route_id in departure_route_ids:
            self.assertNotEqual(routes[route_id]["short_name"], "")
        self.assertIn("R1", result["lines"][ABBORRSJON])
        self.assertIn("R2", result["lines"][ABBORRSJON])


class ExternalDepartureDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / "out"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _build(self, stop_times: str) -> tuple[list[dict], dict]:
        archive_path = self.root / "dedup.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                "stops.txt",
                "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type,platform_code\n"
                "11706,Abborrsjön,59.0,18.0,,1,\n"
                "9022050011706001,Abborrsjön spår 1,59.001,18.001,11706,0,1\n"
                "9022050011706002,Abborrsjön spår 2,58.999,18.002,11706,0,2\n"
            )
            archive.writestr(
                "routes.txt",
                "route_id,route_short_name,route_long_name,route_type\nR1,28,,0\n",
            )
            archive.writestr(
                "trips.txt",
                "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                "R1,S1,T1,Towards Stockholm,0\n"
                "R1,S1,T2,Towards Stockholm,0\n",
            )
            archive.writestr("stop_times.txt", stop_times)
            archive.writestr(
                "calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                "start_date,end_date\n"
                "S1,1,1,1,1,1,1,1,20200101,20301231\n",
            )
        cities = [{
            "id": "stockholm",
            "name": "Stockholm",
            "aliases": [],
            "latitude": 59.0,
            "longitude": 18.0,
            "radiusMeters": 30000,
            "packageMode": "external",
        }]
        with zipfile.ZipFile(archive_path) as archive:
            build_external_stop_packages(archive, cities, self.out, stop_id_mode="exact")
            build_external_route_index(archive, cities, self.out)
            build_external_departure_index(
                archive, cities, self.out, timezone_name="Europe/Stockholm"
            )
        departures = json.loads(
            (self.out / "departures" / "stockholm.json").read_text(encoding="utf-8")
        )
        routes = json.loads(
            (self.out / "routes" / "stockholm.json").read_text(encoding="utf-8")
        )
        return departures, routes

    def test_exact_duplicates_collapse_to_one_row(self) -> None:
        stop_times = (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,07:55:00,08:00:00,9022050011706001,1\n"
            "T1,07:55:00,08:00:00,9022050011706001,1\n"
        )
        departures, _ = self._build(stop_times)
        board = departures["stops"]["11706"]
        self.assertEqual(len(board), 1)
        self.assertEqual(board[0]["t"], "T1")
        self.assertEqual(board[0]["s"], "9022050011706001")
        self.assertEqual(board[0]["platform"], "1")

    def test_same_trip_time_on_different_platforms_keeps_two_rows(self) -> None:
        stop_times = (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,07:55:00,08:00:00,9022050011706001,1\n"
            "T1,08:00:00,08:00:00,9022050011706002,2\n"
        )
        departures, _ = self._build(stop_times)
        board = departures["stops"]["11706"]
        self.assertEqual(len(board), 2)
        platforms = {item["platform"] for item in board}
        self.assertEqual(platforms, {"1", "2"})
        child_ids = {item["s"] for item in board}
        self.assertEqual(child_ids, {"9022050011706001", "9022050011706002"})

    def test_same_route_time_with_different_trips_keeps_two_rows(self) -> None:
        stop_times = (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,07:55:00,08:00:00,9022050011706001,1\n"
            "T2,07:55:00,08:00:00,9022050011706001,1\n"
        )
        departures, _ = self._build(stop_times)
        board = departures["stops"]["11706"]
        self.assertEqual(len(board), 2)
        self.assertEqual({item["t"] for item in board}, {"T1", "T2"})

    def test_ordering_remains_stable_first_occurrence_kept(self) -> None:
        stop_times = (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,07:55:00,08:00:00,9022050011706001,1\n"
            "T1,07:55:00,08:00:00,9022050011706001,1\n"
            "T1,08:00:00,08:00:00,9022050011706002,2\n"
            "T2,07:55:00,08:00:00,9022050011706001,1\n"
        )
        departures, _ = self._build(stop_times)
        board = departures["stops"]["11706"]
        self.assertEqual(
            [(item["t"], item["s"], item.get("platform")) for item in board],
            [
                ("T1", "9022050011706001", "1"),
                ("T1", "9022050011706002", "2"),
                ("T2", "9022050011706001", "1"),
            ],
        )

    def test_references_remain_valid_after_dedup(self) -> None:
        stop_times = (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,07:55:00,08:00:00,9022050011706001,1\n"
            "T1,07:55:00,08:00:00,9022050011706001,1\n"
            "T2,08:00:00,08:00:00,9022050011706002,2\n"
        )
        departures, routes = self._build(stop_times)
        package_ids = {
            stop["id"]
            for stop in json.loads(
                (self.out / "stops" / "stockholm.json").read_text(encoding="utf-8")
            )
        }
        self.assertTrue(departures["stops"].keys() <= package_ids)
        for board in departures["stops"].values():
            for item in board:
                self.assertIn(item["r"], routes)
        self.assertEqual(len(departures["stops"]["11706"]), 2)


if __name__ == "__main__":
    unittest.main()
