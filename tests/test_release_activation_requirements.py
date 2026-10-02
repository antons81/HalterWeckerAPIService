"""Shared release consumer invariants, including receipt-independent checks."""

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.release_activation_requirements import (
    ActivationRequirementError, validate_provider_contract, validate_shared_release,
)


class ProviderContractTests(unittest.TestCase):
    def test_germany_build_does_not_require_germany_runtime(self):
        validate_provider_contract(("germany", "israel-mot"), ("israel-mot",), ("israel-mot",))

    def test_missing_build_germany_blocks_activation(self):
        with self.assertRaisesRegex(ActivationRequirementError, "mandatory build provider"):
            validate_provider_contract(("israel-mot",), ("israel-mot",), ("israel-mot",))

    def test_unsupported_runtime_germany_blocks_activation(self):
        with self.assertRaisesRegex(ActivationRequirementError, "unsupported runtime providers: germany"):
            validate_provider_contract(("germany", "israel-mot"), ("germany",), ("israel-mot",))

    def test_runtime_provider_must_exist_in_build(self):
        with self.assertRaisesRegex(ActivationRequirementError, "absent from the build"):
            validate_provider_contract(("germany",), ("israel-mot",), ("israel-mot",))

    def test_empty_runtime_blocks_activation(self):
        with self.assertRaisesRegex(ActivationRequirementError, "runtime provider list is empty"):
            validate_provider_contract(("germany",), (), ())


class SharedReleaseRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = {"status": "active", "required": True, "cities": ["berlin", "wuppertal"]}
        self.write_manifest()
        self.common = self.root / "common.sqlite"
        self.structural = self.root / "structural.sqlite"
        self.temporal = self.root / "temporal.sqlite"
        for path in (self.common, self.structural):
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE provider_city_modes (provider_id TEXT, city_id TEXT)")
                connection.executemany("INSERT INTO provider_city_modes VALUES (?, ?)",
                                       [("germany", "berlin"), ("germany", "wuppertal")])
        with sqlite3.connect(self.common) as connection:
            connection.execute("CREATE TABLE provider_city_stops (provider_id TEXT, city_id TEXT, stop_id TEXT)")
            connection.executemany("INSERT INTO provider_city_stops VALUES (?, ?, ?)",
                                   [("germany", "berlin", "1"), ("germany", "wuppertal", "2")])
        with sqlite3.connect(self.structural) as connection:
            for table in ("routes", "trips", "stop_times"):
                connection.execute(f"CREATE TABLE {table} (id TEXT)")
                connection.execute(f"INSERT INTO {table} VALUES ('1')")
        with sqlite3.connect(self.temporal) as connection:
            connection.execute("CREATE TABLE active_services (service_id TEXT, service_date TEXT)")
            connection.execute("INSERT INTO active_services VALUES ('1', '20261002')")
        self.reference = SimpleNamespace(
            structural=SimpleNamespace(database_path=self.structural),
            temporal=SimpleNamespace(database_path=self.temporal, valid_from="2026-09-28", valid_through="2026-10-18"))
        manifest = SimpleNamespace(common_database_path=self.common, release_id="test", providers={"germany": self.reference})
        self.loader = patch("services.release_activation_requirements.load_release_manifest", return_value=manifest)
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def write_manifest(self):
        (self.root / "release.json").write_text(json.dumps({"providers": {"germany": self.entry}}))

    def validate(self):
        return validate_shared_release(self.root, validation_date=date(2026, 10, 2))

    def test_complete_candidate_passes(self):
        self.assertEqual(self.validate()["germanyCityMappings"], 2)

    def test_missing_germany_fails_even_with_existing_receipt(self):
        (self.root / "release.json").write_text('{"providers": {}}')
        (self.root / "validation-receipt.json").write_text('{"status": "PASS"}')
        with self.assertRaises(ActivationRequirementError):
            self.validate()

    def test_inactive_or_optional_germany_fails(self):
        for key, value in (("status", "optional"), ("required", False)):
            original = self.entry[key]
            self.entry[key] = value
            self.write_manifest()
            with self.assertRaises(ActivationRequirementError):
                self.validate()
            self.entry[key] = original

    def test_missing_city_mapping_fails(self):
        with sqlite3.connect(self.common) as connection:
            connection.execute("DELETE FROM provider_city_modes WHERE city_id='berlin'")
        with self.assertRaises(ActivationRequirementError):
            self.validate()

    def test_missing_stop_routing_fails(self):
        with sqlite3.connect(self.common) as connection:
            connection.execute("DELETE FROM provider_city_stops WHERE city_id='berlin'")
        with self.assertRaises(ActivationRequirementError):
            self.validate()

    def test_empty_structural_data_fails(self):
        with sqlite3.connect(self.structural) as connection:
            connection.execute("DELETE FROM trips")
        with self.assertRaises(ActivationRequirementError):
            self.validate()

    def test_missing_database_fails(self):
        self.temporal.rename(self.root / "not-published.sqlite")
        with self.assertRaises(sqlite3.OperationalError):
            self.validate()

    def test_expired_temporal_window_fails(self):
        self.reference.temporal.valid_through = "2026-10-01"
        with self.assertRaises(ActivationRequirementError):
            self.validate()

    def test_temporal_with_no_current_services_fails(self):
        with sqlite3.connect(self.temporal) as connection:
            connection.execute("DELETE FROM active_services")
        with self.assertRaises(ActivationRequirementError):
            self.validate()


if __name__ == "__main__":
    unittest.main()
