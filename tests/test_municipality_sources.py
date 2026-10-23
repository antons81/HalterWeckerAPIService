"""Regression coverage for municipality reference-source outages."""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from scripts import build_stop_packages as builder


def feature(code="11000000", name="Berlin", state="11"):
    return {
        "type": "Feature", "bbox": [13, 52, 14, 53],
        "properties": {"ags": code, "gen": name, "sn_l": state, "lkz": "BE"},
        "geometry": {"type": "Polygon", "coordinates": [[[13, 52], [14, 52], [14, 53], [13, 52]]]},
    }


class MunicipalitySourcesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / "bkg"
        self.source = builder.BKG_MUNICIPALITIES_URL
        self.unavailable = HTTPError(self.source, 400, "Bad Request", {}, None)

    def load(self, **kwargs):
        return builder.load_municipalities(self.source, cache_root=self.cache, **kwargs)

    def seed_cache(self):
        with mock.patch.object(builder, "load_municipality_features", return_value=[feature()]):
            result = self.load()
        return result, next(self.cache.glob("*.json"))

    def test_primary_success_creates_validated_cache(self):
        expected, path = self.seed_cache()
        cached = json.loads(path.read_text())
        self.assertEqual(cached["source"], self.source)
        self.assertEqual(cached["featureCount"], 1)
        self.assertEqual(expected[0]["code"], "11000000")
        self.assertEqual(list(self.cache.glob("*.tmp")), [])

    def test_http_400_uses_verified_cache_and_keeps_municipalities(self):
        expected, _ = self.seed_cache()
        with mock.patch.object(builder, "load_municipality_features", side_effect=self.unavailable), \
                mock.patch.object(builder, "load_nrw_municipality_features") as alternative, \
                mock.patch("builtins.print") as output:
            self.assertEqual(self.load(), expected)
        alternative.assert_not_called()
        self.assertIn("fallback=validated-cache", str(output.call_args_list))

    def test_cache_miss_fetches_alternative_with_explicit_warning(self):
        with mock.patch.object(builder, "load_municipality_features", side_effect=self.unavailable), \
                mock.patch.object(builder, "load_nrw_municipality_features", return_value=[feature()]), \
                mock.patch("builtins.print") as output:
            result = self.load()
        self.assertEqual(result[0]["name"], "Berlin")
        cached = json.loads(next(self.cache.glob("*.json")).read_text())
        self.assertEqual(cached["origin"], builder.NRW_MUNICIPALITIES_URL)
        self.assertIn("referenceDate=unconfirmed", str(output.call_args_list))

    def test_corrupt_cache_does_not_masquerade_as_valid_reference_data(self):
        _, path = self.seed_cache()
        cached = json.loads(path.read_text())
        cached["features"][0]["properties"]["gen"] = "Tampered"
        path.write_text(json.dumps(cached))
        with mock.patch.object(builder, "load_municipality_features", side_effect=self.unavailable), \
                mock.patch.object(builder, "load_nrw_municipality_features", return_value=[feature()]) as alternative:
            self.assertEqual(self.load()[0]["name"], "Berlin")
        alternative.assert_called_once_with()

    def test_failed_sources_without_cache_raise_instead_of_returning_empty_data(self):
        with mock.patch.object(builder, "load_municipality_features", side_effect=self.unavailable), \
                mock.patch.object(builder, "load_nrw_municipality_features", side_effect=URLError("offline")):
            with self.assertRaises(URLError):
                self.load()
        self.assertFalse(self.cache.exists())

    def test_custom_source_does_not_silently_switch_to_unrelated_boundaries(self):
        with mock.patch.object(builder, "load_municipality_features", side_effect=self.unavailable), \
                mock.patch.object(builder, "load_nrw_municipality_features") as alternative:
            with self.assertRaises(HTTPError):
                builder.load_municipalities("https://example.org/municipalities", cache_root=self.cache)
        alternative.assert_not_called()

    def test_local_corrupt_file_does_not_fall_back_to_network(self):
        path = Path(self.directory.name) / "invalid.json"
        path.write_text("{invalid")
        with mock.patch.object(builder, "load_nrw_municipality_features") as alternative:
            with self.assertRaises(ValueError):
                builder.load_municipalities(str(path), cache_root=self.cache)
        alternative.assert_not_called()

    def test_failed_atomic_cache_replacement_preserves_previous_cache_and_removes_temp(self):
        _, path = self.seed_cache()
        before = path.read_bytes()
        with mock.patch.object(builder, "load_municipality_features", return_value=[feature(name="Changed")]), \
                mock.patch.object(builder.os, "replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.load()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.cache.iterdir()), [path])

    def mock_nrw(self, rows, *, expected=None):
        offsets = []

        def response(request, **kwargs):
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(query["where"], ["gf=4"])
            if "returnCountOnly" in query:
                payload = {"count": len(rows) if expected is None else expected}
            else:
                offset = int(query["resultOffset"][0])
                count = int(query["resultRecordCount"][0])
                self.assertEqual(query["outSR"], ["4326"])
                self.assertEqual(query["orderByFields"], ["fid ASC"])
                offsets.append(offset)
                payload = {"type": "FeatureCollection", "features": rows[offset:offset + count]}
            return io.BytesIO(json.dumps(payload).encode())

        return response, offsets

    def nationwide_rows(self):
        return [feature(code=state + "000000", state=state) for state in builder.GERMAN_STATE_ABBREVIATIONS]

    def test_alternative_paginates_all_states_and_normalizes_bbox_and_state(self):
        rows = self.nationwide_rows()
        for row in rows:
            row.pop("bbox")
            row["properties"].pop("lkz")
        response, offsets = self.mock_nrw(rows)
        with mock.patch.object(builder, "urlopen_with_tls", side_effect=response), \
                mock.patch.object(builder, "BKG_PAGE_SIZE", 7):
            features = builder.load_nrw_municipality_features()
        self.assertEqual(offsets, [0, 7, 14])
        self.assertEqual(len(features), 16)
        self.assertEqual(features[0]["properties"]["lkz"], "SH")
        self.assertEqual(features[0]["bbox"], [13, 52, 14, 53])
        self.assertEqual(len(builder.municipalities_from_features(features)), 16)

    def test_alternative_rejects_missing_page_duplicate_and_missing_state(self):
        rows = self.nationwide_rows()
        cases = [(rows, len(rows) + 1), (rows + [copy.deepcopy(rows[0])], None), (rows[1:], None)]
        for dataset, count in cases:
            with self.subTest(count=count, size=len(dataset)):
                response, _ = self.mock_nrw(dataset, expected=count)
                with mock.patch.object(builder, "urlopen_with_tls", side_effect=response):
                    with self.assertRaises(ValueError):
                        builder.load_nrw_municipality_features()

    def test_alternative_rejects_projected_or_unclosed_geometry(self):
        for coordinates in ([[[600000, 5800000]] * 4], [[[13, 52], [14, 52], [14, 53], [13, 53]]]):
            rows = self.nationwide_rows()
            rows[0]["geometry"]["coordinates"] = coordinates
            response, _ = self.mock_nrw(rows)
            with self.subTest(coordinates=coordinates), \
                    mock.patch.object(builder, "urlopen_with_tls", side_effect=response):
                with self.assertRaises(ValueError):
                    builder.load_nrw_municipality_features()


if __name__ == "__main__":
    unittest.main()
