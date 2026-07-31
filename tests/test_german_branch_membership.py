import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_stop_packages import (
    german_branch_cities,
    is_german_branch_city,
    load_cities,
    merge_manifest_entries,
    nl_city_ids,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class GermanBranchMembershipTests(unittest.TestCase):
    def test_shared_cities_json_excludes_netherlands_from_german_branch(self) -> None:
        cities = load_cities(REPOSITORY_ROOT / "config" / "cities.json")
        german_ids = {str(city["id"]) for city in german_branch_cities(cities)}
        dutch_ids = nl_city_ids(cities)

        self.assertIn("amsterdam", dutch_ids)
        self.assertNotIn("amsterdam", german_ids)
        self.assertTrue(dutch_ids.isdisjoint(german_ids))

        # Austrian packageMode cities stay out of the German branch.
        self.assertNotIn("wien", german_ids)
        self.assertNotIn("st-poelten", german_ids)

        # ÖBB-only radar cities in the shared file are not German packages.
        self.assertNotIn("graz", german_ids)

        # A normal German radar city remains.
        self.assertIn("wuppertal", german_ids)

    def test_amsterdam_does_not_collide_between_german_and_netherlands_branches(self) -> None:
        cities = load_cities(REPOSITORY_ROOT / "config" / "cities.json")
        german_ids = {str(city["id"]) for city in german_branch_cities(cities)}
        dutch_ids = nl_city_ids(cities)

        self.assertIn("amsterdam", dutch_ids)
        self.assertNotIn("amsterdam", german_ids)

        # Simulate effective branch membership: German claims only german_branch_cities.
        manifest: list[dict[str, object]] = []
        sources: dict[str, str] = {}
        merge_manifest_entries(
            manifest,
            [{"id": city_id} for city_id in sorted(german_ids)],
            source="German GTFS branch (config/cities.json)",
            sources_by_city_id=sources,
        )
        self.assertNotIn("amsterdam", sources)

        # Netherlands branch can safely register Amsterdam from the same shared file.
        merge_manifest_entries(
            manifest,
            [{"id": "amsterdam"}],
            source="Netherlands GTFS branch (config/cities.json)",
            sources_by_city_id=sources,
        )
        self.assertEqual(
            sources["amsterdam"],
            "Netherlands GTFS branch (config/cities.json)",
        )
        self.assertEqual(
            [entry["id"] for entry in manifest].count("amsterdam"),
            1,
        )

    def test_genuine_duplicate_effective_city_id_still_fails(self) -> None:
        manifest: list[dict[str, object]] = []
        sources: dict[str, str] = {}
        merge_manifest_entries(
            manifest,
            [{"id": "amsterdam"}],
            source="German GTFS branch (config/cities.json)",
            sources_by_city_id=sources,
        )
        with self.assertRaisesRegex(
            ValueError,
            "amsterdam.*German GTFS branch.*Netherlands GTFS branch",
        ):
            merge_manifest_entries(
                manifest,
                [{"id": "amsterdam"}],
                source="Netherlands GTFS branch (config/cities.json)",
                sources_by_city_id=sources,
            )

    def test_is_german_branch_city_helper(self) -> None:
        self.assertTrue(is_german_branch_city({
            "id": "koeln",
            "packageMode": "german",
            "transitRadar": {"adapter": "vrs"},
        }))
        self.assertFalse(is_german_branch_city({
            "id": "amsterdam",
            "transitRadar": {"adapter": "netherlands"},
        }))
        self.assertFalse(is_german_branch_city({
            "id": "wien",
            "packageMode": "austrian",
        }))
        self.assertFalse(is_german_branch_city({
            "id": "stockholm",
            "packageMode": "external",
            "transitRadar": {"adapter": "sweden", "operator": "sl"},
        }))
        self.assertFalse(is_german_branch_city({
            "id": "graz",
            "transitRadar": {"adapter": "oebb"},
        }))


if __name__ == "__main__":
    unittest.main()
