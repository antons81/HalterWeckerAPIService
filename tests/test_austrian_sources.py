import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from austrian_sources import load_austrian_sources, public_stop_id
from build_stop_packages import build_austrian_stop_packages_from_sources
from validate_austrian_stop_packages import validate


class AustrianSourceRegistryTests(unittest.TestCase):
    def test_registry_covers_eight_cities_and_linz_has_two_feeds(self) -> None:
        sources = load_austrian_sources(Path(__file__).resolve().parents[1] / "config" / "austrian-sources.json")
        self.assertEqual({city for source in sources for city in source["cities"]}, {
            "wien", "graz", "linz", "salzburg", "innsbruck", "klagenfurt", "st-poelten", "bregenz"
        })
        self.assertEqual([source["id"] for source in sources if "linz" in source["cities"]], ["ooevv", "linz-ag"])

    def test_regional_prefixes_do_not_collide_and_vor_ids_remain_compatible(self) -> None:
        sources = load_austrian_sources(Path(__file__).resolve().parents[1] / "config" / "austrian-sources.json")
        prefixes = [str(source["identifierPrefix"]) for source in sources]
        self.assertEqual(len(prefixes), len(set(prefixes)))
        vor = next(source for source in sources if source["id"] == "vor")
        steiermark = next(source for source in sources if source["id"] == "steiermark")
        self.assertEqual(public_stop_id(vor, "at:49:1349:23"), "at:49:1349:23")
        self.assertEqual(public_stop_id(steiermark, "123"), "steiermark:123")

    def test_all_city_packages_are_nonempty_and_validate(self) -> None:
        cities = [
            {"id": city, "name": city, "latitude": 48.0, "longitude": 16.0, "radiusMeters": 100_000, "packageMode": "austrian"}
            for city in ("wien", "graz", "linz", "salzburg", "innsbruck", "klagenfurt", "st-poelten", "bregenz")
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archives = {}
            for source_id in ("vor", "steiermark", "salzburg", "kaernten", "ooevv", "tirol", "vorarlberg", "linz-ag"):
                path = root / f"{source_id}.zip"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n1,Test,48.0,16.0\n")
                archives[source_id] = zipfile.ZipFile(path)
            output = root / "output"
            registry = load_austrian_sources(Path(__file__).resolve().parents[1] / "config" / "austrian-sources.json")
            build_austrian_stop_packages_from_sources(archives, cities, output, registry)
            # Validation uses the production registry and therefore checks all package files.
            counts = validate(output, Path(__file__).resolve().parents[1] / "config" / "austrian-sources.json")
            self.assertEqual(set(counts), {city["id"] for city in cities})
            self.assertTrue(all(count > 0 for count in counts.values()))
            for archive in archives.values():
                archive.close()


if __name__ == "__main__":
    unittest.main()
