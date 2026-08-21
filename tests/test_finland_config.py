import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from external_gtfs import (  # noqa: E402
    authenticated_external_request,
    load_external_cities,
    load_external_gtfs_sources,
    validate_external_gtfs_source,
)


class FinlandConfigTests(unittest.TestCase):
    repository_root = Path(__file__).resolve().parents[1]

    def test_finland_registry_contains_only_selected_gtfs_feeds(self) -> None:
        sources = load_external_gtfs_sources(
            self.repository_root / "config" / "external-gtfs-sources.json"
        )
        finland = [source for source in sources if str(source["id"]).startswith("finland-")]
        self.assertEqual(len(finland), 26)
        configured_urls = {str(source["url"]) for source in finland}
        self.assertTrue(all("/finland/" in url and url.endswith(".zip") for url in configured_urls))
        excluded = {"Viro", "MATKA", "flixbus", "02Taksi", "CAR_FERRIES", "VR_bussit"}
        self.assertFalse(any(any(name in url for name in excluded) for url in configured_urls))
        self.assertTrue(all(source["country"] == "FI" for source in finland))

    def test_provider_filtered_city_assignments_are_nonempty(self) -> None:
        sources = load_external_gtfs_sources(
            self.repository_root / "config" / "external-gtfs-sources.json"
        )
        finland = [source for source in sources if str(source["id"]).startswith("finland-")]
        for source in finland:
            cities = load_external_cities(source, self.repository_root)
            self.assertTrue(cities, source["id"])
            self.assertTrue(all(source["id"] in city["externalGTFSProviders"] for city in cities))

    def test_finland_sources_pass_registry_validation(self) -> None:
        registry = json.loads(
            (self.repository_root / "config" / "external-gtfs-sources.json").read_text()
        )
        known_ids = set()
        known_prefixes = set()
        known_namespaces = set()
        for source in registry:
            validate_external_gtfs_source(
                source,
                self.repository_root,
                known_source_ids=known_ids,
                known_prefixes=known_prefixes,
                known_namespaces=known_namespaces,
            )
            known_ids.add(str(source["id"]))
            known_prefixes.add(str(source.get("identifierPrefix", "")))
            known_namespaces.add(str(source.get("namespace", "")))

    def test_finland_auth_uses_header_without_logging_or_url_secret(self) -> None:
        url, headers = authenticated_external_request(
            "finland-hsl",
            "https://api.digitransit.fi/routing-data/v3/finland/HSL-gtfs.zip",
            {"DIGITRANSIT_KEY": "test-secret"},
        )
        self.assertNotIn("test-secret", url)
        self.assertEqual(headers["digitransit-subscription-key"], "test-secret")
        with self.assertRaisesRegex(ValueError, "DIGITRANSIT_KEY"):
            authenticated_external_request(
                "finland-hsl",
                url,
                {},
            )


if __name__ == "__main__":
    unittest.main()
