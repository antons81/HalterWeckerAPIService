#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_stop_packages import canonicalize, swiss_stops, write_stop_package


class SwissStopPackageTests(unittest.TestCase):
    def test_german_canonicalization_still_uses_parent_and_deduplicates(self) -> None:
        rows = [
            gtfs_stop("parent", "Central", "47.0000", "8.0000"),
            gtfs_stop("platform-a", "Central platform A", "47.0001", "8.0001", "parent"),
            gtfs_stop("platform-b", "Central platform B", "47.0001", "8.0001", "parent")
        ]

        stops = canonicalize(rows)

        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["id"], "parent")

    def test_swiss_mode_preserves_duplicate_name_coordinate_stop_ids(self) -> None:
        rows = [
            gtfs_stop("SLOID:ch:1", "Central", "47.0000", "8.0000"),
            gtfs_stop("legacy:2", "Central", "47.0000", "8.0000")
        ]

        stops = list(swiss_stops(rows))

        self.assertEqual([stop["id"] for stop in stops], ["SLOID:ch:1", "legacy:2"])
        self.assertEqual(len(stops), 2)

    def test_swiss_and_german_stops_use_the_same_package_schema(self) -> None:
        german = canonicalize([gtfs_stop("de-1", "Central", "47.0000", "8.0000")])
        swiss = list(swiss_stops([gtfs_stop("SLOID:ch:1", "Central", "47.0000", "8.0000")]))

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_stop_package(output, "german", german)
            write_stop_package(output, "swiss", swiss)
            german_package = json.loads((output / "german.json").read_text(encoding="utf-8"))
            swiss_package = json.loads((output / "swiss.json").read_text(encoding="utf-8"))

        expected_keys = {"id", "name", "latitude", "longitude", "searchName"}
        self.assertEqual(set(german_package[0]), expected_keys)
        self.assertEqual(set(swiss_package[0]), expected_keys)


def gtfs_stop(
    stop_id: str,
    name: str,
    latitude: str,
    longitude: str,
    parent_station: str = ""
) -> dict[str, str]:
    return {
        "stop_id": stop_id,
        "stop_name": name,
        "stop_lat": latitude,
        "stop_lon": longitude,
        "parent_station": parent_station
    }


if __name__ == "__main__":
    unittest.main()
