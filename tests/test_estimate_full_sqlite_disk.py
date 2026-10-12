from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.estimate_full_sqlite_disk import estimate


class FullSQLiteDiskEstimateTests(unittest.TestCase):
    def test_only_sqlite_departure_packages_are_excluded_and_hardlinks_are_counted_once(self):
        with tempfile.TemporaryDirectory(prefix="full-sqlite-disk-estimate-") as temporary:
            root = Path(temporary)
            database = root / "departures.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE provider_city_modes(provider_id, city_id)")
                connection.executemany(
                    "INSERT INTO provider_city_modes VALUES ('source', ?)",
                    (("sqlite-city",),),
                )
                connection.commit()
            stop_data = root / "stop-data"
            departures = stop_data / "departures"
            partitions = stop_data / "departures-v2" / "sqlite-city"
            departures.mkdir(parents=True)
            partitions.mkdir(parents=True)
            sqlite_payload = departures / "sqlite-city.json"
            sqlite_payload.write_bytes(b"shared-schedule-payload" * 100)
            (partitions / "2026-10-12.json").hardlink_to(sqlite_payload)
            (partitions / "2026-10-13.json").write_bytes(b"other-schedule-payload" * 100)
            (stop_data / "stops").mkdir()
            (stop_data / "stops" / "sqlite-city.json").write_text("[]")
            (stop_data / "departures" / "package-only.json").write_text("{\"stops\":{}}")
            (stop_data / "departures-v2" / "package-only").mkdir()
            (stop_data / "departures-v2" / "package-only" / "2026-10-12.json").write_text("{}")
            # This hardlink proves the estimator does not remove blocks that
            # remain reachable from a non-schedule asset in the same release.
            (stop_data / "routes").mkdir()
            (stop_data / "routes" / "retained.json").hardlink_to(sqlite_payload)

            report = estimate(stop_data, database)

            self.assertGreater(report["replacedScheduleAllocatedBytes"], 0)
            self.assertLess(
                report["candidateStopDataBytes"],
                report["activeStopDataAllocatedBytes"],
            )
            self.assertEqual(report["databaseGrowthPercent"], 25)

    def test_missing_active_database_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="full-sqlite-disk-estimate-") as temporary:
            root = Path(temporary)
            stop_data = root / "stop-data"
            stop_data.mkdir()
            with self.assertRaisesRegex(ValueError, "active departures SQLite is missing"):
                estimate(stop_data, root / "missing.sqlite")


if __name__ == "__main__":
    unittest.main()
