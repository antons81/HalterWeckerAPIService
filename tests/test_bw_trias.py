#!/usr/bin/env python3

import json
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_stop_packages import (
    SUPPORTED_TRANSIT_RADAR_ADAPTERS,
    load_cities,
    transit_radar_manifest,
    validate_transit_radar_provider,
    transit_radar_configurations,
)


BW_CITY_IDS = [
    "stuttgart", "karlsruhe", "ulm",
    "mannheim", "heidelberg", "freiburg-im-breisgau",
    "reutlingen", "heilbronn", "pforzheim",
    "konstanz", "baden-baden", "tuebingen",
]


class BWTriasAdapterTests(unittest.TestCase):
    def test_bw_trias_in_supported_adapters(self) -> None:
        self.assertIn("bwTrias", SUPPORTED_TRANSIT_RADAR_ADAPTERS)

    def test_bw_trias_passes_validation(self) -> None:
        config = {"adapter": "bwTrias"}
        validate_transit_radar_provider("mannheim", 49.48, 8.47, config)

    def test_bw_trias_rejects_region(self) -> None:
        config = {
            "adapter": "bwTrias",
            "region": {
                "minimumLongitude": 8.0,
                "minimumLatitude": 49.0,
                "maximumLongitude": 9.0,
                "maximumLatitude": 50.0,
            },
        }
        with self.assertRaises(ValueError):
            validate_transit_radar_provider("mannheim", 49.48, 8.47, config)

    def test_bw_trias_rejects_radar_stops(self) -> None:
        config = {
            "adapter": "bwTrias",
            "radarStops": [{"id": "test", "latitude": 49.48, "longitude": 8.47}],
        }
        with self.assertRaises(ValueError):
            validate_transit_radar_provider("mannheim", 49.48, 8.47, config)

    def test_bw_trias_rejects_efa_path(self) -> None:
        config = {"adapter": "bwTrias", "efaPath": "sl3-alone"}
        with self.assertRaises(ValueError):
            validate_transit_radar_provider("mannheim", 49.48, 8.47, config)

    def test_bw_trias_emits_provider_id_bw_trias(self) -> None:
        cities = [{
            "id": "test-city",
            "name": "Test City",
            "aliases": [],
            "latitude": 49.48,
            "longitude": 8.47,
            "radiusMeters": 15000,
            "transitRadar": [{"adapter": "bwTrias"}],
        }]
        manifest = transit_radar_manifest(cities, skip_auto_radar_stops=True)
        self.assertEqual(len(manifest["cities"]), 1)
        providers = manifest["cities"][0]["providers"]
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0]["providerID"], "bw-trias")

    def test_bw_trias_supports_departures(self) -> None:
        cities = [{
            "id": "test-city",
            "name": "Test City",
            "aliases": [],
            "latitude": 49.48,
            "longitude": 8.47,
            "radiusMeters": 15000,
            "transitRadar": [{"adapter": "bwTrias"}],
        }]
        manifest = transit_radar_manifest(cities, skip_auto_radar_stops=True)
        features = manifest["cities"][0]["providers"][0]["features"]
        self.assertIn("realtimeDepartures", features)
        self.assertIn("firstDepartures", features)
        self.assertIn("stopLookup", features)

    def test_bw_trias_does_not_support_live_vehicles(self) -> None:
        cities = [{
            "id": "test-city",
            "name": "Test City",
            "aliases": [],
            "latitude": 49.48,
            "longitude": 8.47,
            "radiusMeters": 15000,
            "transitRadar": [{"adapter": "bwTrias"}],
        }]
        manifest = transit_radar_manifest(cities, skip_auto_radar_stops=True)
        features = manifest["cities"][0]["providers"][0]["features"]
        self.assertNotIn("liveVehicles", features)


class BWTriasCityManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cities_path = Path(__file__).resolve().parents[1] / "config" / "cities.json"
        cls.cities = load_cities(cities_path)
        cls.manifest = transit_radar_manifest(cls.cities, skip_auto_radar_stops=True)
        cls.manifest_by_id = {
            city["appCityID"]: city for city in cls.manifest["cities"]
        }

    def test_all_12_bw_city_ids_present(self) -> None:
        for city_id in BW_CITY_IDS:
            self.assertIn(
                city_id, self.manifest_by_id,
                f"Missing BW city: {city_id}"
            )

    def test_all_12_bw_city_ids_appear_once(self) -> None:
        manifest_ids = [c["appCityID"] for c in self.manifest["cities"]]
        for city_id in BW_CITY_IDS:
            count = manifest_ids.count(city_id)
            self.assertEqual(
                count, 1,
                f"City ID {city_id} appears {count} times (expected 1)"
            )

    def test_stuttgart_retains_vvs_efa(self) -> None:
        stuttgart = self.manifest_by_id["stuttgart"]
        provider_ids = [p["providerID"] for p in stuttgart["providers"]]
        self.assertIn("vvs-efa-stuttgart", provider_ids)

    def test_stuttgart_has_bw_trias(self) -> None:
        stuttgart = self.manifest_by_id["stuttgart"]
        provider_ids = [p["providerID"] for p in stuttgart["providers"]]
        self.assertIn("bw-trias", provider_ids)

    def test_stuttgart_provider_order(self) -> None:
        stuttgart = self.manifest_by_id["stuttgart"]
        provider_ids = [p["providerID"] for p in stuttgart["providers"]]
        self.assertEqual(provider_ids[0], "vvs-efa-stuttgart")
        self.assertEqual(provider_ids[1], "bw-trias")

    def test_karlsruhe_retains_kvv_efa(self) -> None:
        karlsruhe = self.manifest_by_id["karlsruhe"]
        provider_ids = [p["providerID"] for p in karlsruhe["providers"]]
        self.assertIn("kvv-efa-karlsruhe", provider_ids)

    def test_karlsruhe_has_bw_trias(self) -> None:
        karlsruhe = self.manifest_by_id["karlsruhe"]
        provider_ids = [p["providerID"] for p in karlsruhe["providers"]]
        self.assertIn("bw-trias", provider_ids)

    def test_karlsruhe_provider_order(self) -> None:
        karlsruhe = self.manifest_by_id["karlsruhe"]
        provider_ids = [p["providerID"] for p in karlsruhe["providers"]]
        self.assertEqual(provider_ids[0], "kvv-efa-karlsruhe")
        self.assertEqual(provider_ids[1], "bw-trias")

    def test_ulm_retains_swu(self) -> None:
        ulm = self.manifest_by_id["ulm"]
        provider_ids = [p["providerID"] for p in ulm["providers"]]
        self.assertIn("swu-ulm", provider_ids)

    def test_ulm_has_bw_trias(self) -> None:
        ulm = self.manifest_by_id["ulm"]
        provider_ids = [p["providerID"] for p in ulm["providers"]]
        self.assertIn("bw-trias", provider_ids)

    def test_ulm_provider_order(self) -> None:
        ulm = self.manifest_by_id["ulm"]
        provider_ids = [p["providerID"] for p in ulm["providers"]]
        self.assertEqual(provider_ids[0], "swu-ulm")
        self.assertEqual(provider_ids[1], "bw-trias")

    def test_bw_trias_only_cities_departures_only(self) -> None:
        bw_only = [
            "mannheim", "heidelberg", "freiburg-im-breisgau",
            "reutlingen", "heilbronn", "pforzheim",
            "konstanz", "baden-baden", "tuebingen",
        ]
        for city_id in bw_only:
            city = self.manifest_by_id[city_id]
            providers = city["providers"]
            bw_provider = [p for p in providers if p["providerID"] == "bw-trias"]
            self.assertEqual(len(bw_provider), 1, f"{city_id}: expected 1 bw-trias provider")
            bw_p = bw_provider[0]
            self.assertIn("realtimeDepartures", bw_p["features"], f"{city_id}: bwTrias should support departures")
            self.assertNotIn("liveVehicles", bw_p["features"], f"{city_id}: bwTrias should not support live vehicles")

    def test_multi_provider_cities_capabilities_aggregated(self) -> None:
        multi = {
            "stuttgart": {"departures": True, "liveVehicles": True},
            "karlsruhe": {"departures": True, "liveVehicles": True},
            "ulm": {"departures": True, "liveVehicles": True},
        }
        for city_id, expected in multi.items():
            city = self.manifest_by_id[city_id]
            all_features = set()
            for p in city["providers"]:
                all_features.update(p["features"])
            if expected["departures"]:
                self.assertIn("realtimeDepartures", all_features, f"{city_id}: should have departures")
            if expected["liveVehicles"]:
                self.assertIn("liveVehicles", all_features, f"{city_id}: should have live vehicles")

    def test_no_duplicate_ids_or_aliases(self) -> None:
        ids = [c["appCityID"] for c in self.manifest["cities"]]
        self.assertEqual(len(ids), len(set(ids)), "Duplicate appCityIDs found")

    def test_schema_version(self) -> None:
        self.assertEqual(self.manifest["schemaVersion"], 1)

    def test_bw_city_names_match_ios(self) -> None:
        expected_names = {
            "stuttgart": "Stuttgart",
            "karlsruhe": "Karlsruhe",
            "ulm": "Ulm",
            "mannheim": "Mannheim",
            "heidelberg": "Heidelberg",
            "freiburg-im-breisgau": "Freiburg im Breisgau",
            "reutlingen": "Reutlingen",
            "heilbronn": "Heilbronn",
            "pforzheim": "Pforzheim",
            "konstanz": "Konstanz",
            "baden-baden": "Baden-Baden",
            "tuebingen": "Tübingen",
        }
        for city_id, expected_name in expected_names.items():
            city = self.manifest_by_id[city_id]
            self.assertEqual(
                city["name"], expected_name,
                f"{city_id}: expected name '{expected_name}', got '{city['name']}'"
            )

    def test_city_ids_match_ios_exactly(self) -> None:
        expected_ids = {
            "stuttgart": "stuttgart-de",
            "karlsruhe": "karlsruhe-de",
            "ulm": "ulm-de",
            "mannheim": "mannheim-de",
            "heidelberg": "heidelberg-de",
            "freiburg-im-breisgau": "freiburg-im-breisgau-de",
            "reutlingen": "reutlingen-de",
            "heilbronn": "heilbronn-de",
            "pforzheim": "pforzheim-de",
            "konstanz": "konstanz-de",
            "baden-baden": "baden-baden-de",
            "tuebingen": "tuebingen-de",
        }
        for app_id, expected_city_id in expected_ids.items():
            city = self.manifest_by_id[app_id]
            self.assertEqual(
                city["cityID"], expected_city_id,
                f"{app_id}: expected cityID '{expected_city_id}', got '{city['cityID']}'"
            )


class BWTriasConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cities_path = Path(__file__).resolve().parents[1] / "config" / "cities.json"
        cls.cities = load_cities(cities_path)

    def test_config_has_all_12_bw_cities(self) -> None:
        config_ids = {c["id"] for c in self.cities}
        for city_id in BW_CITY_IDS:
            self.assertIn(city_id, config_ids, f"config/cities.json missing {city_id}")

    def test_config_bw_cities_have_bw_trias(self) -> None:
        for city_id in BW_CITY_IDS:
            city = next(c for c in self.cities if c["id"] == city_id)
            tr = city.get("transitRadar")
            configs = transit_radar_configurations(tr)
            bw_configs = [c for c in configs if c.get("adapter") == "bwTrias"]
            self.assertEqual(
                len(bw_configs), 1,
                f"{city_id}: expected 1 bwTrias config, got {len(bw_configs)}"
            )

    def test_config_bw_trias_no_region(self) -> None:
        for city_id in BW_CITY_IDS:
            city = next(c for c in self.cities if c["id"] == city_id)
            configs = transit_radar_configurations(city["transitRadar"])
            bw = next(c for c in configs if c.get("adapter") == "bwTrias")
            self.assertNotIn("region", bw, f"{city_id}: bwTrias should not have region")

    def test_config_existing_adapters_preserved(self) -> None:
        expected = {
            "stuttgart": "vvsEFA",
            "karlsruhe": "kvvEFA",
            "ulm": "swu",
        }
        for city_id, adapter in expected.items():
            city = next(c for c in self.cities if c["id"] == city_id)
            configs = transit_radar_configurations(city["transitRadar"])
            adapter_configs = [c for c in configs if c.get("adapter") == adapter]
            self.assertEqual(
                len(adapter_configs), 1,
                f"{city_id}: expected {adapter} config preserved"
            )
            ac = adapter_configs[0]
            if adapter in ("vvsEFA", "kvvEFA"):
                self.assertIn("region", ac, f"{city_id}: {adapter} should have region")
                self.assertIn("radarStops", ac, f"{city_id}: {adapter} should have radarStops")

    def test_no_duplicate_config_ids(self) -> None:
        ids = [c["id"] for c in self.cities]
        self.assertEqual(len(ids), len(set(ids)), "Duplicate IDs in config/cities.json")


if __name__ == "__main__":
    unittest.main()
