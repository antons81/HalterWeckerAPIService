import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import external_gtfs
from build_stop_packages import load_gtfs_archive
from external_build_cache import (
    CacheKey,
    CacheKeyUnavailable,
    ExternalBuildCache,
    projection_fingerprint,
)
from external_staging import ExternalDepartureStage, NormalizedProviderContext
from external_gtfs import load_external_gtfs_sources, process_external_gtfs_sources
from gtfs_source_cache import GTFSArtifactCache
from artifact_provenance import artifact_provenance

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _write_feed(
    path: Path,
    *,
    active_date: str | None = None,
    marker: str = "1",
    stop_lat: float = 41.8800,
    stop_lon: float = -87.6300,
    empty_trips: bool = False,
    agency_id: str | None = None,
    extra_stop: tuple[str, float, float] | None = None,
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        if agency_id is not None:
            archive.writestr(
                "agency.txt",
                f"agency_id,agency_name,agency_url,agency_timezone\n{agency_id},Fixture,http://fixture.test,America/New_York\n",
            )
        stop_rows = (
            "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type\n"
            f"station,Station,{stop_lat:.6f},{stop_lon:.6f},,1\n"
            f"platform,Platform,{stop_lat:.6f},{stop_lon:.6f},station,0\n"
            f"street,Street Stop,{stop_lat + 0.001:.6f},{stop_lon - 0.001:.6f},,0\n"
        )
        if extra_stop is not None:
            stop_rows += (
                f"{extra_stop[0]},Second City Stop,{extra_stop[1]:.6f},"
                f"{extra_stop[2]:.6f},,0\n"
            )
        archive.writestr("stops.txt", stop_rows)
        archive.writestr(
            "routes.txt",
            (
                "route_id,route_short_name,route_long_name,route_type,agency_id\n"
                if agency_id is not None
                else "route_id,route_short_name,route_long_name,route_type\n"
            )
            + (
                f"R{marker},17,Line {marker},0,{agency_id}\n"
                if agency_id is not None
                else f"R{marker},17,Line {marker},0\n"
            ),
        )
        archive.writestr(
            "trips.txt",
            (
                "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                if empty_trips
                else f"route_id,service_id,trip_id,trip_headsign,direction_id\nR{marker},S1,T{marker},Terminal {marker},0\n"
            ),
        )
        stop_time_rows = (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            if empty_trips
            else f"trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            f"T{marker},08:00:00,08:00:00,platform,1\n"
            f"T{marker},08:10:00,08:10:00,street,2\n"
        )
        if extra_stop is not None and not empty_trips:
            stop_time_rows += f"T{marker},08:20:00,08:20:00,{extra_stop[0]},3\n"
        archive.writestr("stop_times.txt", stop_time_rows)
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


def _write_headsign_window_feed(
    path: Path,
    *,
    first_date: str,
    second_date: str,
    stop_lat: float,
    stop_lon: float,
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type\n"
            f"platform,Platform,{stop_lat:.6f},{stop_lon:.6f},,0\n",
        )
        archive.writestr(
            "routes.txt",
            "route_id,route_short_name,route_long_name,route_type\n"
            "R1,1,Fixture Line,3\n",
        )
        archive.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,trip_headsign,direction_id\n"
            "R1,S1,T1,First destination,0\n"
            "R1,S2,T2,Second destination,0\n",
        )
        archive.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,08:00:00,08:00:00,platform,1\n"
            "T2,09:00:00,09:00:00,platform,1\n",
        )
        archive.writestr(
            "calendar_dates.txt",
            "service_id,date,exception_type\n"
            f"S1,{first_date},1\n"
            f"S2,{second_date},1\n",
        )


def _write_route_field_feed(
    path: Path,
    *,
    route_header: str,
    route_row: str,
    stop_lat: float,
    stop_lon: float,
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type\n"
            f"platform,Platform,{stop_lat:.6f},{stop_lon:.6f},,0\n"
            f"street,Street,{stop_lat + 0.001:.6f},{stop_lon - 0.001:.6f},,0\n",
        )
        archive.writestr("routes.txt", f"{route_header}\n{route_row}\n")
        archive.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,trip_headsign,direction_id\n"
            "R1,S1,T1,Terminal,0\n",
        )
        archive.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,08:00:00,08:00:00,platform,1\n"
            "T1,08:10:00,08:10:00,street,2\n",
        )
        archive.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "S1,1,1,1,1,1,1,1,20200101,20301231\n",
        )


class ExternalBuildCacheTests(unittest.TestCase):
    def _write_cities(
        self,
        path: Path,
        provider_ids: list[str],
        city_ids: tuple[str, ...] = ("fixture-city",),
        coordinates: tuple[tuple[float, float], ...] | None = None,
    ) -> None:
        points = coordinates or tuple((0.0, 0.0) for _ in city_ids)
        self.assertEqual(len(points), len(city_ids))
        path.write_text(
            json.dumps(
                [
                    {
                        "id": city_id,
                        "name": city_id,
                        "aliases": [],
                        "latitude": latitude,
                        "longitude": longitude,
                        "radiusMeters": 100_000,
                        "timezone": "UTC",
                        "packageMode": "external",
                        "externalGTFSProvider": provider_ids[0],
                        "externalGTFSProviders": provider_ids,
                    }
                    for city_id, (latitude, longitude) in zip(
                        city_ids, points, strict=True
                    )
                ]
            ),
            encoding="utf-8",
        )

    def _immutable_output_bytes(
        self,
        output: Path,
        city_ids: tuple[str, ...],
    ) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for city_id in city_ids:
            for directory in ("stops", "routes", "trips"):
                path = output / directory / f"{city_id}.json"
                if path.is_file():
                    result[path.relative_to(output).as_posix()] = path.read_bytes()
        return result

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
        repository_root: Path = REPOSITORY_ROOT,
        gate: bool = True,
        allowlist: str | None = None,
        transformed_gate: bool = False,
        output_name: str = "output",
        use_normalized_context: bool = True,
    ) -> tuple[Path, str]:
        sources_path = root / f"sources-{output_name}.json"
        sources_path.write_text(json.dumps([source]), encoding="utf-8")
        output = root / output_name
        environment = {"HALTEWECKER_EXTERNAL_BUILD_CACHE": "1" if gate else "0"}
        environment["HALTEWECKER_EXTERNAL_TRANSFORMED_BUILD_CACHE"] = (
            "1" if transformed_gate else "0"
        )
        if allowlist is not None:
            environment["HALTEWECKER_EXTERNAL_BUILD_CACHE_PROVIDERS"] = allowlist
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            process_external_gtfs_sources(
                repository_root=repository_root,
                sources_path=sources_path,
                url_by_provider={},
                output=output,
                load_gtfs_archive=load_gtfs_archive,
                environ=environment,
                gtfs_cache=GTFSArtifactCache(root / "gtfs-cache"),
                use_normalized_context=use_normalized_context,
            )
        return output, stream.getvalue()

    def _run_sources(
        self,
        root: Path,
        sources: list[dict[str, object]],
        *,
        repository_root: Path = REPOSITORY_ROOT,
        gate: bool = True,
        allowlist: str | None = None,
        transformed_gate: bool = False,
        output_name: str = "output",
    ) -> tuple[Path, str]:
        sources_path = root / f"sources-{output_name}.json"
        sources_path.write_text(json.dumps(sources), encoding="utf-8")
        output = root / output_name
        environment = {"HALTEWECKER_EXTERNAL_BUILD_CACHE": "1" if gate else "0"}
        environment["HALTEWECKER_EXTERNAL_TRANSFORMED_BUILD_CACHE"] = (
            "1" if transformed_gate else "0"
        )
        if allowlist is not None:
            environment["HALTEWECKER_EXTERNAL_BUILD_CACHE_PROVIDERS"] = allowlist
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            process_external_gtfs_sources(
                repository_root=repository_root,
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

    def _test_repository(
        self,
        root: Path,
        cities_path: Path,
        provider_ids: list[str],
        city_ids: tuple[str, ...] = ("fixture-city",),
        coordinates: tuple[tuple[float, float], ...] | None = None,
    ) -> Path:
        repository_root = root / "repository"
        (repository_root / "scripts").mkdir(parents=True)
        (repository_root / "config").mkdir()
        for relative_script in (
            "external_gtfs.py",
            "external_staging.py",
            "gtfs_csv.py",
            "build_stop_packages.py",
            "external_build_cache.py",
        ):
            shutil.copyfile(
                REPOSITORY_ROOT / "scripts" / relative_script,
                repository_root / "scripts" / relative_script,
            )
        self._write_cities(
            cities_path,
            provider_ids,
            city_ids,
            coordinates,
        )
        return repository_root

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

    def test_normalized_context_reads_each_gtfs_table_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "context.zip"
            _write_feed(
                feed,
                active_date=datetime.now(ZoneInfo("America/Vancouver"))
                .date()
                .strftime("%Y%m%d"),
            )
            source = self._source_with_test_city(
                feed,
                provider_id="translink",
                city_id="vancouver",
            )
            latitude, longitude = self._city_coordinates(source, "vancouver")
            _write_feed(
                feed,
                active_date=datetime.now(ZoneInfo("America/Vancouver"))
                .date()
                .strftime("%Y%m%d"),
                stop_lat=latitude,
                stop_lon=longitude,
            )
            output = root / "context-output"
            cities = json.loads(
                (REPOSITORY_ROOT / str(source["cities"])).read_text(encoding="utf-8")
            )

            class CountingArchive:
                def __init__(self, archive: zipfile.ZipFile) -> None:
                    self.archive = archive
                    self.open_counts: dict[str, int] = {}

                def namelist(self):
                    return self.archive.namelist()

                def open(self, name: str):
                    self.open_counts[name] = self.open_counts.get(name, 0) + 1
                    return self.archive.open(name)

            with zipfile.ZipFile(feed) as archive:
                counted = CountingArchive(archive)
                context = NormalizedProviderContext.from_archive(counted)
                try:
                    external_gtfs.build_external_stop_packages(
                        counted, cities, output, context=context
                    )
                    external_gtfs.build_external_route_index(
                        counted, cities, output, context=context
                    )
                    external_gtfs.build_external_departure_index(
                        counted, cities, output, timezone_name="America/Vancouver", context=context
                    )
                    external_gtfs.build_external_trip_index(
                        counted, cities, output, context=context
                    )
                    external_gtfs.build_external_lines(
                        counted,
                        {str(cities[0]["id"]): json.loads(
                            (output / "stops/vancouver.json").read_text()
                        )},
                        context=context,
                    )
                finally:
                    context.close()

            self.assertEqual(
                counted.open_counts,
                {
                    "routes.txt": 1,
                    "stops.txt": 1,
                    "trips.txt": 1,
                    "stop_times.txt": 1,
                    "calendar_dates.txt": 1,
                },
            )

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
            self.assertIn(
                "reusedOutputs=stops,routes,lineMembership recomputedOutputs=departures",
                logs,
            )
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

    def test_cache_hit_bypasses_normalized_provider_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            self._build_once(root, feed)

            with mock.patch(
                "external_gtfs.NormalizedProviderContext.from_archive",
                wraps=NormalizedProviderContext.from_archive,
            ) as context_factory:
                _output, logs = self._run(
                    root, feed, self._source(feed), output_name="context-free-hit"
                )

            self.assertIn("source=cta-chicago stage=build-cache status=HIT", logs)
            self.assertEqual(context_factory.call_count, 0)

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
            self.assertEqual(trips.call_count, 0)
            self.assertIn("source=translink stage=build-cache status=HIT", second_logs)
            self.assertEqual(
                (first_output / "stops/vancouver.json").read_bytes(),
                (second_output / "stops/vancouver.json").read_bytes(),
            )
            self.assertEqual(
                (first_output / "routes/vancouver.json").read_bytes(),
                (second_output / "routes/vancouver.json").read_bytes(),
            )
            self.assertEqual(
                (first_output / "trips/vancouver.json").read_bytes(),
                (second_output / "trips/vancouver.json").read_bytes(),
            )
            self.assertTrue(self._cache_directory_for(root, "translink").is_dir())

            manifest = json.loads(
                (
                    self._cache_directory_for(root, "translink") / "manifest.json"
                ).read_text()
            )
            self.assertIn(
                "tripIndexBase",
                {item["name"] for item in manifest["cachedOutputs"]},
            )

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

    def test_namespaced_provider_cache_has_full_immutable_parity(self) -> None:
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
            first_output, _first_logs = self._run(
                root,
                feed,
                source,
                allowlist="translink",
                transformed_gate=True,
                output_name="namespaced-first",
            )
            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                second_output, logs = self._run(
                    root,
                    feed,
                    source,
                    allowlist="translink",
                    transformed_gate=True,
                    output_name="namespaced-second",
                )
            self.assertEqual(stops.call_count, 0)
            self.assertIn("stage=build-cache status=HIT", logs)
            self.assertIn(
                "source=translink stage=cache-restore status=completed",
                logs,
            )
            for relative in (
                "stops/vancouver.json",
                "routes/vancouver.json",
                "trips/vancouver.json",
            ):
                self.assertEqual(
                    (first_output / relative).read_bytes(),
                    (second_output / relative).read_bytes(),
                )

    def test_agency_scoped_provider_cache_has_full_immutable_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "mbta.zip"
            source = self._source_with_test_city(
                feed,
                provider_id="mbta-boston",
                city_id="boston",
            )
            latitude, longitude = self._city_coordinates(source, "boston")
            active_date = (
                datetime.now(ZoneInfo("America/New_York")).date().strftime("%Y%m%d")
            )
            _write_feed(
                feed,
                active_date=active_date,
                stop_lat=latitude,
                stop_lon=longitude,
                agency_id="1",
            )
            first_output, _ = self._run(
                root,
                feed,
                source,
                allowlist="mbta-boston",
                transformed_gate=True,
                output_name="agency-first",
            )
            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                second_output, logs = self._run(
                    root,
                    feed,
                    source,
                    allowlist="mbta-boston",
                    transformed_gate=True,
                    output_name="agency-second",
                )
            self.assertEqual(stops.call_count, 0)
            self.assertIn("source=mbta-boston stage=build-cache status=HIT", logs)
            for relative in (
                "stops/boston.json",
                "routes/boston.json",
                "trips/boston.json",
            ):
                self.assertEqual(
                    (first_output / relative).read_bytes(),
                    (second_output / relative).read_bytes(),
                )
            changed_feed = root / "mbta-agency-2.zip"
            _write_feed(
                changed_feed,
                active_date=datetime.now(ZoneInfo("America/New_York"))
                .date()
                .strftime("%Y%m%d"),
                stop_lat=latitude,
                stop_lon=longitude,
                agency_id="2",
            )
            changed_source = dict(
                source,
                agencyID="2",
                url=str(changed_feed),
            )
            _output, changed_logs = self._run(
                root,
                changed_feed,
                changed_source,
                allowlist="mbta-boston",
                transformed_gate=True,
                output_name="agency-selector-change",
            )
            self.assertIn(
                "source=mbta-boston stage=build-cache status=MISS",
                changed_logs,
            )

    def test_merge_group_cache_preserves_membership_and_invalidates_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "merge.zip"
            repository_root = self._test_repository(
                root,
                root / "repository/config/merge-cities.json",
                ["cta-chicago", "translink"],
            )
            _write_feed(
                feed,
                active_date=datetime.now(ZoneInfo("UTC")).date().strftime("%Y%m%d"),
                stop_lat=0.0,
                stop_lon=0.0,
            )
            cta = self._source(
                feed,
                provider_id="cta-chicago",
                cities_path="config/merge-cities.json",
            )
            cta["namespace"] = "cta:"
            cta["mergeGroup"] = "fixture-merge"
            cta["timezone"] = "UTC"
            translink = self._source(
                feed,
                provider_id="translink",
                city_id="fixture-city",
                cities_path="config/merge-cities.json",
            )
            translink["namespace"] = "trans:"
            translink["mergeGroup"] = "fixture-merge"
            translink["timezone"] = "UTC"
            sources = [cta, translink]
            first_output, _ = self._run_sources(
                root,
                sources,
                repository_root=repository_root,
                allowlist="cta-chicago,translink",
                transformed_gate=True,
                output_name="merge-first",
            )
            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                second_output, hit_logs = self._run_sources(
                    root,
                    sources,
                    repository_root=repository_root,
                    allowlist="cta-chicago,translink",
                    transformed_gate=True,
                    output_name="merge-hit",
                )
            self.assertEqual(stops.call_count, 0)
            self.assertEqual(
                self._immutable_output_bytes(
                    first_output,
                    ("fixture-city",),
                ),
                self._immutable_output_bytes(second_output, ("fixture-city",)),
            )
            routes = json.loads(
                (second_output / "routes/fixture-city.json").read_text()
            )
            self.assertEqual(set(routes), {"cta:R1", "trans:R1"})
            self.assertIn("source=cta-chicago stage=build-cache status=HIT", hit_logs)
            self.assertIn("source=translink stage=build-cache status=HIT", hit_logs)

            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                _output, order_logs = self._run_sources(
                    root,
                    list(reversed(sources)),
                    repository_root=repository_root,
                    allowlist="cta-chicago,translink",
                    transformed_gate=True,
                    output_name="merge-order-change",
                )
            self.assertEqual(stops.call_count, 2)
            self.assertIn(
                "source=cta-chicago stage=build-cache status=MISS",
                order_logs,
            )
            self.assertIn(
                "source=translink stage=build-cache status=MISS",
                order_logs,
            )

            changed_group = [
                dict(source, mergeGroup="fixture-merge-v2") for source in sources
            ]
            _output, config_logs = self._run_sources(
                root,
                changed_group,
                repository_root=repository_root,
                allowlist="cta-chicago,translink",
                transformed_gate=True,
                output_name="merge-config-change",
            )
            self.assertIn(
                "source=cta-chicago stage=build-cache status=MISS",
                config_logs,
            )
            self.assertIn(
                "source=translink stage=build-cache status=MISS",
                config_logs,
            )

            _write_feed(
                feed,
                active_date=datetime.now(ZoneInfo("UTC")).date().strftime("%Y%m%d"),
                marker="2",
                stop_lat=0.0,
                stop_lon=0.0,
            )
            _output, member_logs = self._run_sources(
                root,
                sources,
                repository_root=repository_root,
                allowlist="cta-chicago,translink",
                transformed_gate=True,
                output_name="merge-member-change",
            )
            self.assertIn(
                "source=cta-chicago stage=build-cache status=MISS",
                member_logs,
            )
            self.assertIn(
                "source=translink stage=build-cache status=MISS",
                member_logs,
            )

    def test_filtered_city_selection_change_is_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "finland.zip"
            repository_root = self._test_repository(
                root,
                root / "repository/config/finland-cities.json",
                ["finland-hsl"],
                ("alpha",),
            )
            _write_feed(
                feed,
                active_date=datetime.now(ZoneInfo("Europe/Helsinki"))
                .date()
                .strftime("%Y%m%d"),
                stop_lat=0.0,
                stop_lon=0.0,
            )
            source = self._source(
                feed,
                provider_id="finland-hsl",
                city_id="fixture-city",
                cities_path="config/finland-cities.json",
            )
            first_output, _ = self._run(
                root,
                feed,
                source,
                repository_root=repository_root,
                allowlist="finland-hsl",
                transformed_gate=True,
                output_name="filter-first",
            )
            self._write_cities(
                repository_root / "config/finland-cities.json",
                ["finland-hsl"],
                ("beta",),
            )
            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                second_output, city_logs = self._run(
                    root,
                    feed,
                    source,
                    repository_root=repository_root,
                    allowlist="finland-hsl",
                    transformed_gate=True,
                    output_name="filter-city-change",
                )
            self.assertEqual(stops.call_count, 1)
            self.assertIn("source=finland-hsl stage=build-cache status=MISS", city_logs)
            self.assertTrue((first_output / "stops/alpha.json").is_file())
            self.assertTrue((second_output / "stops/beta.json").is_file())

            source["filterCitiesByProvider"] = False
            _output, filter_logs = self._run(
                root,
                feed,
                source,
                repository_root=repository_root,
                allowlist="finland-hsl",
                transformed_gate=True,
                output_name="filter-config-change",
            )
            self.assertIn(
                "source=finland-hsl stage=build-cache status=MISS",
                filter_logs,
            )

    def test_multi_city_cache_has_per_city_output_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "sweden.zip"
            repository_root = self._test_repository(
                root,
                root / "repository/config/sweden-cities.json",
                ["sweden"],
                ("alpha", "beta"),
                ((0.0, 0.0), (5.0, 5.0)),
            )
            _write_feed(
                feed,
                active_date=datetime.now(ZoneInfo("UTC")).date().strftime("%Y%m%d"),
                stop_lat=0.0,
                stop_lon=0.0,
                extra_stop=("beta-stop", 5.0, 5.0),
            )
            source = self._source(
                feed,
                provider_id="sweden",
                city_id="alpha",
                cities_path="config/sweden-cities.json",
            )
            first_output, _ = self._run(
                root,
                feed,
                source,
                repository_root=repository_root,
                allowlist="sweden",
                transformed_gate=True,
                output_name="multi-city-first",
            )
            second_output, logs = self._run(
                root,
                feed,
                source,
                repository_root=repository_root,
                allowlist="sweden",
                transformed_gate=True,
                output_name="multi-city-hit",
            )
            self.assertIn("source=sweden stage=build-cache status=HIT", logs)
            self.assertEqual(
                self._immutable_output_bytes(first_output, ("alpha", "beta")),
                self._immutable_output_bytes(second_output, ("alpha", "beta")),
            )
            alpha_stops = json.loads((second_output / "stops/alpha.json").read_text())
            beta_stops = json.loads((second_output / "stops/beta.json").read_text())
            self.assertTrue(any(item.get("id") == "beta-stop" for item in beta_stops))
            self.assertFalse(any(item.get("id") == "beta-stop" for item in alpha_stops))

    def test_exclusive_partition_cache_has_parity_and_config_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "australia.zip"
            repository_root = self._test_repository(
                root,
                root / "repository/config/australia-cities.json",
                ["australia-transport-nsw"],
            )
            _write_feed(
                feed,
                active_date=datetime.now(ZoneInfo("UTC")).date().strftime("%Y%m%d"),
                stop_lat=0.0,
                stop_lon=0.0,
            )
            source = self._source(
                feed,
                provider_id="australia-transport-nsw",
                city_id="fixture-city",
                cities_path="config/australia-cities.json",
            )
            first_output, _ = self._run(
                root,
                feed,
                source,
                repository_root=repository_root,
                allowlist="australia-transport-nsw",
                transformed_gate=True,
                output_name="exclusive-first",
            )
            second_output, hit_logs = self._run(
                root,
                feed,
                source,
                repository_root=repository_root,
                allowlist="australia-transport-nsw",
                transformed_gate=True,
                output_name="exclusive-hit",
            )
            self.assertIn(
                "source=australia-transport-nsw stage=build-cache status=HIT",
                hit_logs,
            )
            self.assertEqual(
                self._immutable_output_bytes(first_output, ("fixture-city",)),
                self._immutable_output_bytes(second_output, ("fixture-city",)),
            )
            source["exclusiveCityPartition"] = False
            _output, config_logs = self._run(
                root,
                feed,
                source,
                repository_root=repository_root,
                allowlist="australia-transport-nsw",
                transformed_gate=True,
                output_name="exclusive-config-change",
            )
            self.assertIn(
                "source=australia-transport-nsw stage=build-cache status=MISS",
                config_logs,
            )

    def test_local_path_uses_content_digest_and_ireland_stays_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "mta.zip"
            repository_root = self._test_repository(
                root,
                root / "repository/config/mta-cities.json",
                ["mta-ny-nyct-bus"],
            )
            _write_feed(
                feed,
                active_date=datetime.now(ZoneInfo("America/New_York"))
                .date()
                .strftime("%Y%m%d"),
                stop_lat=0.0,
                stop_lon=0.0,
            )
            source = self._source(
                feed,
                provider_id="mta-ny-nyct-bus",
                city_id="fixture-city",
                cities_path="config/mta-cities.json",
            )
            source["url"] = None
            source["localPath"] = str(feed)
            first_output, _ = self._run(
                root,
                feed,
                source,
                repository_root=repository_root,
                allowlist="mta-ny-nyct-bus",
                transformed_gate=True,
                output_name="local-path-first",
            )
            second_output, hit_logs = self._run(
                root,
                feed,
                source,
                repository_root=repository_root,
                allowlist="mta-ny-nyct-bus",
                transformed_gate=True,
                output_name="local-path-hit",
            )
            self.assertIn(
                "source=mta-ny-nyct-bus stage=build-cache status=HIT",
                hit_logs,
            )
            self.assertEqual(
                self._immutable_output_bytes(first_output, ("fixture-city",)),
                self._immutable_output_bytes(second_output, ("fixture-city",)),
            )
            _write_feed(
                feed,
                active_date=datetime.now(ZoneInfo("America/New_York"))
                .date()
                .strftime("%Y%m%d"),
                marker="2",
                stop_lat=0.0,
                stop_lon=0.0,
            )
            _output, changed_logs = self._run(
                root,
                feed,
                source,
                repository_root=repository_root,
                allowlist="mta-ny-nyct-bus",
                transformed_gate=True,
                output_name="local-path-content-change",
            )
            self.assertIn(
                "source=mta-ny-nyct-bus stage=build-cache status=MISS",
                changed_logs,
            )
            ireland = self._source(feed, provider_id="ireland", city_id="galway")
            cities = external_gtfs.load_external_cities(ireland, REPOSITORY_ROOT)
            eligible, reason, _city_id = external_gtfs._external_cache_is_eligible(
                ireland,
                cities,
            )
            self.assertFalse(eligible)
            self.assertEqual(reason, "provider-not-in-reviewed-transform-class")

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

    def test_missing_cached_trip_index_is_invalid_then_falls_back(self) -> None:
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
            self._run(
                root,
                feed,
                source,
                allowlist="translink",
                output_name="trip-index-first",
            )
            (
                self._cache_directory_for(root, "translink")
                / "trip-index-base/vancouver.json"
            ).unlink()

            with mock.patch(
                "external_gtfs.build_external_trip_index",
                wraps=external_gtfs.build_external_trip_index,
            ) as trips:
                _output, logs = self._run(
                    root,
                    feed,
                    source,
                    allowlist="translink",
                    output_name="trip-index-missing",
                )

            self.assertEqual(trips.call_count, 1)
            self.assertIn("stage=build-cache status=INVALID", logs)

    def test_v3_trip_index_base_with_headsign_is_invalid_then_falls_back(self) -> None:
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
            self._run(
                root,
                feed,
                source,
                allowlist="translink",
                output_name="stale-base-first",
            )
            cache_directory = self._cache_directory_for(root, "translink")
            base_path = cache_directory / "trip-index-base/vancouver.json"
            base_path.write_text(
                json.dumps({"T1": {"r": "R1", "h": "stale"}}),
                encoding="utf-8",
            )
            manifest_path = cache_directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            digest, size = artifact_provenance(base_path)
            for entry in manifest["cachedOutputs"]:
                if entry["path"] == "trip-index-base/vancouver.json":
                    entry["sha256"] = digest
                    entry["size"] = size
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with mock.patch(
                "external_gtfs.build_external_trip_index",
                wraps=external_gtfs.build_external_trip_index,
            ) as trips:
                _output, logs = self._run(
                    root,
                    feed,
                    source,
                    allowlist="translink",
                    output_name="stale-base",
                )

            self.assertEqual(trips.call_count, 1)
            self.assertIn(
                "stage=build-cache status=INVALID reason=immutable trip index base is invalid",
                logs,
            )

    def test_v1_and_v2_cache_schemas_are_invalid_then_fall_back(self) -> None:
        for schema_version in (1, 2):
            with self.subTest(schema_version=schema_version):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    feed = root / "cta.zip"
                    _write_feed(feed)
                    self._build_once(root, feed)
                    manifest_path = self._cache_directory(root) / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["cacheSchemaVersion"] = schema_version
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                    with mock.patch(
                        "external_gtfs.build_external_stop_packages",
                        wraps=external_gtfs.build_external_stop_packages,
                    ) as stops:
                        _output, logs = self._run(
                            root,
                            feed,
                            self._source(feed),
                            output_name=f"schema-v{schema_version}",
                        )

                    self.assertEqual(stops.call_count, 1)
                    self.assertIn(
                        "stage=build-cache status=INVALID reason=cache schema mismatch",
                        logs,
                    )

    def test_normalized_context_matches_legacy_malformed_conversions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "malformed.zip"
            with zipfile.ZipFile(feed, "w") as archive:
                archive.writestr(
                    "calendar_dates.txt",
                    "service_id,date,exception_type\n"
                    "S1,20260101,1\n"
                    "S1,20260102,2\n"
                    "S1,20260103,bad\n"
                    "S1,20260104,\n"
                    "S1,20260105,   \n",
                )
                archive.writestr(
                    "stop_times.txt",
                    "trip_id,stop_id,arrival_time,departure_time,stop_sequence\n"
                    "T1,S,00:00:00,00:00:00,1\n"
                    "T1,S,23:59:59,23:59:59,+1\n"
                    "T1,S,24:00:00,24:00:00,001\n"
                    "T1,S,25:15:00,25:15:00,0\n"
                    "T1,S,48:00:00,48:00:00,-1\n"
                    "T1,S,, ,\n"
                    "T1,S,bad,bad,bad\n",
                )
                archive.writestr(
                    "routes.txt",
                    "route_id,route_short_name\nR1,first\nR1,last\n",
                )
                archive.writestr(
                    "stops.txt",
                    "stop_id,stop_name,stop_lat,stop_lon\nS,first,0,0\nS,last,0,0\n",
                )
                archive.writestr(
                    "trips.txt",
                    "trip_id,route_id,service_id\nT1,R1,S1\nT1,R1,S1\n",
                )

            with zipfile.ZipFile(feed) as archive:
                legacy_calendar = external_gtfs._service_calendar_from_source(
                    archive, None
                )
                context = NormalizedProviderContext.from_archive(archive)
                try:
                    self.assertEqual(context.service_calendar(), legacy_calendar)
                    self.assertEqual(
                        [
                            row["stop_sequence"]
                            for row in context.iter_table("stop_times.txt")
                        ],
                        [1, 1, 1, 0, -1, 0, 0],
                    )
                    self.assertEqual(
                        [
                            row["route_short_name"]
                            for row in context.iter_table("routes.txt")
                        ],
                        ["first", "last"],
                    )
                    self.assertEqual(
                        [row["stop_name"] for row in context.iter_table("stops.txt")],
                        ["first", "last"],
                    )
                    self.assertEqual(
                        len(list(context.iter_table("trips.txt"))),
                        2,
                    )
                finally:
                    context.close()

    def test_calendar_dates_conversion_matrix_matches_legacy(self) -> None:
        values = ("1", "2", "bad", "", " ", "+1", "01", "-1")
        expected = (1, 2, None, 0, None, 1, 1, -1)
        with tempfile.TemporaryDirectory() as temporary:
            feed = Path(temporary) / "calendar-dates-matrix.zip"
            with zipfile.ZipFile(feed, "w") as archive:
                archive.writestr(
                    "calendar_dates.txt",
                    "service_id,date,exception_type\n"
                    + "".join(
                        f"S1,202601{index:02d},{value}\n"
                        for index, value in enumerate(values, start=1)
                    ),
                )

            with zipfile.ZipFile(feed) as archive:
                legacy = external_gtfs._service_calendar_from_source(archive, None)
                context = NormalizedProviderContext.from_archive(archive)
                try:
                    self.assertEqual(context.service_calendar(), legacy)
                    self.assertEqual(
                        legacy["S1"]["exceptions"],
                        {
                            f"202601{index:02d}": value
                            for index, value in enumerate(expected, start=1)
                            if value is not None
                        },
                    )
                finally:
                    context.close()

    def test_route_fields_preserve_legacy_whitespace_and_missing_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed = root / "route-field-seed.zip"
            source = self._source_with_test_city(
                seed,
                provider_id="translink",
                city_id="vancouver",
            )
            latitude, longitude = self._city_coordinates(source, "vancouver")
            cases = (
                (
                    "spaced",
                    "route_id,route_short_name,route_long_name,route_type",
                    "R1, 17 , Line 1  ,3",
                ),
                (
                    "plain",
                    "route_id,route_short_name,route_long_name,route_type",
                    "R1,17,Line 1,3",
                ),
                (
                    "empty",
                    "route_id,route_short_name,route_long_name,route_type",
                    "R1,,,3",
                ),
                (
                    "whitespace",
                    "route_id,route_short_name,route_long_name,route_type",
                    "R1, , ,3",
                ),
                (
                    "missing-columns",
                    "route_id,route_type",
                    "R1,3",
                ),
                (
                    "trailing-missing",
                    "route_id,route_short_name,route_long_name,route_type",
                    "R1,17",
                ),
            )
            for name, route_header, route_row in cases:
                feed = root / f"route-fields-{name}.zip"
                _write_route_field_feed(
                    feed,
                    route_header=route_header,
                    route_row=route_row,
                    stop_lat=latitude,
                    stop_lon=longitude,
                )
                outputs: list[bytes] = []
                for mode in ("legacy", "d1"):
                    output = root / f"route-fields-{name}-{mode}"
                    archive = load_gtfs_archive(str(feed))
                    context = (
                        None
                        if mode == "legacy"
                        else NormalizedProviderContext.from_archive(archive)
                    )
                    try:
                        _manifest, _packages = (
                            external_gtfs.build_external_stop_packages(
                                archive,
                                external_gtfs.load_external_cities(
                                    source, REPOSITORY_ROOT
                                ),
                                output,
                                context=context,
                            )
                        )
                        external_gtfs.build_external_route_index(
                            archive,
                            external_gtfs.load_external_cities(source, REPOSITORY_ROOT),
                            output,
                            context=context,
                        )
                        outputs.append((output / "routes/vancouver.json").read_bytes())
                    finally:
                        if context is not None:
                            context.close()
                        archive.close()
                self.assertEqual(outputs[0], outputs[1], name)

    def test_duplicate_rows_have_raw_and_context_output_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed = root / "duplicate-seed.zip"
            source = self._source_with_test_city(
                seed,
                provider_id="translink",
                city_id="vancouver",
            )
            latitude, longitude = self._city_coordinates(source, "vancouver")

            def write_fixture(
                path: Path,
                routes: list[str],
                stops: list[str],
                trips: list[str],
            ) -> None:
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(
                        "stops.txt",
                        "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type\n"
                        + "".join(
                            f"platform,{name},{latitude:.6f},{longitude:.6f},,0\n"
                            for name in stops
                        )
                        + f"street,Street,{latitude + 0.001:.6f},{longitude - 0.001:.6f},,0\n",
                    )
                    archive.writestr(
                        "routes.txt",
                        "route_id,route_short_name,route_long_name,route_type\n"
                        + "".join(routes),
                    )
                    archive.writestr(
                        "trips.txt",
                        "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                        + "".join(trips),
                    )
                    archive.writestr(
                        "stop_times.txt",
                        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                        "T1,08:00:00,08:00:00,platform,1\n"
                        "T1,08:10:00,08:10:00,street,2\n",
                    )
                    archive.writestr(
                        "calendar.txt",
                        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                        "S1,1,1,1,1,1,1,1,20200101,20301231\n",
                    )

            route_cases = (
                ["R1,17,First,3\n", "R1,17,First,3\n"],
                ["R1,17,First,3\n", "R1,18,Second,3\n"],
                ["R1,18,Second,3\n", "R1,17,First,3\n"],
                [
                    "R1,17,First,3\n",
                    "R1,18,Second,3\n",
                    "R1,19,Third,3\n",
                ],
            )
            stop_cases = (
                ["Platform,Platform\n", "Platform,Platform\n"],
                ["Platform,Platform\n", "Platform,Changed\n"],
                ["Platform,Changed\n", "Platform,Platform\n"],
                [
                    "Platform,First\n",
                    "Platform,Second\n",
                    "Platform,Third\n",
                ],
            )
            trip_cases = (
                ["R1,S1,T1,First,0\n", "R1,S1,T1,First,0\n"],
                ["R1,S1,T1,First,0\n", "R2,S1,T1,Second,0\n"],
                ["R2,S1,T1,Second,0\n", "R1,S1,T1,First,0\n"],
                [
                    "R2,S1,T1,Second,0\n",
                    "R1,S1,T1,First,0\n",
                    "R2,S1,T1,Second,0\n",
                ],
            )

            def build_observable(path: Path, mode: str) -> dict[str, bytes]:
                output = root / f"duplicate-{path.stem}-{mode}"
                archive = load_gtfs_archive(str(path))
                context = (
                    None
                    if mode == "legacy"
                    else NormalizedProviderContext.from_archive(archive)
                )
                try:
                    cities = external_gtfs.load_external_cities(source, REPOSITORY_ROOT)
                    _manifest, _packages = external_gtfs.build_external_stop_packages(
                        archive, cities, output, context=context
                    )
                    external_gtfs.build_external_route_index(
                        archive, cities, output, context=context
                    )
                    external_gtfs.build_external_trip_index(
                        archive, cities, output, context=context, immutable_output=True
                    )
                    return {
                        relative: (output / relative).read_bytes()
                        for relative in (
                            "stops/vancouver.json",
                            "routes/vancouver.json",
                            "trip-index-base/vancouver.json",
                        )
                    }
                finally:
                    if context is not None:
                        context.close()
                    archive.close()

            for category, cases in (
                ("routes", route_cases),
                ("stops", stop_cases),
                ("trips", trip_cases),
            ):
                for index, values in enumerate(cases):
                    feed = root / f"duplicates-{category}-{index}.zip"
                    if category == "routes":
                        routes = values
                        stops = ["Platform,Platform\n"]
                        trips = ["R1,S1,T1,Terminal,0\n"]
                    elif category == "stops":
                        routes = ["R1,17,First,3\n"]
                        stops = values
                        trips = ["R1,S1,T1,Terminal,0\n"]
                    else:
                        routes = ["R1,17,First,3\n", "R2,18,Second,3\n"]
                        stops = ["Platform,Platform\n"]
                        trips = values
                    write_fixture(feed, routes, stops, trips)
                    self.assertEqual(
                        build_observable(feed, "legacy"),
                        build_observable(feed, "d1"),
                        f"{category} case {index}",
                    )

    def test_duplicate_downstream_failures_have_raw_and_context_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "duplicate-downstream.zip"
            _write_route_field_feed(
                feed,
                route_header="route_id,route_short_name,route_long_name,route_type",
                route_row="R1,17,First,3\nR1,18,Second,3",
                stop_lat=49.2827,
                stop_lon=-123.1207,
            )
            source = self._source_with_test_city(
                feed,
                provider_id="translink",
                city_id="vancouver",
            )
            cities = external_gtfs.load_external_cities(source, REPOSITORY_ROOT)
            outcomes: dict[str, tuple[str, str]] = {}
            for mode in ("legacy", "d1"):
                output = root / mode
                archive = load_gtfs_archive(str(feed))
                context = (
                    None
                    if mode == "legacy"
                    else NormalizedProviderContext.from_archive(archive)
                )
                try:
                    _manifest, packages = external_gtfs.build_external_stop_packages(
                        archive, cities, output, context=context
                    )
                    stage = ExternalDepartureStage()
                    try:
                        try:
                            stage.populate(
                                archive,
                                {"S1": ["20260101"]},
                                {"platform", "street"},
                                context=context,
                            )
                        except Exception as error:
                            outcomes[f"{mode}:terminal-stops"] = (
                                type(error).__name__,
                                str(error),
                            )
                        else:
                            outcomes[f"{mode}:terminal-stops"] = ("OK", "")
                    finally:
                        stage.close()
                    for name in ("lineMembership", "departures"):
                        try:
                            if name == "lineMembership":
                                external_gtfs.build_external_lines(
                                    archive, packages, context=context
                                )
                            else:
                                external_gtfs.build_external_departure_index(
                                    archive,
                                    cities,
                                    output,
                                    "America/Vancouver",
                                    context=context,
                                )
                        except Exception as error:
                            outcomes[f"{mode}:{name}"] = (
                                type(error).__name__,
                                str(error),
                            )
                        else:
                            outcomes[f"{mode}:{name}"] = ("OK", "")
                finally:
                    if context is not None:
                        context.close()
                    archive.close()
            self.assertEqual(
                outcomes["legacy:lineMembership"], outcomes["d1:lineMembership"]
            )
            self.assertEqual(outcomes["legacy:departures"], outcomes["d1:departures"])
            self.assertEqual(
                outcomes["legacy:terminal-stops"], outcomes["d1:terminal-stops"]
            )

            provider_city_outcomes: dict[str, tuple[str, str]] = {}
            for mode in ("legacy", "d1"):
                sources_path = root / f"sources-{mode}.json"
                sources_path.write_text(json.dumps([source]), encoding="utf-8")
                try:
                    process_external_gtfs_sources(
                        repository_root=REPOSITORY_ROOT,
                        sources_path=sources_path,
                        url_by_provider={},
                        output=root / f"provider-city-{mode}",
                        load_gtfs_archive=load_gtfs_archive,
                        environ={
                            "HALTEWECKER_EXTERNAL_BUILD_CACHE": "0",
                            "HALTEWECKER_EXTERNAL_TRANSFORMED_BUILD_CACHE": "0",
                        },
                        gtfs_cache=GTFSArtifactCache(root / "provider-city-cache"),
                        use_normalized_context=mode == "d1",
                    )
                except Exception as error:
                    provider_city_outcomes[mode] = (
                        type(error).__name__,
                        str(error),
                    )
                else:
                    provider_city_outcomes[mode] = ("OK", "")
            self.assertEqual(
                provider_city_outcomes["legacy"], provider_city_outcomes["d1"]
            )

    def test_legacy_and_d1_full_outputs_are_semantically_equal(self) -> None:
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
            legacy_output, _ = self._run(
                root,
                feed,
                source,
                gate=False,
                output_name="legacy",
                use_normalized_context=False,
            )
            d1_output, _ = self._run(
                root,
                feed,
                source,
                gate=False,
                output_name="d1",
                use_normalized_context=True,
            )

            for relative_path in (
                "stops/vancouver.json",
                "routes/vancouver.json",
                "trips/vancouver.json",
            ):
                self.assertEqual(
                    (legacy_output / relative_path).read_bytes(),
                    (d1_output / relative_path).read_bytes(),
                    relative_path,
                )

            legacy_departures = json.loads(
                (legacy_output / "departures/vancouver.json").read_text()
            )
            d1_departures = json.loads(
                (d1_output / "departures/vancouver.json").read_text()
            )
            legacy_departures.pop("generatedAt", None)
            d1_departures.pop("generatedAt", None)
            self.assertEqual(legacy_departures, d1_departures)

            def build_lines(use_context: bool):
                archive = load_gtfs_archive(str(feed))
                context = (
                    NormalizedProviderContext.from_archive(archive)
                    if use_context
                    else None
                )
                try:
                    package_stops = {
                        "vancouver": json.loads(
                            (legacy_output / "stops/vancouver.json").read_text()
                        )
                    }
                    return external_gtfs.build_external_lines(
                        archive, package_stops, context=context
                    )
                finally:
                    if context is not None:
                        context.close()
                    archive.close()

            self.assertEqual(build_lines(False), build_lines(True))

            def terminal_stops(use_context: bool):
                archive = load_gtfs_archive(str(feed))
                context = (
                    NormalizedProviderContext.from_archive(archive)
                    if use_context
                    else None
                )
                stage = ExternalDepartureStage()
                try:
                    stage.populate(
                        archive,
                        {"S1": ["20260101"]},
                        {"platform", "street"},
                        context=context,
                    )
                    return list(
                        stage.connection.execute(
                            "SELECT trip_id, sequence, stop_id "
                            "FROM terminal_stops ORDER BY trip_id"
                        )
                    )
                finally:
                    stage.close()
                    if context is not None:
                        context.close()
                    archive.close()

            self.assertEqual(terminal_stops(False), terminal_stops(True))

            def process_result(output_name: str, use_context: bool):
                sources_path = root / f"sources-{output_name}.json"
                sources_path.write_text(json.dumps([source]), encoding="utf-8")
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    result = process_external_gtfs_sources(
                        repository_root=REPOSITORY_ROOT,
                        sources_path=sources_path,
                        url_by_provider={},
                        output=root / output_name,
                        load_gtfs_archive=load_gtfs_archive,
                        environ={
                            "HALTEWECKER_EXTERNAL_BUILD_CACHE": "0",
                            "HALTEWECKER_EXTERNAL_TRANSFORMED_BUILD_CACHE": "0",
                        },
                        gtfs_cache=GTFSArtifactCache(root / "provider-cache"),
                        use_normalized_context=use_context,
                    )
                return result

            legacy_result = process_result("provider-city-legacy", False)
            d1_result = process_result("provider-city-d1", True)
            self.assertEqual(legacy_result, d1_result)

    def test_independent_legacy_builders_match_d1_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "independent-legacy.zip"
            source = self._source_with_test_city(
                feed,
                provider_id="translink",
                city_id="vancouver",
            )
            latitude, longitude = self._city_coordinates(source, "vancouver")
            _write_feed(feed, stop_lat=latitude, stop_lon=longitude)
            cities = external_gtfs.load_external_cities(source, REPOSITORY_ROOT)

            def build(mode: str) -> tuple[dict[str, bytes], dict[str, object]]:
                output = root / mode
                archive = load_gtfs_archive(str(feed))
                context = (
                    None
                    if mode == "legacy"
                    else NormalizedProviderContext.from_archive(archive)
                )
                try:
                    _manifest, packages = external_gtfs.build_external_stop_packages(
                        archive, cities, output, context=context
                    )
                    external_gtfs.build_external_route_index(
                        archive, cities, output, context=context
                    )
                    if mode == "legacy":
                        external_gtfs._legacy_build_external_departure_index(
                            archive,
                            cities,
                            output,
                            "America/Vancouver",
                        )
                        external_gtfs._legacy_build_external_trip_index(
                            archive, cities, output
                        )
                        lines = external_gtfs._legacy_build_external_lines(
                            archive, packages
                        )
                    else:
                        external_gtfs.build_external_departure_index(
                            archive,
                            cities,
                            output,
                            "America/Vancouver",
                            context=context,
                        )
                        external_gtfs.build_external_trip_index(
                            archive,
                            cities,
                            output,
                            context=context,
                            immutable_output=True,
                        )
                        external_gtfs.apply_current_departure_headsign_enrichment(
                            output, cities
                        )
                        lines = external_gtfs.build_external_lines(
                            archive, packages, context=context
                        )
                    artifacts = {
                        relative: (output / relative).read_bytes()
                        for relative in (
                            "stops/vancouver.json",
                            "routes/vancouver.json",
                            "trips/vancouver.json",
                        )
                    }
                    departures = json.loads(
                        (output / "departures/vancouver.json").read_text()
                    )
                    departures.pop("generatedAt", None)
                    return artifacts | {"departures": departures}, lines
                finally:
                    if context is not None:
                        context.close()
                    archive.close()

            legacy_artifacts, legacy_lines = build("legacy")
            d1_artifacts, d1_lines = build("d1")
            self.assertEqual(legacy_artifacts, d1_artifacts)
            self.assertEqual(legacy_lines, d1_lines)

    def test_changed_supplemental_manifest_fingerprint_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            self._build_once(root, feed)
            manifest_path = self._cache_directory(root) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["supplementalInputsFingerprint"] = "b" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                _output, logs = self._run(
                    root, feed, self._source(feed), output_name="supplemental-mismatch"
                )

            self.assertEqual(stops.call_count, 1)
            self.assertIn(
                "stage=build-cache status=INVALID reason=supplemental input fingerprint mismatch",
                logs,
            )

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

    def test_missing_semantic_fingerprint_is_invalid_then_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "cta.zip"
            _write_feed(feed)
            self._build_once(root, feed)
            manifest_path = self._cache_directory(root) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            del manifest["projectionFingerprint"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch(
                "external_gtfs.build_external_stop_packages",
                wraps=external_gtfs.build_external_stop_packages,
            ) as stops:
                _output, logs = self._run(
                    root,
                    feed,
                    self._source(feed),
                    output_name="missing-fingerprint",
                )
            self.assertEqual(stops.call_count, 1)
            self.assertIn(
                "stage=build-cache status=INVALID reason=projection fingerprint mismatch",
                logs,
            )

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

    def test_trip_index_headsign_enrichment_is_not_reused_from_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "trip-headsign-window.zip"
            source = self._source_with_test_city(
                feed,
                provider_id="translink",
                city_id="vancouver",
            )
            latitude, longitude = self._city_coordinates(source, "vancouver")
            today = datetime.now(ZoneInfo("America/Vancouver")).date()
            first_date = today.strftime("%Y%m%d")
            second_date = (today + timedelta(days=4)).strftime("%Y%m%d")
            _write_headsign_window_feed(
                feed,
                first_date=first_date,
                second_date=second_date,
                stop_lat=latitude,
                stop_lon=longitude,
            )
            first_output, _ = self._run(
                root,
                feed,
                source,
                allowlist="translink",
                transformed_gate=True,
                output_name="headsign-first",
            )

            original_datetime = external_gtfs.datetime

            class LaterDateTime(original_datetime):
                @classmethod
                def now(cls, tz=None):
                    current = original_datetime.now(tz)
                    return current + timedelta(days=4)

            with mock.patch("external_gtfs.datetime", LaterDateTime):
                second_output, logs = self._run(
                    root,
                    feed,
                    source,
                    allowlist="translink",
                    transformed_gate=True,
                    output_name="headsign-second",
                )

            self.assertIn("source=translink stage=build-cache status=HIT", logs)
            first_trip_index = json.loads(
                (first_output / "trips/vancouver.json").read_text()
            )
            first_base = json.loads(
                (first_output / "trip-index-base/vancouver.json").read_text()
            )
            second_trip_index = json.loads(
                (second_output / "trips/vancouver.json").read_text()
            )
            self.assertEqual(first_base, {"T1": {"r": "R1"}, "T2": {"r": "R1"}})
            self.assertEqual(first_trip_index["T1"]["h"], "First destination")
            self.assertNotIn("h", first_trip_index["T2"])
            self.assertNotIn("h", second_trip_index["T1"])
            self.assertEqual(second_trip_index["T2"]["h"], "Second destination")
            cached_base = json.loads(
                (
                    self._cache_directory_for(root, "translink")
                    / "trip-index-base/vancouver.json"
                ).read_text()
            )
            self.assertEqual(cached_base, {"T1": {"r": "R1"}, "T2": {"r": "R1"}})

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

    def test_reviewed_transformed_provider_policy_covers_only_explicit_sources(
        self,
    ) -> None:
        reviewed = {
            "finland-hsl",
            "poland-warsaw",
            "wmata-bus",
            "mbta-boston",
            "sweden",
            "mta-ny-nyct-bus",
            "ttc-surface",
            "australia-transport-nsw",
        }
        with tempfile.TemporaryDirectory() as temporary:
            feed = Path(temporary) / "fixture.zip"
            _write_feed(feed)
            for provider_id in reviewed:
                source = self._source(feed, provider_id=provider_id)
                cities = external_gtfs.load_external_cities(
                    source,
                    REPOSITORY_ROOT,
                )
                eligible, reason, _city_id = external_gtfs._external_cache_is_eligible(
                    source,
                    cities,
                )
                self.assertTrue(eligible, (provider_id, reason))

            unsupported = self._source(feed, provider_id="ireland")
            cities = external_gtfs.load_external_cities(unsupported, REPOSITORY_ROOT)
            eligible, reason, _city_id = external_gtfs._external_cache_is_eligible(
                unsupported,
                cities,
            )
            self.assertFalse(eligible)
            self.assertEqual(reason, "provider-not-in-reviewed-transform-class")

    def test_transformed_feature_gate_is_off_by_default_for_heavy_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "translink.zip"
            source = self._source_with_test_city(
                feed,
                provider_id="ttc-surface",
                city_id="toronto",
            )
            source["namespace"] = "fixture:"
            source["mergeGroup"] = "fixture-shared"
            latitude, longitude = self._city_coordinates(source, "toronto")
            active_date = (
                datetime.now(ZoneInfo("America/Toronto")).date().strftime("%Y%m%d")
            )
            _write_feed(
                feed,
                active_date=active_date,
                stop_lat=latitude,
                stop_lon=longitude,
            )
            _output, logs = self._run(
                root,
                feed,
                source,
                allowlist="ttc-surface",
                output_name="transformed-gate-off",
            )
            self.assertIn(
                "stage=build-cache status=DISABLED reason=transformed-feature-gate-off",
                logs,
            )
            self.assertFalse((root / "gtfs-cache" / "external-build").exists())

    def test_empty_trip_index_is_a_valid_cache_artifact_and_hits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "translink-empty-trips.zip"
            source = self._source_with_test_city(
                feed,
                provider_id="translink",
                city_id="vancouver",
            )
            latitude, longitude = self._city_coordinates(source, "vancouver")
            _write_feed(
                feed,
                stop_lat=latitude,
                stop_lon=longitude,
                empty_trips=True,
            )
            first_output, _ = self._run(
                root,
                feed,
                source,
                allowlist="translink",
                transformed_gate=True,
                output_name="empty-trips-first",
            )
            with mock.patch(
                "external_gtfs.build_external_trip_index",
                wraps=external_gtfs.build_external_trip_index,
            ) as trips:
                second_output, logs = self._run(
                    root,
                    feed,
                    source,
                    allowlist="translink",
                    transformed_gate=True,
                    output_name="empty-trips-second",
                )
            self.assertEqual(
                json.loads((first_output / "trips/vancouver.json").read_text()), {}
            )
            self.assertEqual(trips.call_count, 0)
            self.assertIn("stage=build-cache status=HIT", logs)
            self.assertEqual(
                (first_output / "trips/vancouver.json").read_bytes(),
                (second_output / "trips/vancouver.json").read_bytes(),
            )

    def test_missing_raw_fingerprint_is_cache_ineligible(self) -> None:
        with self.assertRaises(CacheKeyUnavailable):
            external_gtfs.cache_key(
                repository_root=REPOSITORY_ROOT,
                provider_id="cta-chicago",
                raw_sha256="",
                source=self._source(Path("fixture.zip")),
                city_id="chicago",
            )

    def test_city_config_fingerprint_change_is_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            (temporary_root / "scripts").mkdir()
            for relative_script in (
                "external_gtfs.py",
                "external_staging.py",
                "gtfs_csv.py",
                "build_stop_packages.py",
                "external_build_cache.py",
            ):
                shutil.copyfile(
                    REPOSITORY_ROOT / "scripts" / relative_script,
                    temporary_root / "scripts" / relative_script,
                )
            relative = Path("cities.json")
            city_path = temporary_root / relative
            city_path.write_text('[{"id":"chicago"}]', encoding="utf-8")
            source = {
                "id": "cta-chicago",
                "cities": relative.as_posix(),
                "buildStops": True,
                "buildRoutes": True,
            }
            first = external_gtfs.cache_key(
                repository_root=temporary_root,
                provider_id="cta-chicago",
                raw_sha256="a" * 64,
                source=source,
                city_id="chicago",
            )
            city_path.write_text('[{"id":"chicago","v":1}]', encoding="utf-8")
            second = external_gtfs.cache_key(
                repository_root=temporary_root,
                provider_id="cta-chicago",
                raw_sha256="a" * 64,
                source=source,
                city_id="chicago",
            )
            self.assertNotEqual(
                first.city_config_fingerprint, second.city_config_fingerprint
            )
            self.assertNotEqual(first.value, second.value)

    def test_multi_city_cache_restores_each_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            for city_id in ("alpha", "beta"):
                (output / "stops").mkdir(parents=True, exist_ok=True)
                (output / "routes").mkdir(parents=True, exist_ok=True)
                (output / "trip-index-base").mkdir(parents=True, exist_ok=True)
                (output / f"stops/{city_id}.json").write_text(
                    json.dumps([{"id": city_id}]), encoding="utf-8"
                )
                (output / f"routes/{city_id}.json").write_text(
                    json.dumps({f"route-{city_id}": {}}), encoding="utf-8"
                )
                (output / f"trip-index-base/{city_id}.json").write_text(
                    "{}", encoding="utf-8"
                )
            cache = ExternalBuildCache(
                root / "cache",
                provider_id="fixture-provider",
                city_id="alpha",
                city_ids=("alpha", "beta"),
            )
            key = CacheKey(
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                provider_id="fixture-provider",
                city_id="alpha",
                projection_fingerprint=projection_fingerprint({}, ("alpha", "beta")),
                city_ids=("alpha", "beta"),
            )
            cache.persist(key, output, {"alpha": {}, "beta": {}})
            lookup = cache.lookup(key)
            self.assertEqual(lookup.status, "HIT")
            restored = cache.restore(lookup, root / "restored")
            self.assertEqual(
                set(restored.package_stops_by_city_id or {}), {"alpha", "beta"}
            )
            self.assertEqual(
                restored.package_stops_by_city_id,
                {"alpha": [{"id": "alpha"}], "beta": [{"id": "beta"}]},
            )

    def test_projection_fingerprint_covers_each_reviewed_transform(self) -> None:
        base = {
            "namespace": "base:",
            "mergeGroup": "base",
            "filterCitiesByProvider": False,
            "exclusiveCityPartition": False,
            "agencyID": None,
            "stopIDMode": "exact",
            "publishPassengerStopIDs": False,
        }
        city_ids = ("alpha",)
        for field, changed in (
            ("namespace", "changed:"),
            ("mergeGroup", "changed-merge"),
            ("filterCitiesByProvider", True),
            ("exclusiveCityPartition", True),
            ("agencyID", "1"),
            ("stopIDMode", "unsupported"),
            ("publishPassengerStopIDs", True),
        ):
            candidate = dict(base)
            candidate[field] = changed
            self.assertNotEqual(
                projection_fingerprint(base, city_ids),
                projection_fingerprint(candidate, city_ids),
                field,
            )
        self.assertNotEqual(
            projection_fingerprint(base, city_ids),
            projection_fingerprint(base, ("alpha", "beta")),
        )

    def test_concurrent_writers_publish_one_complete_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            (output / "stops").mkdir(parents=True)
            (output / "routes").mkdir(parents=True)
            (output / "stops/chicago.json").write_text("[]", encoding="utf-8")
            (output / "routes/chicago.json").write_text("{}", encoding="utf-8")
            cache = ExternalBuildCache(
                root / "cache",
                provider_id="cta-chicago",
                city_id="chicago",
                include_trip_index=False,
            )
            key = CacheKey(
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                provider_id="cta-chicago",
                city_id="chicago",
                projection_fingerprint="f" * 64,
                city_ids=("chicago",),
            )
            errors: list[Exception] = []

            def persist() -> None:
                try:
                    cache.persist(key, output, {})
                except (OSError, TypeError, ValueError) as error:
                    errors.append(error)

            writers = [threading.Thread(target=persist) for _ in range(2)]
            for writer in writers:
                writer.start()
            for writer in writers:
                writer.join()
            self.assertEqual(errors, [])
            self.assertEqual(cache.lookup(key).status, "HIT")
            self.assertEqual(list(cache.root.glob(".*.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
