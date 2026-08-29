import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dynamic_resource_resolver import (  # noqa: E402
    resolve_ckan_gtfs_zip,
    resolve_realtime_manifest,
    resolve_wroclaw_gtfs_zip,
)


class DynamicResourceResolverTests(unittest.TestCase):
    def test_ckan_selects_newest_active_zip(self) -> None:
        payload = {
            "success": True,
            "result": {
                "id": "package-1",
                "license_title": "CC BY",
                "resources": [
                    {"id": "old", "state": "active", "format": "ZIP", "url": "https://example.test/old.zip", "last_modified": "2026-08-27T10:00:00"},
                    {"id": "new", "state": "active", "format": "ZIP", "url": "https://example.test/new.zip", "last_modified": "2026-08-28T10:00:00"},
                    {"id": "inactive", "state": "inactive", "format": "ZIP", "url": "https://example.test/inactive.zip", "last_modified": "2026-09-01T10:00:00"},
                ],
            },
        }
        with mock.patch(
            "dynamic_resource_resolver._read_metadata",
            return_value=json.dumps(payload).encode(),
        ):
            resolved = resolve_ckan_gtfs_zip(
                "https://example.test/package_show",
                package_id="package-1",
            )

        self.assertEqual(resolved.url, "https://example.test/new.zip")
        self.assertEqual(resolved.metadata["resourceID"], "new")

    def test_wroclaw_selects_newest_catalog_row(self) -> None:
        html = """
        <table>
          <tr><td>GTFS 2026-08-27 10:00:00</td><td><a href='/hdb/download/12/'>ZIP</a></td></tr>
          <tr><td>GTFS 2026-08-28 10:00:00</td><td><a href='/hdb/download/13/'>ZIP</a></td></tr>
        </table>
        """
        with mock.patch(
            "dynamic_resource_resolver._read_metadata",
            return_value=html.encode(),
        ):
            resolved = resolve_wroclaw_gtfs_zip("https://example.test/hdb/ft/6/")

        self.assertEqual(resolved.url, "https://example.test/hdb/download/13/")
        self.assertEqual(resolved.version, "2026-08-28 10:00:00")

    def test_manifest_normalizes_combined_and_component_keys(self) -> None:
        payload = {
            "GTFS-RT": {
                "All": "http://upstream.test/all",
                "Trip updates": "http://upstream.test/trips",
                "Vehicle positions": "http://upstream.test/vehicles",
            }
        }
        with mock.patch(
            "dynamic_resource_resolver._read_metadata",
            return_value=json.dumps(payload).encode(),
        ):
            resolved = resolve_realtime_manifest("https://example.test/manifest.json")

        self.assertEqual(
            resolved,
            {
                "combined": "http://upstream.test/all",
                "tripUpdates": "http://upstream.test/trips",
                "vehiclePositions": "http://upstream.test/vehicles",
            },
        )


if __name__ == "__main__":
    unittest.main()
