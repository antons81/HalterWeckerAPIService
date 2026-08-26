import contextlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import external_gtfs
from build_stop_packages import load_gtfs_archive
from external_build_cache import CacheKey, ExternalBuildCache
from external_gtfs import load_external_gtfs_sources, process_external_gtfs_sources
from gtfs_source_cache import GTFSArtifactCache

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _write_feed(
    path: Path,
    *,
    active_date: str | None = None,
    marker: str = "1",
    stop_lat: float = 41.8800,
    stop_lon: float = -87.6300,
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type\n"
            f"station,Station,{stop_lat:.6f},{stop_lon:.6f},,1\n"
            f"platform,Platform,{stop_lat:.6f},{stop_lon:.6f},station,0\n"
            f"street,Street Stop,{stop_lat + 0.001:.6f},{stop_lon - 0.001:.6f},,0\n",
        )
        archive.writestr(
            "routes.txt",
            f"route_id,route_short_name,route_long_name,route_type\nR{marker},17,Line {marker},0\n",
        )
        archive.writestr(
            "trips.txt",
            f"route_id,service_id,trip_id,trip_headsign,direction_id\nR{marker},S1,T{marker},Terminal {marker},0\n",
        )
        archive.writestr(
            "stop_times.txt",
            f"trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            f"T{marker},08:00:00,08:00:00,platform,1\n"
            f"T{marker},08:10:00,08:10:00,street,2\n",
        )
        if active_date is None:
            archive.writestr(
                "calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                "S1,1,1,1,1,1,1,20200101,20301231\n",
            )
        else:
            archive.writestr(
                "calendar_dates.txt",
                f"service_id,date,exception_type\nS1,{active_date},1\n",
            )


class ExternalBuildCacheTests(unittest.TestCase):
    def _source(
        self,
        feed: Path,
        *,
        provider_id: str = "cta-chicago",
        city_id: str = "chicago",
        cities_path: str | None = None,
    ) -> dict[str, object]:
        source = next(
            item
            for item in load_external_gtfs_sources(
                REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
            )
            if item["id"] == provider_id
        )
        source["url"] = str(feed)
        source["cities"] = cities_path or str(source["cities"])
        return source

    def _source_with_test_city(
        self,
        feed: Path,
        *,
        provider_id: str,
        city_id: str,
    ) -> dict[str, object]:
        source = self._source(
            feed,
            provider_id=provider_id,
            city_id=city_id,
        )
        cities = json.loads(
            (REPOSITORY_ROOT / str(source["cities"])).read_text(encoding="utf-8")
        )
        self.assertEqual([city["id"] for city in cities], [city_id])
        return source

    def _run(
        self,
        root: Path,
        feed: Path,
        source: dict[str, object],
        *,
        gate: bool = True,
        allowlist: str | None = None,
        output_name: str = "output",
    ) -> tuple[Path, str]:
        sources_path = root / f"sources-{output_name}.json"
        sources_path.write_text(json.dumps([source]), encoding="utf-8")
        output = root / output_name
        environment = {"HALTEWECKER_EXTERNAL_BUILD_CACHE": "1" if gate else "0"}
        if allowlist is not None:
            environment["HALTEWECKER_EXTERNAL_BUILD_CACHE_PROVIDERS"] = allowlist
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            process_external_gtfs_sources(
                repository_root=REPOSITORY_ROOT,
                sources_path=sources_path,
                url_by_provider={},
                output=output,
                load_gtfs_archive=load_gtfs_archive,
                environ=environment,
                gtfs_cache=GTFSArtifactCache(root / "gtfs-cache"),
            )
        return output, stream.getvalue()

    def _run_sources(
        self,
        root: Path,
        sources: list[dict[str, object]],
        *,
        gate: bool = True,
        allowlist: str | None = None,
        output_name: str = "output",
    ) -> tuple[Path, str]:
        sources_path = root / f"sources-{output_name}.json"
        sources_path.write_text(json.dumps(sources), encoding="utf-8")
        output = root / output_name
        environment = {"HALTEWECKER_EXTERNAL_BUILD_CACHE": "1" if gate else "0"}
        if allowlist is not None:
            environment["HALTEWECKER_EXTERNAL_BUILD_CACHE_PROVIDERS"] = allowlist
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            process_external_gtfs_sources(
                repository_root=REPOSITORY_ROOT,
                sources_path=sources_path,
                url_by_provider={},
                output=output,
                load_gtfs_archive=load_gtfs_archive,
                environ=environment,
                gtfs_cache=GTFSArtifactCache(root / "gtfs-cache"),
            )
        return output, stream.getvalue()

    def _cache_directory(self, root: Path) -> Path:
        return self._cache_directory_for(root, "cta-chicago")

    def _cache_directory_for(
        self,
        root: Path,
        provider_id: str,
    ) -> Path:
        directories = list(
            (root / "gtfs-cache" / "external-build" / provider_id).glob("*")
        )
        return next(
            path
            for path in directories
            if path.is_dir() and not path.name.startswith(".")
        )

    def _city_coordinates(
        self,
        source: dict[str, object],
        city_id: str,
    ) -> tuple[float, float]:
        cities = json.loads(
            (REPOSITORY_ROOT / str(source["cities"])).read_text(encoding="utf-8")
        )
        city = next(city for city in cities if city["id"] == city_id)
        return float(city["latitude"]), float(city["longitude"])

    def _build_once(self, root: Path, feed: Path) -> tuple[Path, str]:
        return self._run(root, feed, self._source(feed), output_name="first")

    def test_first_build_is_miss_and_writes_complete_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)

            with (
                mock.patch(
                    "external_gtfs.build_external_stop_packages",
                    wraps=external_gtfs.build_external_stop_packages,
                ) as stops,
                mock.patch(
                    "external_gtfs.build_external_route_index",
                    wraps=external_gtfs.build_external_route_index,
                ) as routes,
                mock.patch(
                    "external_gtfs.build_external_lines",
                    wraps=external_gtfs.build_external_lines,
                ) as lines,
            ):
                output, logs = self._build_once(root, feed)

            self.assertEqual(stops.call_count, 1)
            self.assertEqual(routes.call_count, 1)
            self.assertEqual(lines.call_count, 1)
            self.assertIn("source=cta-chicago stage=build-cache status=MISS", logs)
            self.assertIn("stage=cache-persist status=completed", logs)
            cache_directory = self._cache_directory(root)
            manifest = json.loads((cache_directory / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            self.assertTrue(manifest["complete"])
            self.assertEqual(
                {item["name"] for item in manifest["cachedOutputs"]},
                {"stops", "routes", "lineMembership"},
            )
            self.assertTrue((output / "stops/chicago.json").is_file())
            self.assertTrue((output / "routes/chicago.json").is_file())

    def test_second_identical_build_hits_and_rebuilds_departures_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            first_output, _ = self._build_once(root, feed)

            with (
                mock.patch(
                    "external_gtfs.build_external_stop_packages",
                    wraps=external_gtfs.build_external_stop_packages,
                ) as stops,
                mock.patch(
                    "external_gtfs.build_external_route_index",
                    wraps=external_gtfs.build_external_route_index,
                ) as routes,
                mock.patch(
                    "external_gtfs.build_external_departure_index",
                    wraps=external_gtfs.build_external_departure_index,
                ) as departures,
                mock.patch(
                    "external_gtfs.build_external_lines",
                    wraps=external_gtfs.build_external_lines,
                ) as lines,
            ):
                second_output, logs = self._run(
                    root, feed, self._source(feed), output_name="second"
                )

            self.assertEqual(stops.call_count, 0)
            self.assertEqual(routes.call_count, 0)
            self.assertEqual(lines.call_count, 0)
            self.assertEqual(departures.call_count, 1)
            self.assertIn("source=cta-chicago stage=build-cache status=HIT", logs)
            self.assertIn("stage=cache-restore status=completed", logs)
            self.assertEqual(
                (first_output / "stops/chicago.json").read_bytes(),
                (second_output / "stops/chicago.json").read_bytes(),
            )
            self.assertEqual(
                (first_output / "routes/chicago.json").read_bytes(),
                (second_output / "routes/chicago.json").read_bytes(),
            )
            first_departures = json.loads(
                (first_output / "departures/chicago.json").read_text()
            )
            second_departures = json.loads(
                (second_output / "departures/chicago.json").read_text()
            )
            first_departures.pop("generatedAt", None)
            second_departures.pop("generatedAt", None)
            self.assertEqual(first_departures, second_departures)

    def test_class_a_provider_uses_generic_cache_when_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "translink.zip"
            source = self._source_with_test_city(
                feed,
                provider_id="translink",
                city_id="vancouver",
            )
            latitude, longitude = self._city_coordinates(source, "vancouver")
            active_date = (
                datetime.now(ZoneInfo("America/Vancouver")).date().strftime("%Y%m%d")
            )
            _write_feed(
                feed,
                active_date=active_date,
                stop_lat=latitude,
                stop_lon=longitude,
            )

            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                first_output, first_logs = self._run(
                    root,
                    feed,
                    source,
                    allowlist="translink",
                    output_name="translink-first",
                )
            self.assertEqual(stops.call_count, 1)
            self.assertIn("source=translink stage=build-cache status=MISS", first_logs)

            with (
                mock.patch(
                    "external_gtfs.build_external_stop_packages",
                    wraps=external_gtfs.build_external_stop_packages,
                ) as stops,
                mock.patch(
                    "external_gtfs.build_external_route_index",
                    wraps=external_gtfs.build_external_route_index,
                ) as routes,
                mock.patch(
                    "external_gtfs.build_external_lines",
                    wraps=external_gtfs.build_external_lines,
                ) as lines,
                mock.patch(
                    "external_gtfs.build_external_departure_index",
                    wraps=external_gtfs.build_external_departure_index,
                ) as departures,
                mock.patch(
                    "external_gtfs.build_external_trip_index",
                    wraps=external_gtfs.build_external_trip_index,
                ) as trips,
            ):
                second_output, second_logs = self._run(
                    root,
                    feed,
                    source,
                    allowlist="translink",
                    output_name="translink-second",
                )

            self.assertEqual(stops.call_count, 0)
            self.assertEqual(routes.call_count, 0)
            self.assertEqual(lines.call_count, 0)
            self.assertEqual(departures.call_count, 1)
            self.assertEqual(trips.call_count, 1)
            self.assertIn("source=translink stage=build-cache status=HIT", second_logs)
            self.assertEqual(
                (first_output / "stops/vancouver.json").read_bytes(),
                (second_output / "stops/vancouver.json").read_bytes(),
            )
            self.assertEqual(
                (first_output / "routes/vancouver.json").read_bytes(),
                (second_output / "routes/vancouver.json").read_bytes(),
            )
            self.assertTrue(self._cache_directory_for(root, "translink").is_dir())

    def test_all_class_a_provider_policies_have_independent_cache_roots(self) -> None:
        providers = (
            ("cta-chicago", "chicago"),
            ("translink", "vancouver"),
            ("king-county-metro", "seattle"),
            ("stm-montreal", "montreal"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for provider_id, city_id in providers:
                feed = root / f"{provider_id}.zip"
                source = self._source_with_test_city(
                    feed,
                    provider_id=provider_id,
                    city_id=city_id,
                )
                latitude, longitude = self._city_coordinates(source, city_id)
                _write_feed(feed, stop_lat=latitude, stop_lon=longitude)
                _output, logs = self._run(
                    root,
                    feed,
                    source,
                    allowlist=provider_id,
                    output_name=f"{provider_id}-first",
                )
                self.assertIn(
                    f"source={provider_id} stage=build-cache status=MISS",
                    logs,
                )
                self.assertTrue(self._cache_directory_for(root, provider_id).is_dir())

    def test_allowlisted_providers_have_independent_hit_and_miss_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cta_feed = root / "cta.zip"
            translink_feed = root / "translink.zip"
            cta_source = self._source(cta_feed)
            translink_source = self._source_with_test_city(
                translink_feed,
                provider_id="translink",
                city_id="vancouver",
            )
            latitude, longitude = self._city_coordinates(translink_source, "vancouver")
            _write_feed(cta_feed)
            _write_feed(translink_feed, stop_lat=latitude, stop_lon=longitude)
            sources = [cta_source, translink_source]
            self._run_sources(
                root,
                sources,
                allowlist="cta-chicago,translink",
                output_name="both-first",
            )

            _write_feed(
                translink_feed, stop_lat=latitude, stop_lon=longitude, marker="2"
            )
            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                _output, logs = self._run_sources(
                    root,
                    sources,
                    allowlist="cta-chicago,translink",
                    output_name="both-second",
                )

            self.assertEqual(stops.call_count, 1)
            self.assertIn("source=cta-chicago stage=build-cache status=HIT", logs)
            self.assertIn("source=translink stage=build-cache status=MISS", logs)
            self.assertTrue((root / "both-second/stops/chicago.json").is_file())
            self.assertTrue((root / "both-second/stops/vancouver.json").is_file())

    def test_corrupting_one_provider_cache_does_not_affect_another(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cta_feed = root / "cta.zip"
            translink_feed = root / "translink.zip"
            cta_source = self._source(cta_feed)
            translink_source = self._source_with_test_city(
                translink_feed,
                provider_id="translink",
                city_id="vancouver",
            )
            latitude, longitude = self._city_coordinates(translink_source, "vancouver")
            _write_feed(cta_feed)
            _write_feed(translink_feed, stop_lat=latitude, stop_lon=longitude)
            sources = [cta_source, translink_source]
            self._run_sources(
                root,
                sources,
                allowlist="cta-chicago,translink",
                output_name="corruption-first",
            )
            (
                self._cache_directory_for(root, "cta-chicago") / "manifest.json"
            ).write_text("{bad-json", encoding="utf-8")

            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                _output, logs = self._run_sources(
                    root,
                    sources,
                    allowlist="cta-chicago,translink",
                    output_name="corruption-second",
                )

            self.assertEqual(stops.call_count, 1)
            self.assertIn("source=cta-chicago stage=build-cache status=INVALID", logs)
            self.assertIn("source=translink stage=build-cache status=HIT", logs)

    def test_namespaced_provider_cannot_use_class_a_cache_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "translink.zip"
            source = self._source_with_test_city(
                feed,
                provider_id="translink",
                city_id="vancouver",
            )
            source["namespace"] = "fixture:"
            source["mergeGroup"] = "fixture-shared"
            latitude, longitude = self._city_coordinates(source, "vancouver")
            active_date = (
                datetime.now(ZoneInfo("America/Vancouver")).date().strftime("%Y%m%d")
            )
            _write_feed(
                feed,
                active_date=active_date,
                stop_lat=latitude,
                stop_lon=longitude,
            )
            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                _output, logs = self._run(
                    root,
                    feed,
                    source,
                    allowlist="translink",
                    output_name="namespaced",
                )
            self.assertEqual(stops.call_count, 1)
            self.assertIn(
                "source=translink stage=build-cache status=DISABLED "
                "reason=namespaced-source-not-supported",
                logs,
            )
            self.assertFalse(
                (root / "gtfs-cache" / "external-build" / "translink").exists()
            )

    def test_gate_off_preserves_builder_path_and_does_not_create_derived_cache(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                _output, logs = self._run(
                    root, feed, self._source(feed), gate=False, output_name="off"
                )
            self.assertEqual(stops.call_count, 1)
            self.assertIn("source=cta-chicago stage=build-cache status=DISABLED", logs)
            self.assertFalse((root / "gtfs-cache" / "external-build").exists())

    def test_class_a_provider_without_allowlist_preserves_old_builder_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "translink.zip"
            source = self._source_with_test_city(
                feed,
                provider_id="translink",
                city_id="vancouver",
            )
            latitude, longitude = self._city_coordinates(source, "vancouver")
            _write_feed(feed, stop_lat=latitude, stop_lon=longitude)
            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                _output, logs = self._run(
                    root,
                    feed,
                    source,
                    output_name="translink-not-allowlisted",
                )
            self.assertEqual(stops.call_count, 1)
            self.assertIn(
                "source=translink stage=build-cache status=DISABLED "
                "reason=provider-not-allowlisted",
                logs,
            )
            self.assertFalse(
                (root / "gtfs-cache" / "external-build" / "translink").exists()
            )

    def test_cache_persist_failure_does_not_fail_ordinary_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            with mock.patch(
                "external_gtfs.ExternalBuildCache.persist",
                side_effect=OSError("simulated cache disk failure"),
            ):
                output, logs = self._build_once(root, feed)
            self.assertTrue((output / "stops/chicago.json").is_file())
            self.assertIn("stage=cache-persist status=failed", logs)

    def test_raw_sha_change_is_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_feed = root / "cta-first.zip"
            second_feed = root / "cta-second.zip"
            _write_feed(first_feed, marker="1")
            _write_feed(second_feed, marker="2")
            self._build_once(root, first_feed)
            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                _output, logs = self._run(
                    root,
                    second_feed,
                    self._source(second_feed),
                    output_name="raw-change",
                )
            self.assertEqual(stops.call_count, 1)
            self.assertIn("stage=build-cache status=MISS", logs)

    def test_provider_config_change_is_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            self._build_once(root, feed)
            changed_source = self._source(feed)
            changed_source["departurePackageDays"] = 4
            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                _output, logs = self._run(
                    root, feed, changed_source, output_name="config-change"
                )
            self.assertEqual(stops.call_count, 1)
            self.assertIn("stage=build-cache status=MISS", logs)

    def test_builder_fingerprint_change_is_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            self._build_once(root, feed)
            original_cache_key = external_gtfs.cache_key

            def changed_cache_key(**kwargs: object) -> CacheKey:
                original = original_cache_key(**kwargs)
                return CacheKey(
                    value=original.value[:-1] + "0"
                    if original.value[-1] != "0"
                    else original.value[:-1] + "1",
                    raw_sha256=original.raw_sha256,
                    provider_config_fingerprint=original.provider_config_fingerprint,
                    city_config_fingerprint=original.city_config_fingerprint,
                    builder_fingerprint="f" * 64,
                )

            with (
                mock.patch("external_gtfs.cache_key", side_effect=changed_cache_key),
                mock.patch(
                    "external_gtfs.build_external_stop_packages",
                    wraps=external_gtfs.build_external_stop_packages,
                ) as stops,
            ):
                _output, logs = self._run(
                    root, feed, self._source(feed), output_name="builder-change"
                )
            self.assertEqual(stops.call_count, 1)
            self.assertIn("stage=build-cache status=MISS", logs)

    def test_corrupt_manifest_is_invalid_then_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            self._build_once(root, feed)
            manifest_path = self._cache_directory(root) / "manifest.json"
            manifest_path.write_text("{not-json", encoding="utf-8")
            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                _output, logs = self._run(
                    root, feed, self._source(feed), output_name="bad-manifest"
                )
            self.assertEqual(stops.call_count, 1)
            self.assertIn("stage=build-cache status=INVALID", logs)

    def test_missing_cached_artifact_is_invalid_then_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            self._build_once(root, feed)
            (self._cache_directory(root) / "routes/chicago.json").unlink()
            with mock.patch(
                "external_gtfs.build_external_route_index",
                wraps=external_gtfs.build_external_route_index,
            ) as routes:
                _output, logs = self._run(
                    root, feed, self._source(feed), output_name="missing-artifact"
                )
            self.assertEqual(routes.call_count, 1)
            self.assertIn("stage=build-cache status=INVALID", logs)

    def test_wrong_cached_digest_is_invalid_then_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            self._build_once(root, feed)
            route_path = self._cache_directory(root) / "routes/chicago.json"
            route_path.write_text(
                route_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
            )
            with mock.patch(
                "external_gtfs.build_external_route_index",
                wraps=external_gtfs.build_external_route_index,
            ) as routes:
                _output, logs = self._run(
                    root, feed, self._source(feed), output_name="wrong-digest"
                )
            self.assertEqual(routes.call_count, 1)
            self.assertIn("stage=build-cache status=INVALID", logs)

    def test_date_change_hits_immutable_cache_and_rebuilds_departures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta-date.zip"
            today = datetime.now(ZoneInfo("America/Chicago")).date().strftime("%Y%m%d")
            _write_feed(feed, active_date=today)
            first_output, _ = self._build_once(root, feed)

            original_datetime = external_gtfs.datetime

            class NextDayDateTime(original_datetime):
                @classmethod
                def now(cls, tz=None):
                    current = original_datetime.now(tz)
                    return current + timedelta(days=4)

            with mock.patch("external_gtfs.datetime", NextDayDateTime):
                second_output, logs = self._run(
                    root, feed, self._source(feed), output_name="next-day"
                )

            self.assertIn("source=cta-chicago stage=build-cache status=HIT", logs)
            self.assertEqual(
                (first_output / "stops/chicago.json").read_bytes(),
                (second_output / "stops/chicago.json").read_bytes(),
            )
            first_departures = json.loads(
                (first_output / "departures/chicago.json").read_text()
            )
            second_departures = json.loads(
                (second_output / "departures/chicago.json").read_text()
            )
            self.assertTrue(first_departures["stops"])
            self.assertFalse(second_departures["stops"])

    def test_cache_hit_does_not_change_input_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            first_output, _ = self._build_once(root, feed)
            second_output, logs = self._run(
                root, feed, self._source(feed), output_name="provenance-hit"
            )
            first_provenance = json.loads(
                (first_output / "provenance/input-artifacts.json").read_text()
            )
            second_provenance = json.loads(
                (second_output / "provenance/input-artifacts.json").read_text()
            )
            self.assertIn("status=HIT", logs)
            self.assertEqual(first_provenance, second_provenance)

    def test_cached_line_membership_matches_full_builder_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            output, _ = self._build_once(root, feed)
            cache_directory = self._cache_directory(root)
            cached_lines = json.loads(
                (cache_directory / "line-membership.json").read_text()
            )["linesByStopID"]
            package_stops = json.loads((output / "stops/chicago.json").read_text())
            with zipfile.ZipFile(feed) as archive:
                full_lines = external_gtfs.build_external_lines(
                    archive,
                    {"chicago": package_stops},
                )
            self.assertEqual(cached_lines, full_lines)

    def test_other_provider_does_not_use_derived_cache_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "fixture.zip"
            _write_feed(feed)
            cities_path = root / "fixture-cities.json"
            cities_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "fixture-city",
                            "name": "Chicago",
                            "aliases": [],
                            "latitude": 41.8781,
                            "longitude": -87.6298,
                            "radiusMeters": 55_000,
                            "packageMode": "external",
                            "externalGTFSProvider": "fixture-provider",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            source = {
                "id": "fixture-provider",
                "url": str(feed),
                "cities": str(cities_path),
                "timezone": "America/Chicago",
                "identifierPrefix": "fixture:",
                "stopIDMode": "exact",
                "country": "US",
                "buildStops": True,
                "buildRoutes": True,
                "buildDepartures": True,
                "buildTripIndex": False,
            }
            _output, logs = self._run(root, feed, source, output_name="other-provider")
            self.assertNotIn("stage=build-cache", logs)
            self.assertFalse((root / "gtfs-cache" / "external-build").exists())

    def test_cache_key_isolated_by_provider_and_supplemental_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "fixture.zip"
            _write_feed(feed)
            cta_source = self._source(feed)
            translink_source = self._source_with_test_city(
                feed,
                provider_id="translink",
                city_id="vancouver",
            )
            cta_key = external_gtfs.cache_key(
                repository_root=REPOSITORY_ROOT,
                provider_id="cta-chicago",
                raw_sha256="a" * 64,
                source=cta_source,
                city_id="chicago",
            )
            translink_key = external_gtfs.cache_key(
                repository_root=REPOSITORY_ROOT,
                provider_id="translink",
                raw_sha256="a" * 64,
                source=translink_source,
                city_id="vancouver",
            )
            supplemental_key = external_gtfs.cache_key(
                repository_root=REPOSITORY_ROOT,
                provider_id="cta-chicago",
                raw_sha256="a" * 64,
                source=cta_source,
                city_id="chicago",
                supplemental_input_digests={"catalog": "b" * 64},
            )
            self.assertNotEqual(cta_key.value, translink_key.value)
            self.assertNotEqual(cta_key.value, supplemental_key.value)
            self.assertEqual(cta_key.provider_id, "cta-chicago")
            self.assertEqual(translink_key.provider_id, "translink")

    def test_partial_cache_candidate_is_not_restorable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = ExternalBuildCache(root / "cache")
            key = CacheKey("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64)
            partial_directory = cache.root / f".{key.value}.tmp-partial"
            partial_directory.mkdir(parents=True)
            (partial_directory / "manifest.json").write_text(
                json.dumps({"status": "complete", "complete": True}),
                encoding="utf-8",
            )
            lookup = cache.lookup(key)
            self.assertEqual(lookup.status, "MISS")
            self.assertTrue(partial_directory.exists())

            directory = cache.root / key.value
            directory.mkdir(parents=True)
            (directory / "stops").mkdir()
            (directory / "stops/chicago.json").write_text("[]", encoding="utf-8")
            lookup = cache.lookup(key)
            self.assertEqual(lookup.status, "INVALID")
            self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
