import json
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from artifact_provenance import (  # noqa: E402
    artifact_provenance,
    canonical_content_provenance,
    immutable_file_path,
)
from external_gtfs import (  # noqa: E402
    configured_external_url,
    load_external_cities,
    load_external_gtfs_sources,
    process_external_gtfs_sources,
    source_classification,
    validate_external_gtfs_source,
    validate_kyiv_gtfs_archive,
)
from build_stop_packages import load_gtfs_archive  # noqa: E402
from gtfs_source_cache import ArtifactResult, GTFSArtifactCache  # noqa: E402
from prepare_custom_gtfs_artifacts import resolve_source  # noqa: E402
from prepare_gtfs_artifacts import (  # noqa: E402
    artifact_payload,
    ensure_current_source_calendar,
    ensure_source_calendar_for_candidate,
    gtfs_calendar_horizon,
    merge_gtfs_feed_bundle,
)
from release_integrity import (  # noqa: E402
    validate_artifact_entry,
    validate_candidate_sources,
    validate_candidate_source_freshness,
    validate_previous_release_cities,
    validate_previous_release_sources,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _write_kyiv_gtfs(path: Path) -> bytes:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,location_type\n"
            "kyiv-stop,Kyiv Main Stop,50.4501,30.5234,0\n",
        )
        archive.writestr(
            "routes.txt",
            "route_id,route_short_name,route_long_name,route_type\n"
            "R1,1,Kyiv Route,3\n",
        )
        archive.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,trip_headsign,direction_id\n"
            "R1,S1,T1,Kyiv Terminal,0\n",
        )
        archive.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,08:00:00,08:00:00,kyiv-stop,1\n",
        )
        archive.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "S1,1,1,1,1,1,1,20200101,20301231\n",
        )
    return path.read_bytes()


def _write_bundle_feed(path: Path, suffix: str, *, service_end: date, route_id: str | None = None) -> Path:
    route_id = route_id or f"route-{suffix}"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("agency.txt", "agency_id,agency_name\nMTA NYCT,New York City Transit\n")
        archive.writestr(
            "routes.txt",
            "route_id,agency_id,route_short_name,route_type\n"
            f"{route_id},MTA NYCT,{suffix},3\n",
        )
        archive.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n"
                         f"stop-{suffix},Stop {suffix},40.0,-73.0\n")
        archive.writestr("trips.txt", "route_id,service_id,trip_id\n"
                         f"{route_id},service-{suffix},trip-{suffix}\n")
        archive.writestr("stop_times.txt", "trip_id,stop_id,stop_sequence,departure_time\n"
                         f"trip-{suffix},stop-{suffix},1,08:00:00\n")
        archive.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            f"service-{suffix},1,1,1,1,1,1,1,20260101,{service_end:%Y%m%d}\n",
        )
    return path


class _ArchiveResponse:
    status = 200
    headers = {"Content-Length": "0"}

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body)
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk


def _kyiv_source_fixture(root: Path) -> tuple[Path, dict[str, object]]:
    source = next(
        item
        for item in load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        if item["id"] == "kyiv"
    )
    source["buildRadarTopology"] = False
    source_path = root / "sources.json"
    source_path.write_text(json.dumps([source]), encoding="utf-8")
    return source_path, source


def _kyiv_catalog_loader(_configuration: dict[str, object]) -> object:
    return [{
        "uid": "catalog-stop",
        "label": "Kyiv Catalog Stop",
        "lat": 50.4510,
        "lon": 30.5240,
        "type": 0,
        "code": "catalog-stop",
    }]


class ExternalGTFSProvenanceTests(unittest.TestCase):
    def test_kyiv_fresh_build_writes_validator_complete_input_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "kyiv.zip"
            archive_bytes = _write_kyiv_gtfs(archive_path)
            source_path, source = _kyiv_source_fixture(root)
            cache = GTFSArtifactCache(root / "gtfs-cache")

            def fresh_download(request, timeout=0):
                return _ArchiveResponse(archive_bytes)

            with mock.patch(
                "gtfs_source_cache.urllib.request.urlopen",
                side_effect=fresh_download,
            ):
                manifest, cities, packages, lines = process_external_gtfs_sources(
                    repository_root=REPOSITORY_ROOT,
                    sources_path=source_path,
                    url_by_provider={"kyiv": source["url"]},
                    output=root / "output",
                    load_gtfs_archive=load_gtfs_archive,
                    load_stop_catalog=_kyiv_catalog_loader,
                    gtfs_cache=cache,
                )

            self.assertEqual([city["id"] for city in cities], ["kyiv"])
            self.assertEqual([entry["id"] for entry in manifest], ["kyiv"])
            self.assertIn("kyiv", packages)
            self.assertIsInstance(lines, dict)
            provenance = json.loads(
                (root / "output" / "provenance" / "input-artifacts.json")
                .read_text(encoding="utf-8")
            )
            entry = provenance["sources"]["kyiv"]
            digest, size, path = validate_artifact_entry("kyiv", entry)
            self.assertEqual(path, Path(entry["path"]))
            self.assertEqual(entry["status"], "updated")
            self.assertEqual((digest, size), artifact_provenance(path))
            self.assertIn("kyiv:supplemental-stop-catalog", provenance["sources"])
            supplemental = provenance["sources"]["kyiv:supplemental-stop-catalog"]
            self.assertIsInstance(supplemental["sha256"], str)
            self.assertGreater(supplemental["size"], 0)

    def test_kyiv_stale_build_writes_validator_complete_input_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "kyiv.zip"
            _write_kyiv_gtfs(archive_path)
            source_path, source = _kyiv_source_fixture(root)
            cache = GTFSArtifactCache(root / "gtfs-cache")
            cache.resolve(
                "kyiv",
                str(archive_path),
                validator=validate_kyiv_gtfs_archive,
            )
            calls = []

            def unavailable(request, timeout=0):
                calls.append(request.full_url)
                raise urllib.error.URLError(TimeoutError("simulated outage"))

            with mock.patch(
                "gtfs_source_cache.urllib.request.urlopen",
                side_effect=unavailable,
            ), mock.patch("gtfs_source_cache.time.sleep"):
                manifest, cities, packages, _lines = process_external_gtfs_sources(
                    repository_root=REPOSITORY_ROOT,
                    sources_path=source_path,
                    url_by_provider={"kyiv": source["url"]},
                    output=root / "output",
                    load_gtfs_archive=load_gtfs_archive,
                    load_stop_catalog=_kyiv_catalog_loader,
                    gtfs_cache=cache,
                )

            self.assertEqual(len(calls), 3)
            self.assertEqual([city["id"] for city in cities], ["kyiv"])
            self.assertEqual([entry["id"] for entry in manifest], ["kyiv"])
            self.assertIn("kyiv", packages)
            provenance = json.loads(
                (root / "output" / "provenance" / "input-artifacts.json")
                .read_text(encoding="utf-8")
            )
            entry = provenance["sources"]["kyiv"]
            digest, size, path = validate_artifact_entry("kyiv", entry)
            self.assertEqual(entry["status"], "preserved-stale")
            self.assertEqual((digest, size), artifact_provenance(path))
            self.assertEqual(path, root / "gtfs-cache" / "kyiv" / "current.zip")
            self.assertIn("kyiv:supplemental-stop-catalog", provenance["sources"])

    def test_scoped_url_resolution_and_precedence(self) -> None:
        self.assertEqual(
            configured_external_url({"url": "https://url", "scopedURL": "https://scoped"}),
            "https://url",
        )
        self.assertEqual(
            configured_external_url({"scopedURL": "https://scoped"}),
            "https://scoped",
        )

    def test_511_registry_is_required_and_has_four_city_inputs(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        source = next(source for source in sources if source["id"] == "511-bay-area")
        validate_external_gtfs_source(source, REPOSITORY_ROOT)
        self.assertEqual(source_classification(source), "required")
        self.assertEqual(
            configured_external_url(source),
            "https://api.511.org/transit/datafeeds?operator_id=RG",
        )
        self.assertEqual(
            {city["id"] for city in load_external_cities(source, REPOSITORY_ROOT)},
            {"san-francisco", "oakland", "berkeley", "san-jose"},
        )

    def test_full_registry_has_unique_resolvable_provider_inventory(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        source_ids = [str(source["id"]) for source in sources]
        self.assertEqual(len(source_ids), len(set(source_ids)))
        self.assertIn("511-bay-area", source_ids)
        for source in sources:
            validate_external_gtfs_source(source, REPOSITORY_ROOT)
            classification = source_classification(source)
            self.assertIn(classification, {"required", "optional", "conditional"})
            self.assertIsInstance(source.get("importIntoStaticDepartures", False), bool)
            has_input = bool(
                configured_external_url(source)
                or str(source.get("localPath", "")).strip()
                or source.get("feedBundle")
            )
            if classification == "required":
                self.assertTrue(has_input, source["id"])
            for city in load_external_cities(source, REPOSITORY_ROOT):
                providers = city.get("externalGTFSProviders")
                if providers is None:
                    self.assertEqual(city.get("externalGTFSProvider"), source["id"])
                else:
                    self.assertIn(source["id"], providers)

    def test_required_missing_input_fails_and_optional_missing_input_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "cities.json").write_text(
                json.dumps([
                    {
                        "id": "fixture-city",
                        "name": "Fixture City",
                        "packageMode": "external",
                        "externalGTFSProvider": "fixture",
                    }
                ]),
                encoding="utf-8",
            )

            def write_source(classification: str) -> Path:
                path = root / f"{classification}.json"
                path.write_text(
                    json.dumps([{
                        "id": "fixture",
                        "classification": classification,
                        "cities": "config/cities.json",
                        "timezone": "UTC",
                        "country": "US",
                        "identifierPrefix": "fixture:",
                    }]),
                    encoding="utf-8",
                )
                return path

            def no_archive(*args, **kwargs):
                raise AssertionError("missing-input source must not load an archive")

            with self.assertRaisesRegex(ValueError, "cannot be skipped"):
                process_external_gtfs_sources(
                    repository_root=root,
                    sources_path=write_source("required"),
                    url_by_provider={},
                    output=root / "required-output",
                    load_gtfs_archive=no_archive,
                )

            manifest, cities, stops, lines = process_external_gtfs_sources(
                repository_root=root,
                sources_path=write_source("optional"),
                url_by_provider={},
                output=root / "optional-output",
                load_gtfs_archive=no_archive,
            )
            self.assertEqual((manifest, cities, stops, lines), ([], [], {}, {}))
            provenance = json.loads(
                (root / "optional-output" / "provenance" / "input-artifacts.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["skipped"]["fixture"]["status"], "skipped")

    def test_required_local_path_is_used_by_runtime_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "cities.json").write_text(
                json.dumps([{
                    "id": "fixture-city",
                    "name": "Fixture City",
                    "packageMode": "external",
                    "externalGTFSProvider": "fixture",
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "radiusMeters": 1000,
                }]),
                encoding="utf-8",
            )
            local_path = root / "fixture-gtfs"
            local_path.mkdir()
            (local_path / "stops.txt").write_text("fixture", encoding="utf-8")
            source_path = root / "sources.json"
            source_path.write_text(
                json.dumps([{
                    "id": "fixture",
                    "classification": "required",
                    "localPath": str(local_path),
                    "cities": "config/cities.json",
                    "timezone": "UTC",
                    "country": "US",
                    "identifierPrefix": "fixture:",
                }]),
                encoding="utf-8",
            )

            def archive_called(*args, **kwargs):
                raise RuntimeError("local archive loader called")

            with self.assertRaisesRegex(RuntimeError, "local archive loader called"):
                process_external_gtfs_sources(
                    repository_root=root,
                    sources_path=source_path,
                    url_by_provider={},
                    output=root / "output",
                    load_gtfs_archive=archive_called,
                )

    def test_local_file_and_directory_use_common_provenance_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file_path = root / "subway.zip"
            file_path.write_bytes(b"local-gtfs-fixture")
            file_payload = artifact_payload(
                ArtifactResult("mta-ny-subway", file_path, "local")
            )
            self.assertTrue(file_payload["sha256"])
            self.assertEqual(file_payload["size"], len(b"local-gtfs-fixture"))
            self.assertEqual(
                artifact_provenance(Path(file_payload["path"])),
                (file_payload["sha256"], file_payload["size"]),
            )

            directory = root / "ireland"
            directory.mkdir()
            (directory / "stops.txt").write_text("stop_id,stop_name\nS,Stop\n", encoding="utf-8")
            (root / "release").mkdir()
            directory_payload = artifact_payload(
                ArtifactResult("ireland", directory, "local"),
                release_root=root / "release",
            )
            self.assertTrue(directory_payload["sha256"])
            self.assertGreater(directory_payload["size"], 0)
            self.assertEqual(
                artifact_provenance(Path(directory_payload["path"])),
                (directory_payload["sha256"], directory_payload["size"]),
            )

    def test_custom_artifact_resolution_returns_verified_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vbb.zip"
            path.write_bytes(b"vbb-fixture")
            digest, size = artifact_provenance(path)

            class FakeCache:
                def resolve(self, source_id, url, **kwargs):
                    return ArtifactResult(
                        source_id,
                        path,
                        "updated",
                        "fixture",
                        {"sha256": digest, "size": size},
                    )

            result = resolve_source(FakeCache(), "vbb", "https://example.invalid/vbb.zip")
            self.assertEqual(result["sha256"], digest)
            self.assertEqual(result["size"], size)
            self.assertEqual(artifact_provenance(Path(result["path"])), (digest, size))

    def test_content_provenance_is_deterministic_and_identity_bound(self) -> None:
        first = canonical_content_provenance({"b": 2, "a": 1}, identity="fixture-v1")
        second = canonical_content_provenance({"a": 1, "b": 2}, identity="fixture-v1")
        third = canonical_content_provenance({"a": 1, "b": 2}, identity="fixture-v2")
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_immutable_file_path_survives_atomic_cache_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "current.zip"
            current.write_bytes(b"old")
            digest, size = artifact_provenance(current)
            immutable = immutable_file_path(current, digest)
            replacement = root / "replacement.zip"
            replacement.write_bytes(b"new")
            os.replace(replacement, current)
            self.assertEqual(artifact_provenance(immutable), (digest, size))

    def test_artifact_path_digest_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.zip"
            path.write_bytes(b"old")
            digest, size = artifact_provenance(path)
            path.write_bytes(b"new")
            with self.assertRaisesRegex(ValueError, "checksum/path mismatch"):
                validate_artifact_entry(
                    "fixture",
                    {"path": str(path), "sha256": digest, "size": size},
                )

    def test_candidate_and_previous_release_guards(self) -> None:
        registry = [{
            "id": "fixture",
            "classification": "required",
            "cities": "config/cities.json",
        }]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "cities.json").write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing from candidate"):
                validate_candidate_sources(registry, {}, set(), root, environ={})
        with self.assertRaisesRegex(ValueError, "lost sources"):
            validate_previous_release_sources({"fixture"}, set())
        validate_previous_release_sources({"fixture"}, set(), allowlisted={"fixture"})
        self.assertEqual(
            validate_previous_release_cities({"fixture-city"}, set()),
            ["fixture-city"],
        )
        self.assertEqual(
            validate_previous_release_cities({"retained-city"}, {"retained-city"}),
            [],
        )

    def test_expired_source_calendar_forces_unconditional_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            today = date.today()
            expired = _write_bundle_feed(root / "expired.zip", "old", service_end=today - timedelta(days=1))
            fresh = _write_bundle_feed(root / "fresh.zip", "new", service_end=today + timedelta(days=21))
            initial = ArtifactResult("fixture", expired, "unchanged", "remote metadata")
            refreshed = ArtifactResult("fixture", fresh, "updated", "downloaded")
            cache = GTFSArtifactCache(root / "cache")
            with mock.patch("prepare_gtfs_artifacts.resolve_one", return_value=refreshed) as request:
                result = ensure_current_source_calendar(
                    cache, initial, source_id="fixture", url="https://gtfs.example/feed.zip",
                    timezone_name="UTC", allow_stale=True,
                )
            self.assertEqual(result.path, fresh)
            self.assertIs(request.call_args.kwargs["force_download"], True)
            self.assertIs(request.call_args.kwargs["allow_stale"], False)

    def test_expired_source_with_failed_refresh_reports_exact_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            today = date.today()
            expired = _write_bundle_feed(root / "expired.zip", "old", service_end=today - timedelta(days=1))
            initial = ArtifactResult("fixture", expired, "unchanged", "remote metadata")
            cache = GTFSArtifactCache(root / "cache")
            with mock.patch(
                "prepare_gtfs_artifacts.resolve_one",
                side_effect=urllib.error.HTTPError(
                    "https://gtfs.example/feed.zip", 503, "unavailable", {}, None,
                ),
            ) as request:
                with self.assertRaisesRegex(ValueError, "source=fixture calendar expired.*gtfs.example/feed.zip.*HTTPError"):
                    ensure_current_source_calendar(
                        cache, initial, source_id="fixture", url="https://gtfs.example/feed.zip",
                        timezone_name="UTC", allow_stale=True, retry_attempts=3,
                    )
            self.assertEqual(request.call_args.kwargs["retry_attempts"], 3)
            self.assertIs(request.call_args.kwargs["force_download"], True)
            self.assertIs(request.call_args.kwargs["allow_stale"], False)

    def test_expired_cached_source_is_preserved_for_assembly_but_marked_unactivatable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            today = date(2026, 10, 11)
            expired = _write_bundle_feed(
                root / "gdansk-expired.zip", "gdansk", service_end=date(2026, 10, 8)
            )
            initial = ArtifactResult("poland-gdansk", expired, "unchanged", "remote metadata")
            cache = GTFSArtifactCache(root / "cache")
            with (
                mock.patch("prepare_gtfs_artifacts.source_calendar_local_date", return_value=today),
                mock.patch(
                    "prepare_gtfs_artifacts.resolve_one",
                    side_effect=urllib.error.HTTPError(
                        "https://gtfs.example/feed.zip", 500, "upstream unavailable", {}, None,
                    ),
                ) as request,
            ):
                preserved, refresh_status = ensure_source_calendar_for_candidate(
                    cache,
                    initial,
                    source_id="poland-gdansk",
                    url="https://gtfs.example/feed.zip",
                    timezone_name="Europe/Warsaw",
                    allow_stale=True,
                    retry_attempts=3,
                )

            self.assertEqual(preserved.path, expired)
            self.assertEqual(refresh_status["status"], "failed")
            self.assertEqual(refresh_status["calendarStatus"], "expired")
            self.assertEqual(refresh_status["serviceThrough"], "2026-10-08")
            self.assertEqual(request.call_args.kwargs["retry_attempts"], 3)
            self.assertIs(request.call_args.kwargs["force_download"], True)

            payload = {
                "sources": {},
                "external": {
                    "poland-gdansk": artifact_payload(preserved)
                    | {"sourceRefresh": refresh_status}
                },
            }
            with self.assertRaisesRegex(
                ValueError,
                "Candidate source freshness blocks activation: poland-gdansk .*service through 2026-10-08",
            ):
                validate_candidate_source_freshness(payload)

    def test_prepare_artifacts_continues_after_expired_source_refresh_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expired = _write_bundle_feed(
                root / "gdansk-expired.zip", "gdansk", service_end=date(2026, 10, 8)
            )
            fresh = _write_bundle_feed(
                root / "fresh.zip", "fresh", service_end=date(2026, 11, 30)
            )
            sources_path = root / "external-sources.json"
            sources_path.write_text(
                json.dumps([
                    {
                        "id": "poland-gdansk",
                        "classification": "required",
                        "url": "https://gdansk.example/feed.zip",
                        "timezone": "Europe/Warsaw",
                        "buildDepartures": True,
                    },
                    {
                        "id": "fixture-after",
                        "classification": "required",
                        "url": "https://next.example/feed.zip",
                        "timezone": "UTC",
                        "buildDepartures": True,
                    },
                ]),
                encoding="utf-8",
            )
            output_path = root / "release" / "gtfs-artifacts.json"
            release_root = output_path.parent
            release_root.mkdir()

            def resolve(_cache, source_id, _url, **kwargs):
                if source_id == "poland-gdansk":
                    if kwargs.get("force_download"):
                        raise urllib.error.HTTPError(
                            "https://gdansk.example/feed.zip", 500, "upstream unavailable", {}, None,
                        )
                    return ArtifactResult(source_id, expired, "unchanged", "remote metadata")
                return ArtifactResult(source_id, fresh, "updated", "downloaded")

            argv = [
                "prepare_gtfs_artifacts.py",
                "--cache-root", str(root / "cache"),
                "--gtfs-url", "https://germany.example/feed.zip",
                "--swiss-gtfs-url", "https://swiss.example/feed.zip",
                "--external-sources", str(sources_path),
                "--output", str(output_path),
                "--release-root", str(release_root),
            ]
            with (
                mock.patch("sys.argv", argv),
                mock.patch("prepare_gtfs_artifacts.source_calendar_local_date", return_value=date(2026, 10, 11)),
                mock.patch("prepare_gtfs_artifacts.resolve_one", side_effect=resolve),
                mock.patch(
                    "prepare_gtfs_artifacts.authenticated_external_request",
                    side_effect=lambda _source_id, url, **_kwargs: (url, {}),
                ),
            ):
                from prepare_gtfs_artifacts import main
                main()

            artifacts = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                artifacts["external"]["poland-gdansk"]["sourceRefresh"]["status"],
                "failed",
            )
            self.assertEqual(artifacts["external"]["fixture-after"]["status"], "updated")
            self.assertTrue(Path(artifacts["external"]["poland-gdansk"]["path"]).is_file())
            validate_candidate_sources(
                json.loads(sources_path.read_text(encoding="utf-8")),
                artifacts["external"],
                set(),
                root,
                environ={},
            )
            with self.assertRaisesRegex(ValueError, "Candidate source freshness blocks activation"):
                validate_candidate_source_freshness(artifacts)

    def test_candidate_source_freshness_allows_unmarked_and_confirmed_current_sources(self) -> None:
        validate_candidate_source_freshness({
            "sources": {},
            "external": {
                "unchanged-current": {"path": "feed.zip"},
                "refreshed": {
                    "sourceRefresh": {
                        "status": "succeeded",
                        "calendarStatus": "current",
                    }
                },
            },
        })

    def test_mta_bus_bundle_merges_identical_rows_and_rejects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            today = date.today()
            first = _write_bundle_feed(root / "first.zip", "A", service_end=today + timedelta(days=30))
            second = _write_bundle_feed(root / "second.zip", "B", service_end=today + timedelta(days=30))
            merged = root / "merged.zip"
            merge_gtfs_feed_bundle([first, second], merged)
            with zipfile.ZipFile(merged) as archive:
                self.assertEqual(archive.read("agency.txt").decode().count("MTA NYCT"), 1)
                self.assertEqual(archive.read("routes.txt").decode().count("route-"), 2)
            conflict = _write_bundle_feed(
                root / "conflict.zip", "C", route_id="route-A", service_end=today + timedelta(days=30),
            )
            with self.assertRaisesRegex(ValueError, "conflicting duplicate key in routes.txt"):
                merge_gtfs_feed_bundle([first, conflict], root / "conflict-merged.zip")

    def test_mta_registry_uses_current_official_feeds_instead_of_local_archives(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        indexed = {source["id"]: source for source in sources}
        subway = indexed["mta-ny-subway"]
        mta_bus = indexed["mta-ny-mta-bus"]
        nyct_bus = indexed["mta-ny-nyct-bus"]
        self.assertEqual(subway["url"], "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip")
        self.assertEqual(mta_bus["url"], "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_busco.zip")
        self.assertNotIn("localPath", subway)
        self.assertNotIn("localPath", mta_bus)
        self.assertEqual(
            [row["id"] for row in nyct_bus["feedBundle"]],
            ["bronx", "brooklyn", "manhattan", "queens", "staten-island"],
        )
        self.assertNotIn("localPath", nyct_bus)
        for source in (subway, mta_bus, nyct_bus):
            validate_external_gtfs_source(source, REPOSITORY_ROOT)

    def test_ireland_registry_uses_refreshable_official_nta_gtfs(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        ireland = next(source for source in sources if source["id"] == "ireland")

        self.assertEqual(
            ireland["url"],
            "https://www.transportforireland.ie/transitData/Data/GTFS_All.zip",
        )
        self.assertNotIn("localPath", ireland)
        self.assertEqual(source_classification(ireland), "required")
        validate_external_gtfs_source(ireland, REPOSITORY_ROOT)
