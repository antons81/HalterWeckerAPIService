import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from common_catalog import build_common_catalog  # noqa: E402


def _providers() -> dict[str, dict[str, object]]:
    return {
        "israel-mot": {
            "status": "active",
            "providerOrder": 0,
            "releaseID": "release-a",
            "structural": {"artifactKey": "israel-structural", "schemaVersion": 2},
            "temporal": {
                "artifactKey": "israel-temporal",
                "schemaVersion": 2,
                "validFrom": "2026-09-12",
                "validThrough": "2026-09-26",
            },
        },
        "ttc-surface": {
            "status": "active",
            "providerOrder": 1,
            "releaseID": "release-a",
            "structural": {"artifactKey": "surface-structural", "schemaVersion": 2},
            "temporal": {
                "artifactKey": "surface-temporal",
                "schemaVersion": 2,
                "validFrom": "2026-09-13",
                "validThrough": "2026-09-27",
            },
        },
        "ttc-subway": {
            "status": "active",
            "providerOrder": 2,
            "releaseID": "release-a",
            "structural": {"artifactKey": "subway-structural", "schemaVersion": 2},
            "temporal": {
                "artifactKey": "subway-temporal",
                "schemaVersion": 2,
                "validFrom": "2026-09-14",
                "validThrough": "2026-09-28",
            },
        },
    }


class CommonCatalogTests(unittest.TestCase):
    def _build(
        self,
        output: Path,
        aliases: list[tuple[str, str]],
        providers: dict[str, dict[str, object]] | None = None,
    ):
        return build_common_catalog(
            output,
            release_id="release-a",
            providers=providers or _providers(),
            aliases=aliases,
        )

    def test_nested_temporal_metadata_is_written_to_provider_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "common.sqlite"
            self._build(output, [])

            with sqlite3.connect(output) as connection:
                rows = connection.execute(
                    "SELECT provider_id, valid_from, valid_through "
                    "FROM provider_registry ORDER BY provider_id"
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    ("israel-mot", "2026-09-12", "2026-09-26"),
                    ("ttc-subway", "2026-09-14", "2026-09-28"),
                    ("ttc-surface", "2026-09-13", "2026-09-27"),
                ],
            )

    def test_missing_temporal_block_fails_closed(self) -> None:
        providers = _providers()
        del providers["israel-mot"]["temporal"]
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "provider=israel-mot needs structural"):
                self._build(Path(temporary) / "common.sqlite", [], providers)

    def test_missing_temporal_valid_from_fails_closed(self) -> None:
        providers = _providers()
        del providers["israel-mot"]["temporal"]["validFrom"]
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "provider=israel-mot temporal validFrom is missing"):
                self._build(Path(temporary) / "common.sqlite", [], providers)

    def test_missing_temporal_valid_through_fails_closed(self) -> None:
        providers = _providers()
        del providers["israel-mot"]["temporal"]["validThrough"]
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "provider=israel-mot temporal validThrough is missing"):
                self._build(Path(temporary) / "common.sqlite", [], providers)

    def test_identical_duplicate_is_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "common.sqlite"
            self._build(output, [("Toronto, ON", "toronto"), ("Toronto, ON", "toronto")])

            with sqlite3.connect(output) as connection:
                rows = connection.execute(
                    "SELECT alias_city_id, canonical_city_id FROM city_aliases"
                ).fetchall()
            self.assertEqual(rows, [("Toronto, ON", "toronto")])

    def test_three_identical_duplicates_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "common.sqlite"
            self._build(
                output,
                [("Toronto, ON", "toronto")] * 3,
            )

            with sqlite3.connect(output) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM city_aliases").fetchone()[0], 1)

    def test_conflicting_duplicate_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "common.sqlite"
            with self.assertRaisesRegex(
                ValueError,
                r"alias_city_id='Toronto, ON'.*\['montreal', 'toronto'\]",
            ):
                self._build(
                    output,
                    [("Toronto, ON", "toronto"), ("Toronto, ON", "montreal")],
                )
            self.assertFalse(output.exists())

    def test_insertion_order_does_not_change_catalog(self) -> None:
        aliases = [("ישראל", "israel"), ("Toronto, ON", "toronto"), ("Toronto, ON", "toronto")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._build(root / "first.sqlite", aliases)
            second = self._build(root / "second.sqlite", list(reversed(aliases)))

            self.assertEqual(first.input_fingerprint, second.input_fingerprint)
            self.assertEqual((root / "first.sqlite").read_bytes(), (root / "second.sqlite").read_bytes())

    def test_existing_toronto_and_israel_aliases_remain_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "common.sqlite"
            self._build(output, [("Toronto, ON", "toronto"), ("ישראל", "israel")])

            with sqlite3.connect(output) as connection:
                rows = connection.execute(
                    "SELECT alias_city_id, canonical_city_id "
                    "FROM city_aliases ORDER BY alias_city_id"
                ).fetchall()
            self.assertEqual(rows, [("Toronto, ON", "toronto"), ("ישראל", "israel")])


if __name__ == "__main__":
    unittest.main()
