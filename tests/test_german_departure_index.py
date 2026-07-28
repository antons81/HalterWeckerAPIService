#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_german_departure_index import build_german_departure_index, parse_gtfs_time, validate_departure_output


class GermanDepartureIndexTests(unittest.TestCase):
    def test_accepts_gtfs_times_after_midnight(self) -> None:
        self.assertEqual(parse_gtfs_time("24:15:30"), 87_330)
        self.assertIsNone(parse_gtfs_time("24:60:00"))

    def test_generates_and_validates_canonical_city_files(self) -> None:
        city_ids = ["neukieritzsch-14729320", "dresden", "munster", "munster-03358016", "koln"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "stops").mkdir()
            manifest_cities = []
            for index, city_id in enumerate(city_ids):
                stop_id = f"parent-{index}"
                (root / "stops" / f"{city_id}.json").write_text(json.dumps([{"id": stop_id, "name": city_id}]), encoding="utf-8")
                manifest_cities.append({"id": city_id, "url": f"stops/{city_id}.json"})
            (root / "manifest.json").write_text(json.dumps({"cities": manifest_cities}), encoding="utf-8")
            archive_path = root / "gtfs.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("stops.txt", "stop_id,stop_name,parent_station,platform_code\n" + "\n".join(f"platform-{index},{city_id},parent-{index},{index + 1}\nparent-{index},{city_id},," for index, city_id in enumerate(city_ids)))
                archive.writestr("routes.txt", "route_id,route_short_name,route_long_name\nr1,S1,Route One\n")
                archive.writestr("trips.txt", "route_id,service_id,trip_id,trip_headsign,direction_id\nr1,weekday,t1,Destination,0\n")
                archive.writestr("calendar.txt", "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nweekday,1,1,1,1,1,1,1,20000101,20991231\n")
                archive.writestr("calendar_dates.txt", "service_id,date,exception_type\n")
                archive.writestr("stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" + "\n".join(f"t1,24:15:00,24:15:00,platform-{index},{index + 1}" for index in range(len(city_ids))))
            with zipfile.ZipFile(archive_path) as archive:
                build_german_departure_index(archive, root, days=2, aliases={"koeln": "koln"})
            validate_departure_output(root, {"koeln": "koln"})
            output = json.loads((root / "departures" / "neukieritzsch-14729320.json").read_text())
            departure = output["stops"]["parent-0"][0]
            self.assertEqual(departure["departureTime"], "24:15:00")
            self.assertEqual(departure["platform"], "1")
            manifest = json.loads((root / "departures-manifest.json").read_text())
            self.assertEqual(manifest["cityIDAliases"], {"koeln": "koln"})
            self.assertEqual({item["cityID"] for item in manifest["cities"]}, set(city_ids))


if __name__ == "__main__":
    unittest.main()
