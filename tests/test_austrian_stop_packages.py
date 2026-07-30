import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_stop_packages import build_austrian_stop_packages, load_cities, transit_radar_manifest


class AustrianStopPackageTests(unittest.TestCase):
    def test_oebb_manifest_has_departures_without_live_vehicle_radar(self) -> None:
        cities = load_cities(Path(__file__).resolve().parents[1] / "config" / "cities.json")
        manifest = transit_radar_manifest(cities, skip_auto_radar_stops=True)
        wien = next(city for city in manifest["cities"] if city["appCityID"] == "wien")
        self.assertEqual(wien["cityID"], "wien-at")
        self.assertEqual(wien["providers"][0]["providerID"], "oebb")
        self.assertFalse(wien["providers"][0]["isExperimental"])
        self.assertEqual(wien["providers"][0]["features"], ["realtimeDepartures", "firstDepartures", "stopLookup", "realtimeDelay"])
        self.assertNotIn("liveVehicles", wien["providers"][0]["features"])

    def test_austrian_gtfs_mode_writes_radius_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "austria.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n8103000,Wien Hbf,48.1855,16.3753\n")
            with zipfile.ZipFile(archive_path) as archive:
                manifest = build_austrian_stop_packages(archive, [{
                    "id": "wien", "name": "Wien", "aliases": [],
                    "latitude": 48.2082, "longitude": 16.3738,
                    "radiusMeters": 5_000, "packageMode": "austrian"
                }], Path(temp) / "out")

            self.assertEqual(manifest[0]["stopCount"], 1)
            payload = json.loads((Path(temp) / "out" / "stops" / "wien.json").read_text())
            self.assertEqual(payload[0]["id"], "8103000")


if __name__ == "__main__":
    unittest.main()
