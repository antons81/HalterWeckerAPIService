import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

import external_build_cache
import external_gtfs
from external_build_cache import (
    DeparturePartitionCache,
    departure_partition_key,
    departure_stop_set_digest,
)
from gtfs_source_cache import GTFSArtifactCache
from static_departures_api import ExternalStaticData


class ExternalDeparturePartitionTests(unittest.TestCase):
    def test_required_provider_without_cache_fails_before_build(self):
        with self.assertRaisesRegex(ValueError, "requires departure v3 cache"):
            external_gtfs._build_external_departure_partitions(
                archive=None, cities=[], output=Path("unused"),
                timezone_name="UTC", namespace="", departure_window_days=3,
                context=None, output_schema_version=3, provider_id="israel-mot",
                source={}, repository_root=Path.cwd(), raw_artifact_digest=None,
                structural_input_key="", gtfs_cache=None,
                environ={"HALTEWECKER_EXTERNAL_DEPARTURES_V3_PROVIDERS": "israel-mot"},
            )

    def test_required_v3_providers_cannot_use_legacy_writer(self):
        environment = {"HALTEWECKER_EXTERNAL_DEPARTURES_V3_PROVIDERS": "israel-mot,ttc-surface,ttc-subway", "HALTEWECKER_EXTERNAL_DEPARTURES_SCHEMA": "1"}
        for provider in ("israel-mot", "ttc-surface", "ttc-subway"):
            self.assertEqual(external_gtfs._departure_output_schema_version(environment, provider), 3)
        self.assertEqual(external_gtfs._departure_output_schema_version(environment, "cta-chicago"), 1)

    def _feed(self, path: Path) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("agency.txt", "agency_id,agency_name\nA,Fixture\n")
            archive.writestr(
                "stops.txt",
                "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                "stop,Stop,43.0,-79.0,0,\n",
            )
            archive.writestr(
                "routes.txt",
                "route_id,route_short_name,route_long_name,route_type,agency_id\n"
                "R1,1,One,3,A\n",
            )
            archive.writestr(
                "trips.txt",
                "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                "R1,S1,T1,Terminal,0\n",
            )
            archive.writestr(
                "stop_times.txt",
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "T1,08:00:00,08:00:00,stop,1\n",
            )
            archive.writestr(
                "calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                "S1,1,1,1,1,1,1,1,20260101,20261231\n",
            )

    def _build(self, root: Path, archive_path: Path, schema: int) -> None:
        (root / "stops").mkdir(parents=True)
        (root / "stops" / "fixture-city.json").write_text(
            json.dumps([{"id": "stop", "name": "Stop", "latitude": 43.0, "longitude": -79.0}]),
            encoding="utf-8",
        )
        with zipfile.ZipFile(archive_path) as archive:
            external_gtfs.build_external_departure_index(
                archive,
                [{"id": "fixture-city", "name": "Fixture"}],
                root,
                "America/Toronto",
                output_schema_version=schema,
                now=datetime(2026, 9, 14, 12, tzinfo=ZoneInfo("America/Toronto")),
            )

    def _write_manifest_package(
        self,
        root: Path,
        *,
        schema: int = 3,
        effective_date: str = "2026-09-14",
        selected_dates: tuple[str, ...] = ("20260914",),
        partition_dates: tuple[str, ...] = ("20260913", "20260914", "20260915"),
        timezone_name: str = "America/Toronto",
        city_id: str = "fixture-city",
    ) -> None:
        (root / "stops").mkdir(parents=True, exist_ok=True)
        (root / "stops" / f"{city_id}.json").write_text(
            json.dumps([{"id": "stop", "name": "Stop", "latitude": 43.0, "longitude": -79.0}]),
            encoding="utf-8",
        )
        partition_root = root / "departures-v2" / city_id
        partition_root.mkdir(parents=True, exist_ok=True)
        for service_date in partition_dates:
            (partition_root / f"{service_date}.json").write_text(
                json.dumps({
                    "generatedAt": f"{service_date}T00:00:00Z",
                    "timezone": timezone_name,
                    "stops": {
                        "stop": [{
                            "t": f"trip-{service_date}",
                            "p": "08:00:00",
                            "q": "1",
                            "r": "R1",
                            "h": "Terminal",
                            "d": "0",
                        }],
                    },
                    "platforms": {},
                }),
                encoding="utf-8",
            )
        manifest = {
            "schemaVersion": schema,
            "cityID": city_id,
            "timezone": timezone_name,
            "partitions": [
                {"serviceDate": service_date, "path": f"{service_date}.json"}
                for service_date in partition_dates
            ],
        }
        if schema == 3:
            manifest["selectionSemantics"] = "legacy-active-service-union"
            manifest["effectiveDate"] = effective_date
            manifest["selectedServiceDates"] = list(selected_dates)
        (partition_root / "manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

    def _load_package(self, root: Path, *, timezone_name: str = "America/Toronto") -> ExternalStaticData:
        data = ExternalStaticData(
            str(root),
            "fixture-city",
            "",
            timezone_name,
            now_provider=lambda: datetime(2026, 9, 14, 23, 30, tzinfo=ZoneInfo("UTC")),
        )
        data._ensure_loaded()
        return data

    def test_v2_partitions_merge_to_v1_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive_path = base / "fixture.zip"
            self._feed(archive_path)
            v1 = base / "v1"
            v2 = base / "v2"
            self._build(v1, archive_path, 1)
            self._build(v2, archive_path, 2)
            manifest = json.loads(
                (v2 / "departures-v2" / "fixture-city" / "manifest.json").read_text()
            )
            self.assertEqual(manifest["schemaVersion"], 2)
            self.assertEqual(
                [item["serviceDate"] for item in manifest["partitions"]],
                ["20260913", "20260914", "20260915"],
            )
            v1_data = ExternalStaticData(str(v1), "fixture-city", "", "America/Toronto", now_provider=lambda: datetime(2026, 9, 14, 12, tzinfo=ZoneInfo("America/Toronto")))
            v2_data = ExternalStaticData(str(v2), "fixture-city", "", "America/Toronto", now_provider=lambda: datetime(2026, 9, 14, 12, tzinfo=ZoneInfo("America/Toronto")))
            v1_data._ensure_loaded()
            v2_data._ensure_loaded()
            self.assertEqual(v1_data.departures, v2_data.departures)
            self.assertEqual(v1_data.platforms, v2_data.platforms)

    def test_v2_missing_required_partition_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive_path = base / "fixture.zip"
            self._feed(archive_path)
            self._build(base / "v2", archive_path, 2)
            partition = base / "v2" / "departures-v2" / "fixture-city" / "20260914.json"
            partition.unlink()
            data = ExternalStaticData(str(base / "v2"), "fixture-city", "", "America/Toronto", now_provider=lambda: datetime(2026, 9, 14, 12, tzinfo=ZoneInfo("America/Toronto")))
            with self.assertRaises(FileNotFoundError):
                data._ensure_loaded()

    def test_unknown_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partition_root = root / "departures-v2" / "fixture-city"
            partition_root.mkdir(parents=True)
            (root / "stops").mkdir()
            (root / "stops" / "fixture-city.json").write_text("[]")
            (partition_root / "manifest.json").write_text(json.dumps({"schemaVersion": 99}))
            data = ExternalStaticData(str(root), "fixture-city", "", "UTC")
            with self.assertRaises(ValueError):
                data._ensure_loaded()

    def test_schema3_selected_one_date_excludes_other_cached_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_manifest_package(root, selected_dates=("20260914",))
            data = self._load_package(root)
            self.assertEqual(
                [item["t"] for item in data.departures["stop"]],
                ["trip-20260914"],
            )

    def test_schema3_requires_legacy_union_selection_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_manifest_package(root)
            manifest_path = root / "departures-v2" / "fixture-city" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest.pop("selectionSemantics")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                self._load_package(root)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_manifest_package(root)
            manifest_path = root / "departures-v2" / "fixture-city" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["selectionSemantics"] = "exact-effective-date"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                self._load_package(root)

    def test_schema3_union_deduplicates_known_israel_rows_and_preserves_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_manifest_package(
                root,
                selected_dates=("20260913", "20260914", "20260915"),
            )
            partition_root = root / "departures-v2" / "fixture-city"
            duplicate = {
                "t": "21800555_140926",
                "r": "17211",
                "h": "Terminal",
                "d": "0",
                "p": "22:45:50",
                "q": "20",
            }
            for service_date in ("20260913", "20260915"):
                payload = json.loads((partition_root / f"{service_date}.json").read_text())
                payload["stops"]["stop"] = [duplicate]
                (partition_root / f"{service_date}.json").write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
            data = self._load_package(root)
            rows = data.departures["stop"]
            self.assertEqual(
                [item["t"] for item in rows],
                ["trip-20260914", "21800555_140926"],
            )

    def test_schema3_union_conflicting_duplicate_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_manifest_package(
                root,
                selected_dates=("20260913", "20260914", "20260915"),
            )
            partition_root = root / "departures-v2" / "fixture-city"
            first = json.loads((partition_root / "20260913.json").read_text())
            second = json.loads((partition_root / "20260915.json").read_text())
            first["stops"]["stop"] = [{
                "t": "584892271_140926",
                "r": "17211",
                "h": "Terminal A",
                "d": "0",
                "p": "23:15:50",
                "q": "20",
            }]
            second["stops"]["stop"] = [{
                "t": "584892271_140926",
                "r": "17211",
                "h": "Terminal B",
                "d": "0",
                "p": "23:15:50",
                "q": "20",
            }]
            (partition_root / "20260913.json").write_text(json.dumps(first), encoding="utf-8")
            (partition_root / "20260915.json").write_text(json.dumps(second), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflicting duplicate departure row"):
                self._load_package(root)

    def test_schema3_selected_two_dates_merges_only_selected_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_manifest_package(
                root,
                selected_dates=("20260913", "20260915"),
            )
            # The effective date must be one of the selected dates.
            manifest_path = root / "departures-v2" / "fixture-city" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["effectiveDate"] = "2026-09-13"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            data = self._load_package(root)
            self.assertEqual(
                {item["t"] for item in data.departures["stop"]},
                {"trip-20260913", "trip-20260915"},
            )

    def test_schema3_selection_is_independent_of_restart_time_and_host_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_manifest_package(
                root,
                selected_dates=("20261231",),
                effective_date="2026-12-31",
                partition_dates=("20261230", "20261231", "20270101"),
            )
            first = ExternalStaticData(
                str(root), "fixture-city", "", "Pacific/Auckland",
                now_provider=lambda: datetime(2027, 1, 1, 0, 30, tzinfo=ZoneInfo("Pacific/Auckland")),
            )
            second = ExternalStaticData(
                str(root), "fixture-city", "", "Pacific/Auckland",
                now_provider=lambda: datetime(2027, 1, 2, 23, 30, tzinfo=ZoneInfo("UTC")),
            )
            first._ensure_loaded()
            second._ensure_loaded()
            self.assertEqual(first.departures, second.departures)

    def test_schema3_missing_selection_metadata_fails_closed(self) -> None:
        for field in ("effectiveDate", "selectedServiceDates"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self._write_manifest_package(root)
                manifest_path = root / "departures-v2" / "fixture-city" / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest.pop(field)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self._load_package(root)

    def test_schema3_missing_or_wrong_date_partition_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_manifest_package(
                root,
                selected_dates=("20260916",),
                effective_date="2026-09-16",
            )
            with self.assertRaises(FileNotFoundError):
                self._load_package(root)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_manifest_package(root, selected_dates=("20260914",))
            manifest_path = root / "departures-v2" / "fixture-city" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["partitions"][1]["path"] = "20260915.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                self._load_package(root)

    def test_schema3_city_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_manifest_package(root)
            manifest_path = root / "departures-v2" / "fixture-city" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["cityID"] = "other-city"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                self._load_package(root)

    def test_schema3_provider_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_manifest_package(root)
            manifest_path = root / "departures-v2" / "fixture-city" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["providerID"] = "provider-a"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            data = ExternalStaticData(
                str(root), "fixture-city", "", "America/Toronto",
                provider_id="provider-b",
            )
            with self.assertRaises(ValueError):
                data._ensure_loaded()

    def test_schema3_builder_publishes_selected_effective_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive_path = base / "fixture.zip"
            self._feed(archive_path)
            root = base / "v3"
            self._build(root, archive_path, 3)
            manifest = json.loads(
                (root / "departures-v2" / "fixture-city" / "manifest.json").read_text()
            )
            self.assertEqual(manifest["schemaVersion"], 3)
            self.assertEqual(manifest["effectiveDate"], "2026-09-14")
            self.assertEqual(
                manifest["selectionSemantics"],
                "legacy-active-service-union",
            )
            self.assertEqual(
                manifest["selectedServiceDates"],
                ["20260913", "20260914", "20260915"],
            )

    def test_sliding_window_reuses_two_partitions_and_builds_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive_path = base / "fixture.zip"
            self._feed(archive_path)
            source = {"id": "fixture", "timezone": "America/Toronto", "departurePackageDays": 3}
            first = base / "first"
            second = base / "second"
            for root in (first, second):
                (root / "stops").mkdir(parents=True)
                (root / "stops" / "fixture-city.json").write_text(
                    json.dumps([{"id": "stop", "name": "Stop", "latitude": 43.0, "longitude": -79.0}]),
                    encoding="utf-8",
                )
            cache = GTFSArtifactCache(base / "gtfs-cache")
            with zipfile.ZipFile(archive_path) as archive:
                external_gtfs._build_external_departure_partitions(
                    archive=archive, cities=[{"id": "fixture-city", "name": "Fixture"}],
                    output=first, timezone_name="America/Toronto", namespace="",
                    departure_window_days=3, context=None, output_schema_version=2,
                    provider_id="fixture", source=source,
                    repository_root=Path(__file__).resolve().parents[1],
                    raw_artifact_digest="b" * 64, structural_input_key="structural",
                    gtfs_cache=cache, environ={
                        "HALTEWECKER_EXTERNAL_DEPARTURE_CACHE": "1",
                        "HALTEWECKER_EXTERNAL_DEPARTURE_CACHE_PROVIDERS": "fixture",
                    },
                    now=datetime(2026, 9, 14, 12, tzinfo=ZoneInfo("America/Toronto")),
                )
            with zipfile.ZipFile(archive_path) as archive:
                external_gtfs._build_external_departure_partitions(
                    archive=archive, cities=[{"id": "fixture-city", "name": "Fixture"}],
                    output=second, timezone_name="America/Toronto", namespace="",
                    departure_window_days=3, context=None, output_schema_version=2,
                    provider_id="fixture", source=source,
                    repository_root=Path(__file__).resolve().parents[1],
                    raw_artifact_digest="b" * 64, structural_input_key="structural",
                    gtfs_cache=cache, environ={
                        "HALTEWECKER_EXTERNAL_DEPARTURE_CACHE": "1",
                        "HALTEWECKER_EXTERNAL_DEPARTURE_CACHE_PROVIDERS": "fixture",
                    },
                    now=datetime(2026, 9, 15, 12, tzinfo=ZoneInfo("America/Toronto")),
                )
            # The helper uses provider-local now; verify the cache has one partition per service date
            # and that a second invocation never creates duplicate key directories.
            keys = list((base / "gtfs-cache" / "external-departure-partitions" / "fixture" / "fixture-city").glob("*/*"))
            self.assertEqual(len(keys), 4)
            self.assertEqual(
                {path.parent.name for path in keys},
                {"20260913", "20260914", "20260915", "20260916"},
            )

    def test_service_dates_use_provider_timezone_at_boundaries(self) -> None:
        self.assertEqual(
            external_gtfs._departure_service_dates(
                "America/Toronto", 3, now=datetime(2026, 9, 14, 0, 30, tzinfo=ZoneInfo("America/Toronto"))
            ),
            ("20260913", "20260914", "20260915"),
        )
        self.assertEqual(
            external_gtfs._departure_service_dates(
                "Pacific/Auckland", 3, now=datetime(2026, 1, 1, 0, 15, tzinfo=ZoneInfo("Pacific/Auckland"))
            ),
            ("20251231", "20260101", "20260102"),
        )
        self.assertEqual(
            external_gtfs._departure_service_dates(
                "America/Toronto", 3, now=datetime(2026, 9, 20, 12, tzinfo=ZoneInfo("America/Toronto"))
            ),
            ("20260919", "20260920", "20260921"),
        )
        self.assertEqual(
            external_gtfs._departure_service_dates(
                "America/Toronto", 3, now=datetime(2026, 3, 1, 12, tzinfo=ZoneInfo("America/Toronto"))
            ),
            ("20260228", "20260301", "20260302"),
        )
        self.assertEqual(
            external_gtfs._departure_service_dates(
                "America/Toronto", 3, now=datetime(2026, 3, 8, 12, tzinfo=ZoneInfo("America/Toronto"))
            ),
            ("20260307", "20260308", "20260309"),
        )

    def test_headsign_enrichment_reads_partitioned_departures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "trip-index-base").mkdir(parents=True)
            (root / "trip-index-base" / "fixture-city.json").write_text(
                json.dumps({"ttc:T1": {"r": "ttc:R1"}}), encoding="utf-8"
            )
            partition_root = root / "departures-v2" / "fixture-city"
            partition_root.mkdir(parents=True)
            dates = ("20260913", "20260914", "20260915")
            for service_date in dates:
                (partition_root / f"{service_date}.json").write_text(
                    json.dumps({
                        "generatedAt": "2026-09-14T00:00:00Z",
                        "timezone": "America/Toronto",
                        "stops": {"ttc:stop": [{"t": "ttc:T1", "h": "Terminal", "p": "08:00:00", "r": "ttc:R1"}]},
                        "platforms": {},
                    }), encoding="utf-8"
                )
            (partition_root / "manifest.json").write_text(json.dumps({
                "schemaVersion": 2, "cityID": "fixture-city", "timezone": "America/Toronto",
                "partitions": [{"serviceDate": value, "path": f"{value}.json"} for value in dates],
            }))
            external_gtfs.apply_current_departure_headsign_enrichment(
                root, [{"id": "fixture-city"}], namespace="ttc:"
            )
            self.assertEqual(
                json.loads((root / "trips" / "fixture-city.json").read_text()),
                {"ttc:T1": {"r": "ttc:R1", "h": "Terminal"}},
            )

    def test_schema3_headsign_enrichment_ignores_unselected_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "trip-index-base").mkdir(parents=True)
            (root / "trip-index-base" / "fixture-city.json").write_text(
                json.dumps({"T1": {"r": "R1"}}), encoding="utf-8"
            )
            partition_root = root / "departures-v2" / "fixture-city"
            partition_root.mkdir(parents=True)
            dates = ("20260913", "20260914", "20260915")
            headsigns = {
                "20260913": "Wrong before",
                "20260914": "Selected terminal",
                "20260915": "Wrong after",
            }
            for service_date in dates:
                (partition_root / f"{service_date}.json").write_text(
                    json.dumps({
                        "generatedAt": "2026-09-14T00:00:00Z",
                        "timezone": "America/Toronto",
                        "stops": {"stop": [{
                            "t": "T1",
                            "h": headsigns[service_date],
                            "p": "08:00:00",
                            "q": "1",
                            "r": "R1",
                            "d": "0",
                        }]},
                        "platforms": {},
                    }), encoding="utf-8"
                )
            (partition_root / "manifest.json").write_text(json.dumps({
                "schemaVersion": 3,
                "cityID": "fixture-city",
                "timezone": "America/Toronto",
                "selectionSemantics": "legacy-active-service-union",
                "effectiveDate": "2026-09-14",
                "selectedServiceDates": ["20260914"],
                "partitions": [{"serviceDate": value, "path": f"{value}.json"} for value in dates],
            }))
            external_gtfs.apply_current_departure_headsign_enrichment(
                root, [{"id": "fixture-city"}], namespace=""
            )
            self.assertEqual(
                json.loads((root / "trips" / "fixture-city.json").read_text()),
                {"T1": {"r": "R1", "h": "Selected terminal"}},
            )

    def _recovery_fixture(
        self, base: Path, *, city_ids: tuple[str, ...] = ("fixture-city",),
    ) -> tuple[Path, dict, DeparturePartitionCache, dict]:
        archive_path = base / "fixture.zip"
        self._feed(archive_path)
        source = {"id": "fixture", "timezone": "America/Toronto", "departurePackageDays": 3}
        cities = [{"id": city_id, "name": city_id} for city_id in city_ids]
        for output in (base / "seed", base / "recovered"):
            (output / "stops").mkdir(parents=True)
            for city_id in city_ids:
                (output / "stops" / f"{city_id}.json").write_text(
                    json.dumps([{"id": "stop", "name": "Stop", "latitude": 43.0, "longitude": -79.0}]),
                    encoding="utf-8",
                )
        gtfs_cache = GTFSArtifactCache(base / "gtfs-cache")
        kwargs = dict(
            cities=cities, output=base / "seed", timezone_name="America/Toronto",
            namespace="", departure_window_days=3, context=None, output_schema_version=3,
            provider_id="fixture", source=source,
            repository_root=Path(__file__).resolve().parents[1],
            raw_artifact_digest="b" * 64, structural_input_key="structural",
            gtfs_cache=gtfs_cache, environ={
                "HALTEWECKER_EXTERNAL_DEPARTURE_CACHE": "1",
                "HALTEWECKER_EXTERNAL_DEPARTURE_CACHE_PROVIDERS": "fixture",
            },
            now=datetime(2026, 9, 14, 12, tzinfo=ZoneInfo("America/Toronto")),
        )
        self._run_recovery_build(archive_path, kwargs)
        kwargs["output"] = base / "recovered"
        cache = DeparturePartitionCache(gtfs_cache.root / "external-departure-partitions", "fixture")
        keys = {}
        for city_id in city_ids:
            stop_digest, _ = departure_stop_set_digest(kwargs["output"] / "stops" / f"{city_id}.json")
            for service_date in ("20260913", "20260914", "20260915"):
                keys[(city_id, service_date)] = departure_partition_key(
                    repository_root=kwargs["repository_root"], provider_id="fixture",
                    city_id=city_id, service_date=service_date, raw_sha256="b" * 64,
                    structural_input_key="structural", stop_set_digest=stop_digest,
                    calendar_fingerprint="b" * 64, source=source,
                )
        return archive_path, kwargs, cache, keys

    def _run_recovery_build(self, archive_path: Path, kwargs: dict) -> str:
        logs = io.StringIO()
        with zipfile.ZipFile(archive_path) as archive, redirect_stdout(logs):
            external_gtfs._build_external_departure_partitions(archive=archive, **kwargs)
        return logs.getvalue()

    def _cache_snapshot(self, cache: DeparturePartitionCache, keys: dict) -> dict:
        snapshot = {}
        for identity, key in keys.items():
            for name in ("manifest.json", "partition.json"):
                path = cache._directory(key) / name
                stat = path.stat()
                snapshot[(identity, name)] = (path.read_bytes(), stat.st_ino, stat.st_mtime_ns)
        return snapshot

    def test_invalid_partition_diagnostics_include_error_and_full_path(self) -> None:
        cases = (
            ("manifest-json", "manifest.json", "JSONDecodeError"),
            ("manifest-encoding", "manifest.json", "UnicodeDecodeError"),
            ("partition-content", "partition.json", "partition provenance mismatch"),
            ("partition-missing", "partition.json", "ValueError"),
            ("partition-unreadable", "partition.json", "PermissionError"),
        )
        for case, filename, expected_error in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                _, _, cache, keys = self._recovery_fixture(Path(temporary))
                key = keys[("fixture-city", "20260914")]
                directory = cache._directory(key)
                damaged = directory / filename
                if case == "manifest-encoding":
                    damaged.write_bytes(b"\xff")
                elif case == "partition-missing":
                    damaged.unlink()
                elif case != "partition-unreadable":
                    damaged.write_text("{broken", encoding="utf-8")
                healthy = {identity: value for identity, value in keys.items() if value != key}
                snapshot = self._cache_snapshot(cache, healthy)
                real_provenance = external_build_cache.artifact_provenance

                def read_provenance(path):
                    if case == "partition-unreadable" and path == damaged:
                        raise PermissionError(13, "fixture read denied", str(path))
                    return real_provenance(path)

                logs = io.StringIO()
                with patch.object(external_build_cache, "artifact_provenance", side_effect=read_provenance):
                    with redirect_stdout(logs):
                        probe = cache.probe(key)
                        self.assertEqual(probe.status, "INVALID")
                        self.assertTrue(directory.is_dir())
                        self.assertEqual(logs.getvalue(), "")
                        lookup = cache.lookup(key)
                self.assertEqual(lookup.status, "INVALID")
                self.assertIn(str(damaged.absolute()), lookup.reason)
                self.assertIn(expected_error, lookup.reason)
                self.assertIn(lookup.reason, logs.getvalue())
                self.assertIn("cityID=fixture-city serviceDate=20260914 status=INVALID", logs.getvalue())
                self.assertFalse(directory.exists())
                self.assertEqual(self._cache_snapshot(cache, healthy), snapshot)

    def test_corrupt_partition_is_rebuilt_without_touching_valid_city_or_dates(self) -> None:
        for filename in ("manifest.json", "partition.json"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                archive_path, kwargs, cache, keys = self._recovery_fixture(
                    Path(temporary), city_ids=("fixture-city", "other-city"),
                )
                key = keys[("fixture-city", "20260914")]
                directory = cache._directory(key)
                original = json.loads((directory / "partition.json").read_text())
                (directory / filename).write_text("{broken", encoding="utf-8")
                healthy = {identity: value for identity, value in keys.items() if value != key}
                snapshot = self._cache_snapshot(cache, healthy)
                real_build = external_gtfs.build_external_departure_index

                def rebuild(*args, **build_kwargs):
                    self.assertFalse(directory.exists())
                    self.assertEqual(args[1], [kwargs["cities"][0]])
                    self.assertEqual(build_kwargs["write_service_dates"], ("20260914",))
                    return real_build(*args, **build_kwargs)

                with patch.object(external_gtfs, "build_external_departure_index", side_effect=rebuild) as builder:
                    logs = self._run_recovery_build(archive_path, kwargs)
                builder.assert_called_once()
                self.assertIn("status=INVALID", logs)
                self.assertIn(str((directory / filename).absolute()), logs)
                self.assertEqual(cache.probe(key).status, "HIT")
                self.assertEqual(self._cache_snapshot(cache, healthy), snapshot)
                rebuilt = json.loads((directory / "partition.json").read_text())
                self.assertEqual(rebuilt["stops"], original["stops"])
                manifest_path = kwargs["output"] / "departures-v2" / "fixture-city" / "manifest.json"
                self.assertEqual(json.loads(manifest_path.read_text())["schemaVersion"], 3)
                for (city_id, service_date), healthy_key in healthy.items():
                    output_path = kwargs["output"] / "departures-v2" / city_id / f"{service_date}.json"
                    self.assertEqual(output_path.read_bytes(), (cache._directory(healthy_key) / "partition.json").read_bytes())
                with patch.object(external_gtfs, "build_external_departure_index") as builder:
                    self._run_recovery_build(archive_path, kwargs)
                builder.assert_not_called()

    def test_recovery_build_failure_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path, kwargs, cache, keys = self._recovery_fixture(Path(temporary))
            key = keys[("fixture-city", "20260914")]
            (cache._directory(key) / "manifest.json").write_text("{broken")
            with patch.object(external_gtfs, "build_external_departure_index", side_effect=RuntimeError("fixture rebuild failed")):
                with self.assertRaisesRegex(RuntimeError, "fixture rebuild failed"):
                    self._run_recovery_build(archive_path, kwargs)
            self.assertFalse(cache._directory(key).exists())
            self.assertFalse((kwargs["output"] / "departures-v2" / "fixture-city" / "manifest.json").exists())

    def test_recovery_without_readable_gtfs_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path, kwargs, cache, keys = self._recovery_fixture(Path(temporary))
            key = keys[("fixture-city", "20260914")]
            (cache._directory(key) / "manifest.json").write_text("{broken")
            with zipfile.ZipFile(archive_path) as archive:
                pass
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "ZIP archive.*closed"):
                external_gtfs._build_external_departure_partitions(archive=archive, **kwargs)
            self.assertFalse(cache._directory(key).exists())

    def test_recovery_cache_write_failure_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path, kwargs, cache, keys = self._recovery_fixture(Path(temporary))
            key = keys[("fixture-city", "20260914")]
            directory = cache._directory(key)
            (directory / "manifest.json").write_text("{broken")
            real_replace = external_build_cache.os.replace

            def replace(source, destination):
                if Path(destination) == directory:
                    raise PermissionError(13, "fixture cache write denied", str(destination))
                return real_replace(source, destination)

            with patch.object(external_build_cache.os, "replace", side_effect=replace):
                with self.assertRaisesRegex(PermissionError, "fixture cache write denied"):
                    self._run_recovery_build(archive_path, kwargs)
            self.assertFalse(directory.exists())
            self.assertEqual(list(directory.parent.glob(".*")), [])
            manifest = kwargs["output"] / "departures-v2" / "fixture-city" / "manifest.json"
            self.assertEqual(json.loads(manifest.read_text())["schemaVersion"], 2)

    def test_recovery_rejects_missing_or_invalid_saved_cache(self) -> None:
        for case in ("missing", "invalid"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                archive_path, kwargs, cache, keys = self._recovery_fixture(Path(temporary))
                key = keys[("fixture-city", "20260914")]
                directory = cache._directory(key)
                (directory / "manifest.json").write_text("{broken")
                real_persist = DeparturePartitionCache.persist

                def persist(instance, saved_key, source):
                    if case == "invalid":
                        real_persist(instance, saved_key, source)
                        (instance._directory(saved_key) / "manifest.json").write_text("{broken")

                with patch.object(DeparturePartitionCache, "persist", autospec=True, side_effect=persist):
                    with self.assertRaisesRegex(ValueError, "departure cache save failed") as raised:
                        self._run_recovery_build(archive_path, kwargs)
                self.assertIn(str(directory.absolute()), str(raised.exception))
                manifest = kwargs["output"] / "departures-v2" / "fixture-city" / "manifest.json"
                self.assertEqual(json.loads(manifest.read_text())["schemaVersion"], 2)

    def test_recovery_removal_failure_stops_before_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path, kwargs, cache, keys = self._recovery_fixture(Path(temporary))
            key = keys[("fixture-city", "20260914")]
            directory = cache._directory(key)
            damaged = directory / "manifest.json"
            damaged.write_text("{broken")
            logs = io.StringIO()
            with patch.object(external_build_cache.shutil, "rmtree", side_effect=PermissionError(13, "fixture removal denied", str(directory))):
                with patch.object(external_gtfs, "build_external_departure_index") as builder:
                    with zipfile.ZipFile(archive_path) as archive, redirect_stdout(logs):
                        with self.assertRaisesRegex(PermissionError, "fixture removal denied"):
                            external_gtfs._build_external_departure_partitions(archive=archive, **kwargs)
                    builder.assert_not_called()
            self.assertTrue(directory.is_dir())
            self.assertIn(str(damaged.absolute()), logs.getvalue())
            self.assertIn("JSONDecodeError", logs.getvalue())

    def test_partition_key_excludes_release_identity_and_cache_restores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key_a = departure_partition_key(
                repository_root=Path(__file__).resolve().parents[1],
                provider_id="fixture",
                city_id="fixture-city",
                service_date="20260914",
                raw_sha256="a" * 64,
                structural_input_key="structural-key",
                stop_set_digest="b" * 64,
                calendar_fingerprint="calendar-key",
                source={"timezone": "UTC", "departurePackageDays": 3},
            )
            key_b = departure_partition_key(
                repository_root=Path(__file__).resolve().parents[1],
                provider_id="fixture",
                city_id="fixture-city",
                service_date="20260914",
                raw_sha256="a" * 64,
                structural_input_key="structural-key",
                stop_set_digest="b" * 64,
                calendar_fingerprint="calendar-key",
                source={"timezone": "UTC", "departurePackageDays": 3, "releaseID": "different"},
            )
            self.assertEqual(key_a.value, key_b.value)
            source = root / "partition.json"
            source.write_text("{\"stops\":{}}")
            cache = DeparturePartitionCache(root / "cache", "fixture")
            cache.persist(key_a, source)
            lookup = cache.lookup(key_b)
            self.assertEqual(lookup.status, "HIT")
            destination = root / "restored.json"
            cache.restore(lookup, destination)
            self.assertEqual(destination.read_text(), source.read_text())

    def test_stop_set_digest_is_canonical_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps([{"id": "2"}, {"id": "1"}]))
            second.write_text("[ {\"id\": \"1\"}, {\"id\": \"2\"} ]")
            self.assertEqual(
                departure_stop_set_digest(first),
                departure_stop_set_digest(second),
            )

    def test_stop_set_change_changes_partition_key(self) -> None:
        common = {
            "repository_root": Path(__file__).resolve().parents[1],
            "provider_id": "fixture",
            "city_id": "fixture-city",
            "service_date": "20260914",
            "raw_sha256": "a" * 64,
            "structural_input_key": "structural-key",
            "calendar_fingerprint": "calendar-key",
            "source": {"timezone": "UTC", "departurePackageDays": 3},
        }
        key_a = departure_partition_key(stop_set_digest="b" * 64, **common)
        key_b = departure_partition_key(stop_set_digest="c" * 64, **common)
        self.assertNotEqual(key_a.value, key_b.value)

    def test_partition_without_stop_set_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "partition.json"
            source.write_text("{\"stops\":{}}")
            key = departure_partition_key(
                repository_root=Path(__file__).resolve().parents[1],
                provider_id="fixture",
                city_id="fixture-city",
                service_date="20260914",
                raw_sha256="a" * 64,
                structural_input_key="structural-key",
                stop_set_digest="b" * 64,
                calendar_fingerprint="calendar-key",
                source={"timezone": "UTC", "departurePackageDays": 3},
            )
            cache = DeparturePartitionCache(root / "cache", "fixture")
            cache.persist(key, source)
            manifest_path = cache._directory(key) / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            del manifest["stopSetDigest"]
            manifest_path.write_text(json.dumps(manifest))
            self.assertEqual(cache.lookup(key).status, "INVALID")


if __name__ == "__main__":
    unittest.main()
