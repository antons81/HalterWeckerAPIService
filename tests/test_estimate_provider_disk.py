"""Provider allocation forecasts, including reuse and isolated changes.

Created by Anton on 2026-10-11.
"""
import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from scripts.estimate_provider_disk import forecast_provider_allocations, MIB


class ProviderDiskForecastTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        raw = self.root / "feed.zip"
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("stop_times.txt", b"x" * MIB)
            archive.writestr("calendar.txt", b"x" * 100)
        provider = {"providerID": "a", "raw": {"status": "HIT", "path": str(raw)},
                    **{layer: {"status": "HIT"} for layer in ("normalized", "structural", "temporal")}}
        second = copy.deepcopy(provider)
        second["providerID"] = "b"
        self.plan = {"providers": [provider, second],
                     "dates": {"validFrom": "2026-10-05", "validThrough": "2026-10-25"}}

    def test_no_change_has_no_additional_provider_allocations(self):
        self.assertEqual(forecast_provider_allocations(self.plan, None)["additionalBytes"], 0)

    def test_changed_provider_does_not_charge_other_provider(self):
        self.plan["providers"][0]["structural"]["status"] = "MISS"
        result = forecast_provider_allocations(self.plan, None)
        self.assertGreaterEqual(result["additionalBytes"], 2 * MIB)
        self.assertEqual(result["providers"][1]["additionalBytes"], 0)

    def test_calendar_rollover_does_not_charge_structural_rebuild(self):
        self.plan["providers"][0]["temporal"]["status"] = "MISS"
        result = forecast_provider_allocations(self.plan, None)
        self.assertEqual(result["additionalBytes"], MIB)
        self.assertEqual(result["providers"][0]["layers"]["structural"], 0)

    def test_previous_database_is_lower_bound_not_new_allocation_on_hit(self):
        database = self.root / "provider.sqlite"
        with database.open("wb") as stream:
            stream.truncate(10 * MIB)
        (self.root / "release.json").write_text(json.dumps({"providers": {
            "a": {"structural": {"path": "provider.sqlite"}}, "b": {}}}))
        self.assertEqual(forecast_provider_allocations(self.plan, self.root)["additionalBytes"], 0)
        self.plan["providers"][0]["structural"]["status"] = "MISS"
        self.assertEqual(forecast_provider_allocations(self.plan, self.root)["additionalBytes"], 125 * MIB // 10)

    def test_invalid_cache_or_unavailable_provider_cannot_be_zero_cost_pass(self):
        for layer in ("raw", "normalized", "structural", "temporal"):
            with self.subTest(layer=layer):
                plan = copy.deepcopy(self.plan)
                plan["providers"][0][layer]["status"] = "INVALID"
                with self.assertRaises(ValueError):
                    forecast_provider_allocations(plan, None)
