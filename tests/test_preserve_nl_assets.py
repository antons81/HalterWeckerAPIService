#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from preserve_nl_assets import preserve_nl_assets


class PreserveNLAssetsTests(unittest.TestCase):
    def test_preserves_only_configured_dutch_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current, output = root / "current", root / "output"
            for directory in (current / "stops", current / "routes", current / "departures", output / "stops"):
                directory.mkdir(parents=True, exist_ok=True)
            old_city = {"id": "amsterdam", "name": "Amsterdam", "url": "stops/amsterdam.json"}
            (current / "manifest.json").write_text(json.dumps({"cities": [old_city]}), encoding="utf-8")
            (output / "manifest.json").write_text(json.dumps({"cities": []}), encoding="utf-8")
            (current / "stops" / "amsterdam.json").write_text("[]", encoding="utf-8")
            (current / "routes" / "amsterdam.json").write_text("{}", encoding="utf-8")
            (current / "departures" / "amsterdam.json").write_text("{}", encoding="utf-8")
            cities = [{"id": "amsterdam", "name": "Amsterdam", "aliases": [], "latitude": 52.37, "longitude": 4.89, "radiusMeters": 1, "transitRadar": {"adapter": "netherlands"}}]
            cities_path = root / "cities.json"
            cities_path.write_text(json.dumps(cities), encoding="utf-8")
            preserve_nl_assets(current, output, cities_path)
            self.assertTrue((output / "stops" / "amsterdam.json").is_file())
            self.assertTrue((output / "departures" / "amsterdam.json").is_file())
            self.assertEqual(json.loads((output / "cities.json").read_text())[0]["id"], "amsterdam")


if __name__ == "__main__":
    unittest.main()
