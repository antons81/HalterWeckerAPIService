import json
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile
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
from prepare_gtfs_artifacts import artifact_payload  # noqa: E402
from release_integrity import (  # noqa: E402
    validate_artifact_entry,
    validate_candidate_sources,
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
            directory_payload = artifact_payload(
                ArtifactResult("ireland", directory, "local")
            )
            self.assertTrue(directory_payload["sha256"])
            self.assertGreater(directory_payload["size"], 0)
            self.assertEqual(
                artifact_provenance(directory),
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
        with self.assertRaisesRegex(ValueError, "lost cities"):
            validate_previous_release_cities({"fixture-city"}, set())
        validate_previous_release_cities({"fixture-city"}, set(), allowlisted={"fixture-city"})
