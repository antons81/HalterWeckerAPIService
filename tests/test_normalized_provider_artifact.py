import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import normalized_provider_artifact as artifact  # noqa: E402
from external_staging import NormalizedProviderContext  # noqa: E402
from build_stop_packages import load_gtfs_archive  # noqa: E402
from external_gtfs import process_external_gtfs_sources  # noqa: E402
from gtfs_source_cache import GTFSArtifactCache  # noqa: E402


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _write_feed(
    path: Path,
    *,
    order: tuple[str, ...],
    compression: int,
    calendar_marker: str = "",
    calendar_dates_value: str = "20260101",
    stop_times_value: str | None = None,
) -> None:
    files = {
        "agency.txt": "agency_id,agency_name\nA1,Agency\n",
        "stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type\n"
            "S1,Station,0,0,,1\n"
            "S2,Platform,0,0,S1,0\n"
        ),
        "routes.txt": "route_id,route_short_name,route_long_name,route_type,agency_id\nR1,1,Route,3,A1\n",
        "trips.txt": "route_id,service_id,trip_id,trip_headsign,direction_id\nR1,S1,T1,Terminal,0\n",
        "stop_times.txt": stop_times_value or (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,08:00:00,08:00:00,S1,1\n"
            "T1,08:00:01,08:00:01,S1,1\n"
            "T1,08:10:00,08:10:00,S2,2\n"
        ),
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            f"S1,1,1,1,1,1,1,1,20200101,20301231{calendar_marker}\n"
        ),
        "calendar_dates.txt": f"service_id,date,exception_type\nS1,{calendar_dates_value},1\n",
        "transfers.txt": "from_stop_id,to_stop_id,transfer_type\nS1,S2,2\n",
        "pathways.txt": "pathway_id,from_stop_id,to_stop_id\nP1,S1,S2\n",
    }
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for index, filename in enumerate(order):
            info = zipfile.ZipInfo(filename)
            info.date_time = (2020, 1, 1, 0, index, 0)
            info.compress_type = compression
            archive.writestr(info, files[filename])


class NormalizedProviderArtifactTests(unittest.TestCase):
    def _build(self, root: Path, feed: Path, raw_sha: str):
        archive = zipfile.ZipFile(feed)
        context, usage = artifact.load_or_build(
            archive=archive,
            repository_root=REPOSITORY_ROOT,
            provider_id="israel-mot",
            raw_artifact_sha256=raw_sha,
            gtfs_cache_root=root / "gtfs-cache",
            environ={artifact.FEATURE_GATE: "1"},
        )
        archive.close()
        return context, usage

    def test_zip_metadata_and_order_are_semantic_hit_and_raw_sha_is_not_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.zip"
            second = root / "second.zip"
            order = (
                "agency.txt",
                "stops.txt",
                "routes.txt",
                "trips.txt",
                "stop_times.txt",
                "calendar.txt",
                "calendar_dates.txt",
                "transfers.txt",
                "pathways.txt",
            )
            _write_feed(first, order=order, compression=zipfile.ZIP_STORED)
            _write_feed(second, order=tuple(reversed(order)), compression=zipfile.ZIP_DEFLATED)

            context, first_usage = self._build(root, first, "a" * 64)
            context.close()
            context, second_usage = self._build(root, second, "b" * 64)
            context.close()

            self.assertEqual(first_usage.status, "MISS")
            self.assertEqual(second_usage.status, "HIT")
            self.assertEqual(first_usage.semantic_key, second_usage.semantic_key)
            self.assertNotEqual(first_usage.manifest["rawArtifactSHA256"], "b" * 64)

    def test_calendar_dates_and_stop_times_change_semantic_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.zip"
            second = root / "second.zip"
            _write_feed(
                first,
                order=("stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt", "calendar_dates.txt"),
                compression=zipfile.ZIP_STORED,
            )
            _write_feed(
                second,
                order=("stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt", "calendar_dates.txt"),
                compression=zipfile.ZIP_STORED,
                calendar_dates_value="20260102",
            )
            context, first_usage = self._build(root, first, "1" * 64)
            context.close()
            context, second_usage = self._build(root, second, "2" * 64)
            context.close()
            self.assertNotEqual(first_usage.semantic_key, second_usage.semantic_key)

            third = root / "third.zip"
            _write_feed(
                third,
                order=("stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt", "calendar_dates.txt"),
                compression=zipfile.ZIP_STORED,
                stop_times_value=(
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "T1,08:00:00,08:00:00,S1,1\n"
                    "T1,08:00:02,08:00:02,S1,1\n"
                    "T1,08:10:00,08:10:00,S2,2\n"
                ),
            )
            context, third_usage = self._build(root, third, "3" * 64)
            context.close()
            self.assertNotEqual(first_usage.semantic_key, third_usage.semantic_key)

    def test_duplicate_stop_times_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "duplicates.zip"
            _write_feed(
                feed,
                order=("stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt"),
                compression=zipfile.ZIP_STORED,
            )
            context, usage = self._build(root, feed, "4" * 64)
            try:
                count = context.connection.execute("SELECT count(*) FROM stop_times").fetchone()[0]
                self.assertEqual(count, 3)
                self.assertEqual(usage.manifest["rowCounts"]["stop_times.txt"], 3)
            finally:
                context.close()

    def test_invalid_manifest_and_database_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "feed.zip"
            _write_feed(
                feed,
                order=("stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt"),
                compression=zipfile.ZIP_STORED,
            )
            context, usage = self._build(root, feed, "5" * 64)
            context.close()
            manifest_path = usage.artifact_directory / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            with self.assertRaises(artifact.NormalizedArtifactError):
                self._build(root, feed, "5" * 64)

            manifest_path.write_text(json.dumps(usage.manifest), encoding="utf-8")
            database_path = usage.artifact_directory / "normalized.sqlite"
            database_path.write_bytes(b"corrupt")
            with self.assertRaises(artifact.NormalizedArtifactError):
                self._build(root, feed, "5" * 64)

    def test_builder_and_schema_changes_invalidate_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "feed.zip"
            _write_feed(
                feed,
                order=("stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt"),
                compression=zipfile.ZIP_STORED,
            )
            archive = zipfile.ZipFile(feed)
            semantic = artifact._semantic_manifest(archive)
            archive.close()
            builder = artifact.builder_fingerprint(REPOSITORY_ROOT)
            first = artifact._semantic_key(
                provider_id="israel-mot",
                builder=builder,
                semantic=semantic,
            )
            with mock.patch.object(artifact, "ARTIFACT_SCHEMA_VERSION", 3):
                second = artifact._semantic_key(
                    provider_id="israel-mot",
                    builder=builder,
                    semantic=semantic,
                )
            self.assertNotEqual(first, second)

    def test_external_old_and_persistent_normalized_paths_are_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "external.zip"
            _write_feed(
                feed,
                order=(
                    "agency.txt",
                    "stops.txt",
                    "routes.txt",
                    "trips.txt",
                    "stop_times.txt",
                    "calendar.txt",
                    "calendar_dates.txt",
                ),
                compression=zipfile.ZIP_STORED,
            )
            cities_path = root / "israel-cities.json"
            cities_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "fixture-israel",
                            "name": "Fixture Israel",
                            "aliases": [],
                            "latitude": 0.0,
                            "longitude": 0.0,
                            "radiusMeters": 100_000,
                            "timezone": "UTC",
                            "packageMode": "external",
                            "externalGTFSProvider": "israel-mot",
                            "externalGTFSProviders": ["israel-mot"],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            source = next(
                item
                for item in json.loads(
                    (REPOSITORY_ROOT / "config" / "external-gtfs-sources.json").read_text(
                        encoding="utf-8"
                    )
                )
                if item["id"] == "israel-mot"
            )
            source["url"] = str(feed)
            source["cities"] = str(cities_path)
            sources_path = root / "sources.json"
            sources_path.write_text(json.dumps([source]), encoding="utf-8")

            def build(output_name: str, persistent: bool) -> tuple[object, str]:
                output = root / output_name
                environment = {
                    "HALTEWECKER_EXTERNAL_BUILD_CACHE": "0",
                    "HALTEWECKER_EXTERNAL_TRANSFORMED_BUILD_CACHE": "0",
                    artifact.FEATURE_GATE: "1" if persistent else "0",
                }
                from contextlib import redirect_stdout
                from io import StringIO

                stream = StringIO()
                with redirect_stdout(stream):
                    result = process_external_gtfs_sources(
                        repository_root=REPOSITORY_ROOT,
                        sources_path=sources_path,
                        url_by_provider={},
                        output=output,
                        load_gtfs_archive=load_gtfs_archive,
                        environ=environment,
                        gtfs_cache=GTFSArtifactCache(
                            root / ("gtfs-cache-persistent" if persistent else "gtfs-cache-legacy")
                        ),
                    )
                return result, stream.getvalue()

            legacy_result, legacy_log = build("legacy", False)
            persistent_result, persistent_log = build("persistent", True)

            self.assertEqual(legacy_result, persistent_result)
            self.assertIn("stage=normalized-provider-artifact status=MISS", persistent_log)
            self.assertNotIn("stage=normalized-provider-artifact", legacy_log)

            persistent_result, warm_log = build("persistent-warm", True)
            self.assertEqual(legacy_result, persistent_result)
            self.assertIn("stage=normalized-provider-artifact status=HIT", warm_log)
            self.assertIn("raw SHA index matched", warm_log)


if __name__ == "__main__":
    unittest.main()
