import io
import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from gtfs_source_cache import GTFSArtifactCache, default_cache_root
from gtfs_source_cache import ArtifactResult
from external_gtfs import validate_kyiv_gtfs_archive
from download_austrian_gtfs import download_source


def write_gtfs(path: Path, marker: str = "1") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            archive.writestr(name, f"marker,{marker}\n")


def write_kyiv_gtfs(path: Path, marker: str = "1") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("stops.txt", f"stop_id,stop_name\nstop-{marker},Main\n")
        archive.writestr("routes.txt", f"route_id,route_name\nroute-{marker},Route\n")
        archive.writestr("trips.txt", f"route_id,trip_id\nroute-{marker},trip-{marker}\n")
        archive.writestr(
            "stop_times.txt",
            f"trip_id,stop_id,stop_sequence\ntrip-{marker},stop-{marker},1\n",
        )


class FakeResponse:
    def __init__(self, body: bytes = b"", status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, *_args):
        if not self.body:
            return b""
        size = _args[0] if _args else len(self.body)
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk


class GTFSCacheRootTests(unittest.TestCase):
    def test_default_cache_root_without_environment_uses_canonical_production_path(self) -> None:
        self.assertEqual(
            default_cache_root({}),
            Path("/srv/haltewecker/data/cache/gtfs"),
        )

    def test_default_cache_root_uses_data_root_when_override_is_absent(self) -> None:
        self.assertEqual(
            default_cache_root({"DATA_ROOT": "/srv/haltewecker/data"}),
            Path("/srv/haltewecker/data/cache/gtfs"),
        )

    def test_explicit_gtfs_cache_root_override_takes_priority(self) -> None:
        self.assertEqual(
            default_cache_root({
                "DATA_ROOT": "/srv/haltewecker/data",
                "GTFS_CACHE_ROOT": "/mnt/custom/gtfs",
            }),
            Path("/mnt/custom/gtfs"),
        )

    def test_cleanup_cli_without_cache_argument_uses_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp) / "data"
            source_directory = data_root / "cache" / "gtfs" / "default-source"
            source_directory.mkdir(parents=True)
            (source_directory / ".lock").touch()
            environment = os.environ.copy()
            environment.pop("GTFS_CACHE_ROOT", None)
            environment["DATA_ROOT"] = str(data_root)

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "gtfs_source_cache.py"),
                    "cleanup",
                    "--dry-run",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("source=default-source status=skipped reason=current-missing-or-nonregular", result.stdout)

    def test_cleanup_cli_preserves_explicit_cache_root_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp) / "data"
            override_root = Path(temp) / "custom-cache"
            source_directory = override_root / "override-source"
            source_directory.mkdir(parents=True)
            (source_directory / ".lock").touch()
            environment = os.environ.copy()
            environment["DATA_ROOT"] = str(data_root)
            environment["GTFS_CACHE_ROOT"] = str(override_root)

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "gtfs_source_cache.py"),
                    "cleanup",
                    "--dry-run",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("source=override-source status=skipped reason=current-missing-or-nonregular", result.stdout)


class GTFSArtifactCacheTests(unittest.TestCase):
    def test_gzip_content_encoding_is_decoded_before_cache_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.zip"
            write_gtfs(source)
            encoded = gzip.compress(source.read_bytes())

            def fake_urlopen(request, timeout=0):
                return FakeResponse(
                    body=encoded,
                    headers={
                        "Content-Encoding": "gzip",
                        "Content-Type": "application/zip",
                    },
                )

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=fake_urlopen):
                result = GTFSArtifactCache(root / "cache").resolve(
                    "australia-transport-nsw",
                    "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses",
                    metadata_probe=False,
                )

            cached = root / "cache" / "australia-transport-nsw" / "current.zip"
            self.assertEqual(result.status, "updated")
            self.assertEqual(cached.read_bytes()[:4], b"PK\x03\x04")
            self.assertEqual(result.state["size"], source.stat().st_size)
            with zipfile.ZipFile(cached) as archive:
                self.assertIn("stops.txt", archive.namelist())

    def test_malformed_gzip_content_encoding_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def fake_urlopen(request, timeout=0):
                return FakeResponse(
                    body=b"not-a-gzip-stream",
                    headers={"Content-Encoding": "gzip"},
                )

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaisesRegex(ValueError, "Invalid gzip HTTP Content-Encoding"):
                    GTFSArtifactCache(root / "cache").resolve(
                        "nsw",
                        "https://example.test/nsw.zip",
                        metadata_probe=False,
                    )

            self.assertFalse((root / "cache" / "nsw" / "current.zip").exists())

    def test_gzip_content_encoding_that_decodes_to_non_zip_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def fake_urlopen(request, timeout=0):
                return FakeResponse(
                    body=gzip.compress(b"not-a-zip"),
                    headers={"Content-Encoding": "gzip"},
                )

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaises(zipfile.BadZipFile):
                    GTFSArtifactCache(root / "cache").resolve(
                        "nonzip",
                        "https://example.test/nonzip.zip",
                        metadata_probe=False,
                    )

            self.assertFalse((root / "cache" / "nonzip" / "current.zip").exists())

    def test_kyiv_timeout_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "kyiv.zip"
            write_kyiv_gtfs(source)
            calls = []

            def fake_urlopen(request, timeout=0):
                calls.append(request.get_method())
                if len(calls) < 3:
                    raise TimeoutError("simulated timeout")
                return FakeResponse(body=source.read_bytes())

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=fake_urlopen):
                result = GTFSArtifactCache(root / "cache").resolve(
                    "kyiv",
                    "https://data.kyivcity.gov.ua/gtfs.zip",
                    allow_stale=True,
                    metadata_probe=False,
                    retry_attempts=3,
                    validator=validate_kyiv_gtfs_archive,
                )

            self.assertEqual(result.status, "updated")
            self.assertEqual(calls, ["GET", "GET", "GET"])

    def test_kyiv_http_503_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "kyiv.zip"
            write_kyiv_gtfs(source)
            calls = []

            def fake_urlopen(request, timeout=0):
                calls.append(request.get_method())
                if len(calls) < 3:
                    return FakeResponse(status=503)
                return FakeResponse(body=source.read_bytes())

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=fake_urlopen):
                result = GTFSArtifactCache(root / "cache").resolve(
                    "kyiv",
                    "https://data.kyivcity.gov.ua/gtfs.zip",
                    allow_stale=True,
                    metadata_probe=False,
                    retry_attempts=3,
                    validator=validate_kyiv_gtfs_archive,
                )

            self.assertEqual(result.status, "updated")
            self.assertEqual(calls, ["GET", "GET", "GET"])

    def test_poland_gdansk_http_500_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "gdansk.zip"
            write_gtfs(source)
            calls = []

            def fake_urlopen(request, timeout=0):
                calls.append(request.get_method())
                if len(calls) < 3:
                    return FakeResponse(status=500)
                return FakeResponse(body=source.read_bytes())

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=fake_urlopen):
                result = GTFSArtifactCache(root / "cache").resolve(
                    "poland-gdansk",
                    "https://ckan.multimediagdansk.pl/gtfs.zip",
                    allow_stale=False,
                    metadata_probe=False,
                    retry_attempts=3,
                )

            self.assertEqual(result.status, "updated")
            self.assertEqual(calls, ["GET", "GET", "GET"])

    def test_kyiv_retry_exhaustion_uses_valid_stale_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "kyiv.zip"
            write_kyiv_gtfs(source, "old")
            cache = GTFSArtifactCache(root / "cache")
            first = cache.resolve(
                "kyiv",
                str(source),
                validator=validate_kyiv_gtfs_archive,
            )
            calls = []

            def fake_urlopen(request, timeout=0):
                calls.append(request.get_method())
                raise TimeoutError("simulated timeout")

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=fake_urlopen):
                result = cache.resolve(
                    "kyiv",
                    "https://data.kyivcity.gov.ua/gtfs.zip",
                    allow_stale=True,
                    metadata_probe=False,
                    retry_attempts=3,
                    validator=validate_kyiv_gtfs_archive,
                )

            self.assertEqual(result.status, "preserved-stale")
            self.assertEqual(len(calls), 3)
            self.assertEqual(result.state["sha256"], first.state["sha256"])
            self.assertEqual((root / "cache" / "kyiv" / "current.zip").read_bytes(), source.read_bytes())

    def test_seeded_kyiv_stale_cache_preserves_provenance_and_age(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "legacy-kyiv.zip"
            write_kyiv_gtfs(source, "legacy")
            legacy = GTFSArtifactCache(root / "legacy-cache")
            seeded = legacy.resolve(
                "kyiv",
                str(source),
                validator=validate_kyiv_gtfs_archive,
            )
            seed_state = dict(seeded.state or {})
            canonical = GTFSArtifactCache(root / "canonical-cache")

            with patch(
                "gtfs_source_cache.urllib.request.urlopen",
                side_effect=TimeoutError("simulated timeout"),
            ):
                result = canonical.resolve(
                    "kyiv",
                    "https://data.kyivcity.gov/gtfs.zip",
                    allow_stale=True,
                    metadata_probe=False,
                    retry_attempts=1,
                    validator=validate_kyiv_gtfs_archive,
                    seed_path=source,
                    seed_state=seed_state,
                )

            self.assertEqual(result.status, "preserved-stale")
            self.assertEqual(result.state["sha256"], seed_state["sha256"])
            self.assertEqual(result.state["downloadedAt"], seed_state["downloadedAt"])
            self.assertTrue(result.state["seededFromLegacyCache"])
            self.assertEqual(
                (root / "canonical-cache" / "kyiv" / "current.zip").read_bytes(),
                source.read_bytes(),
            )

    def test_invalid_kyiv_seed_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "kyiv.zip"
            write_kyiv_gtfs(source)
            with self.assertRaisesRegex(ValueError, "invalid seed state"):
                GTFSArtifactCache(root / "cache").resolve(
                    "kyiv",
                    "https://data.kyivcity.gov/gtfs.zip",
                    allow_stale=True,
                    metadata_probe=False,
                    seed_path=source,
                    seed_state={"validated": True, "sha256": "wrong", "size": source.stat().st_size},
                    validator=validate_kyiv_gtfs_archive,
                )

    def test_kyiv_corrupt_download_does_not_replace_valid_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "kyiv.zip"
            write_kyiv_gtfs(source, "old")
            cache = GTFSArtifactCache(root / "cache")
            first = cache.resolve("kyiv", str(source), validator=validate_kyiv_gtfs_archive)

            def fake_urlopen(request, timeout=0):
                return FakeResponse(body=b"partial-not-a-zip")

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=fake_urlopen):
                result = cache.resolve(
                    "kyiv",
                    "https://data.kyivcity.gov.ua/gtfs.zip",
                    allow_stale=True,
                    metadata_probe=False,
                    retry_attempts=3,
                    validator=validate_kyiv_gtfs_archive,
                )

            self.assertEqual(result.status, "preserved-stale")
            self.assertEqual(result.state["sha256"], first.state["sha256"])
            self.assertEqual((root / "cache" / "kyiv" / "current.zip").read_bytes(), source.read_bytes())

    def test_kyiv_permanent_404_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            calls = []

            def fake_urlopen(request, timeout=0):
                calls.append(request.get_method())
                raise urllib.error.HTTPError(
                    request.full_url, 404, "not found", {}, io.BytesIO()
                )

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaises(urllib.error.HTTPError):
                    GTFSArtifactCache(Path(temp) / "cache").resolve(
                        "kyiv",
                        "https://data.kyivcity.gov.ua/gtfs.zip",
                        allow_stale=False,
                        metadata_probe=False,
                        retry_attempts=3,
                        validator=validate_kyiv_gtfs_archive,
                    )

            self.assertEqual(calls, ["GET"])

    def test_germany_request_preserves_exact_url_and_legacy_user_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "germany.zip"
            write_gtfs(source)
            payload = source.read_bytes()
            cache = GTFSArtifactCache(root / "cache")
            exact_url = (
                "https://download.example.invalid/gtfs.zip?"
                "expires=2099-01-01T00%3A00%3A00Z&signature=abc123"
            )
            requests = []

            def fake_urlopen(request, timeout=0):
                requests.append((request, timeout))
                if request.get_method() == "HEAD":
                    raise urllib.error.HTTPError(
                        request.full_url,
                        403,
                        "HEAD is not supported by the feed",
                        {},
                        io.BytesIO(),
                    )
                return FakeResponse(
                    body=payload,
                    headers={"Content-Length": str(len(payload))},
                )

            with patch(
                "gtfs_source_cache.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ):
                result = cache.resolve("germany", exact_url)

            self.assertEqual(result.status, "updated")
            self.assertEqual([request.full_url for request, _ in requests], [exact_url, exact_url])
            self.assertEqual(
                [request.get_header("User-agent") for request, _ in requests],
                ["HalteWeckerStopPipeline/1.0", "HalteWeckerStopPipeline/1.0"],
            )
            self.assertEqual(requests[0][0].get_method(), "HEAD")
            self.assertEqual(requests[1][0].get_method(), "GET")

    def test_source_version_reuses_validated_local_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.zip"
            write_gtfs(source)
            cache = GTFSArtifactCache(root / "cache")

            first = cache.resolve("swiss", str(source), source_version={"version": 1})
            second = cache.resolve("swiss", str(source), source_version={"version": 1})

            self.assertEqual(first.status, "updated")
            self.assertEqual(second.status, "unchanged")
            state = json.loads((root / "cache" / "swiss" / "state.json").read_text())
            self.assertTrue(state["validated"])

    def test_download_preflight_uses_get_and_preserves_request_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.zip"
            write_gtfs(source)
            requests = []

            def fake_urlopen(request, timeout=0):
                requests.append((request, timeout))
                self.assertEqual(request.get_method(), "GET")
                return FakeResponse(body=source.read_bytes())

            with patch(
                "gtfs_source_cache.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ):
                result = GTFSArtifactCache(root / "cache").resolve(
                    "kyiv",
                    "https://data.example.test/resource/data/download",
                    headers={
                        "Referer": "https://data.example.test/",
                        "User-Agent": "Mozilla/5.0 test",
                    },
                    allow_stale=False,
                    metadata_probe=False,
                )

            self.assertEqual(result.status, "updated")
            self.assertEqual(len(requests), 1)
            request = requests[0][0]
            self.assertEqual(request.get_header("Referer"), "https://data.example.test/")
            self.assertEqual(request.get_header("User-agent"), "Mozilla/5.0 test")

    def test_invalid_download_stops_even_when_previous_cache_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.zip"
            write_gtfs(source, "old")
            cache = GTFSArtifactCache(root / "cache")
            first = cache.resolve("kyiv", str(source), source_version={"version": 1})

            def fake_urlopen(request, timeout=0):
                self.assertEqual(request.get_method(), "GET")
                return FakeResponse(body=b"<html>download failed</html>")

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaises(zipfile.BadZipFile):
                    cache.resolve(
                        "kyiv",
                        "https://data.example.test/resource/data/download",
                        allow_stale=False,
                        metadata_probe=False,
                    )

            state = json.loads((root / "cache" / "kyiv" / "state.json").read_text())
            self.assertEqual(state["sha256"], first.state["sha256"])

    def test_invalid_candidate_keeps_previous_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.zip"
            write_gtfs(source, "old")
            cache = GTFSArtifactCache(root / "cache")
            first = cache.resolve("germany", str(source), source_version={"version": 1})
            old_checksum = first.state["sha256"]

            source.write_bytes(b"truncated")
            with self.assertRaises(Exception):
                cache.resolve("germany", str(source), source_version={"version": 2})

            state = json.loads((root / "cache" / "germany" / "state.json").read_text())
            self.assertEqual(state["sha256"], old_checksum)
            self.assertEqual(cache.resolve("germany", str(source), source_version={"version": 1}).status, "unchanged")

    def test_matching_etag_avoids_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.zip"
            write_gtfs(source)
            cache = GTFSArtifactCache(root / "cache")
            first = cache.resolve("swiss", str(source), source_version={"version": 1})
            state_path = root / "cache" / "swiss" / "state.json"
            state = json.loads(state_path.read_text())
            state["etag"] = '"same"'
            state["url"] = "https://example.invalid/swiss.zip"
            state.pop("downloadedAt", None)
            state_path.write_text(json.dumps(state))

            def fake_urlopen(request, timeout=0):
                self.assertEqual(request.get_method(), "HEAD")
                return FakeResponse(status=200, headers={"ETag": '"same"'})

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=fake_urlopen) as mocked:
                result = cache.resolve("swiss", "https://example.invalid/swiss.zip")

            self.assertEqual(result.status, "unchanged")
            mocked.assert_called_once()
            persisted = json.loads(state_path.read_text())
            self.assertEqual(persisted["sourceCheckStatus"], "success")
            self.assertIsInstance(persisted["sourceCheckedAt"], str)
            self.assertIsInstance(persisted["downloadedAt"], str)

    def test_same_checksum_after_fresh_get_records_upstream_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.zip"
            write_gtfs(source)
            cache = GTFSArtifactCache(root / "cache")
            first = cache.resolve("vbb", str(source), source_version={"version": 1})
            state_path = root / "cache" / "vbb" / "state.json"
            state = json.loads(state_path.read_text())
            state.pop("sourceCheckedAt", None)
            state.pop("sourceCheckStatus", None)
            state_path.write_text(json.dumps(state))

            with patch(
                "gtfs_source_cache.urllib.request.urlopen",
                return_value=FakeResponse(body=source.read_bytes()),
            ):
                result = cache.resolve(
                    "vbb",
                    "https://example.invalid/vbb.zip",
                    metadata_probe=False,
                )

            self.assertEqual(result.status, "unchanged")
            persisted = json.loads(state_path.read_text())
            self.assertEqual(persisted["sha256"], first.state["sha256"])
            self.assertEqual(persisted["sourceCheckStatus"], "success")
            self.assertIsInstance(persisted["sourceCheckedAt"], str)

    def test_austria_uses_mvo_version_without_downloading_again(self) -> None:
        from datetime import date

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            artifact = root / "source.zip"
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\nstop,Fixture,48,16\n")
                archive.writestr("routes.txt", "route_id,route_short_name,route_type\nroute,U1,1\n")
                archive.writestr("trips.txt", "route_id,service_id,trip_id\nroute,service,trip\n")
                archive.writestr("stop_times.txt", "trip_id,departure_time,stop_id,stop_sequence\ntrip,10:00:00,stop,1\n")
                archive.writestr(
                    "calendar.txt",
                    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "service,1,1,1,1,1,1,1,20260101,20301231\n",
                )

            source = {"id": "vor", "datasetId": 52}
            created = "2026-11-17T04:30:48Z"
            size = artifact.stat().st_size
            catalog = [{"id": 52, "year": 2026, "originalName": "vor.zip", "size": size, "created": created}]
            metadata = {"datasetId": 52, "year": 2026, "versionId": 52,
                        "created": created, "originalName": "vor.zip", "size": size}
            cache = GTFSArtifactCache(root / "cache")
            # Seed the real cache with remote provenance, then forbid further requests.
            with patch("urllib.request.urlopen", return_value=FakeResponse(artifact.read_bytes())):
                first = cache.resolve("vor", "https://example.invalid/data-sets/52/2026/file",
                                      source_version=metadata, metadata_probe=False)
            with patch("urllib.request.urlopen", side_effect=AssertionError("unexpected download")) as request, \
                    patch("download_austrian_gtfs.source_calendar_local_date", return_value=date(2026, 11, 17)):
                result = download_source(source, catalog, "token", root / "austria",
                                         {"MVO_API_BASE": "https://example.invalid"}, cache)

            request.assert_not_called()
            self.assertEqual(result["status"], "unchanged")
            self.assertEqual(result["reason"], "source-version")
            self.assertEqual(result["year"], 2026)
            self.assertEqual(cache.state("vor"), first.state)
            self.assertTrue((root / "austria" / "vor-2026.zip").samefile(first.path))

    def test_successful_update_removes_old_hash_and_keeps_new_current_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.zip"
            write_gtfs(source)
            cache = GTFSArtifactCache(root / "cache")
            first = cache.resolve("sweden", str(source), metadata_probe=False)
            current = root / "cache" / "sweden" / "current.zip"
            old_hash = current.with_name(f"{first.state['sha256']}.zip")
            os.link(current, old_hash)

            write_gtfs(source, "new")
            second = cache.resolve("sweden", str(source), metadata_probe=False)
            new_hash = current.with_name(f"{second.state['sha256']}.zip")
            os.link(current, new_hash)

            self.assertNotEqual(first.state["sha256"], second.state["sha256"])
            self.assertFalse(old_hash.exists())
            self.assertTrue(current.exists())
            self.assertTrue(new_hash.samefile(current))
            self.assertTrue((current.parent / ".lock").exists())
            self.assertTrue((current.parent / "state.json").exists())

    def test_cleanup_preserves_current_pair_counts_unique_reclaimed_bytes_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.zip"
            write_gtfs(source)
            cache = GTFSArtifactCache(root / "cache")
            first = cache.resolve("sweden", str(source), metadata_probe=False)
            directory = root / "cache" / "sweden"
            current = directory / "current.zip"
            current_hash = directory / f"{first.state['sha256']}.zip"
            os.link(current, current_hash)

            reclaimable_hash = directory / ("a" * 64 + ".zip")
            reclaimable_hash.write_bytes(b"reclaimable")
            shared_hash = directory / ("b" * 64 + ".zip")
            outside_link = root / "outside.zip"
            outside_link.write_bytes(b"shared")
            os.link(outside_link, shared_hash)
            orphan_temp = directory / ".download-orphan.zip"
            orphan_temp.write_bytes(b"")
            old_time = time.time() - 2 * 24 * 60 * 60
            os.utime(orphan_temp, (old_time, old_time))

            result = cache.cleanup_source(
                "sweden",
                orphan_temp_max_age_seconds=60 * 60,
            )

            self.assertEqual(result.status, "cleaned")
            self.assertEqual(result.reclaimed_bytes, len(b"reclaimable"))
            self.assertTrue(current_hash.samefile(current))
            self.assertFalse(reclaimable_hash.exists())
            self.assertFalse(shared_hash.exists())
            self.assertTrue(outside_link.exists())
            self.assertFalse(orphan_temp.exists())
            self.assertTrue((directory / ".lock").exists())
            self.assertTrue((directory / "state.json").exists())

            second = cache.cleanup_source("sweden", orphan_temp_max_age_seconds=60 * 60)
            self.assertEqual(second.status, "cleaned")
            self.assertEqual(second.removed, ())
            self.assertEqual(second.orphan_temp_removed, ())
            self.assertEqual(second.reclaimed_bytes, 0)

    def test_cleanup_does_not_remove_only_hash_from_incomplete_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "cache" / "incomplete"
            directory.mkdir(parents=True)
            (directory / ".lock").touch()
            only_copy = directory / ("c" * 64 + ".zip")
            only_copy.write_bytes(b"potential working copy")

            result = GTFSArtifactCache(Path(temp) / "cache").cleanup_source("incomplete")

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.reason, "current-missing-or-nonregular")
            self.assertTrue(only_copy.exists())

    def test_cleanup_skips_missing_or_nonregular_lock_without_creating_it(self) -> None:
        for lock_kind in ("missing", "symlink", "directory"):
            with self.subTest(lock_kind=lock_kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "cache"
                directory = root / "sweden"
                directory.mkdir(parents=True)
                lock_path = directory / ".lock"
                if lock_kind == "symlink":
                    target = Path(temp) / "lock-target"
                    target.touch()
                    lock_path.symlink_to(target)
                elif lock_kind == "directory":
                    lock_path.mkdir()

                result = GTFSArtifactCache(root).cleanup_source("sweden")

                self.assertEqual(result.status, "skipped")
                self.assertEqual(result.reason, "lock-missing-or-nonregular")
                if lock_kind == "missing":
                    self.assertFalse(lock_path.exists())

    def test_cleanup_still_skips_kyiv_state_hash_inode_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "cache"
            source = Path(temp) / "source.zip"
            write_gtfs(source)
            cache = GTFSArtifactCache(root)
            result = cache.resolve("kyiv", str(source), metadata_probe=False)
            directory = root / "kyiv"
            expected_hash = directory / f"{result.state['sha256']}.zip"
            expected_hash.write_bytes(b"same name, different inode")

            cleanup = cache.cleanup_source("kyiv")

            self.assertEqual(cleanup.status, "skipped")
            self.assertEqual(cleanup.reason, "state-hash-inode-mismatch")
            self.assertTrue((directory / "current.zip").exists())
            self.assertTrue(expected_hash.exists())

    def test_http_304_and_checksum_hit_remove_download_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.zip"
            write_gtfs(source)
            payload = source.read_bytes()
            cache = GTFSArtifactCache(root / "cache")

            with patch(
                "gtfs_source_cache.urllib.request.urlopen",
                return_value=FakeResponse(body=payload),
            ):
                first = cache.resolve(
                    "sweden",
                    "https://example.test/sweden.zip",
                    metadata_probe=False,
                )

            def not_modified(request, timeout=0):
                return FakeResponse(status=304)

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=not_modified):
                result_304 = cache.resolve(
                    "sweden",
                    "https://example.test/sweden.zip",
                    metadata_probe=False,
                )

            self.assertEqual(first.status, "updated")
            self.assertEqual(result_304.status, "unchanged")
            self.assertEqual(result_304.reason, "HTTP 304")
            self.assertEqual(list((root / "cache" / "sweden").glob(".download-*.zip")), [])

            with patch(
                "gtfs_source_cache.urllib.request.urlopen",
                return_value=FakeResponse(body=payload),
            ):
                result_checksum = cache.resolve(
                    "sweden",
                    "https://example.test/sweden.zip",
                    metadata_probe=False,
                )

            self.assertEqual(result_checksum.status, "unchanged")
            self.assertEqual(result_checksum.reason, "checksum")
            self.assertEqual(list((root / "cache" / "sweden").glob(".download-*.zip")), [])

    def test_cleanup_skips_source_when_update_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.zip"
            write_gtfs(source)
            cache = GTFSArtifactCache(root / "cache")
            first = cache.resolve("sweden", str(source), metadata_probe=False)
            directory = root / "cache" / "sweden"
            old_hash = directory / f"{first.state['sha256']}.zip"
            os.link(directory / "current.zip", old_hash)
            active_temp = directory / ".download-active.zip"
            active_temp.write_bytes(b"active")
            ready = root / "lock-ready"
            lock_path = directory / ".lock"
            child_code = (
                "import fcntl, pathlib, sys, time\n"
                "lock = open(sys.argv[1], 'a+')\n"
                "fcntl.flock(lock.fileno(), fcntl.LOCK_EX)\n"
                "pathlib.Path(sys.argv[2]).touch()\n"
                "time.sleep(10)\n"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, str(lock_path), str(ready)]
            )
            try:
                deadline = time.time() + 3
                while not ready.exists() and time.time() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                result = cache.cleanup_source("sweden", orphan_temp_max_age_seconds=0)
                self.assertEqual(result.status, "skipped")
                self.assertEqual(result.reason, "lock-held-active-update")
                self.assertTrue(old_hash.exists())
                self.assertTrue(active_temp.exists())
            finally:
                child.terminate()
                child.wait(timeout=5)


class GTFSConditionalRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.zip"
        write_gtfs(self.source, "old")
        self.cache = GTFSArtifactCache(self.root / "cache")
        self.url = "https://example.test/source.zip"
        self.validators = {
            "ETag": '"old"',
            "Last-Modified": "Mon, 16 Nov 2026 00:00:00 GMT",
        }
        with patch(
            "gtfs_source_cache.urllib.request.urlopen",
            return_value=FakeResponse(self.source.read_bytes(), headers=self.validators),
        ):
            self.original = self.cache.resolve("source", self.url, metadata_probe=False)
        self.current = self.original.path
        self.state_path = self.current.with_name("state.json")
        self.hash_path = self.current.with_name(f"{self.original.state['sha256']}.zip")
        os.link(self.current, self.hash_path)
        self.state_bytes = self.state_path.read_bytes()
        self.new_source = self.root / "new.zip"
        write_gtfs(self.new_source, "new")

    def assert_original_cache_preserved(self) -> None:
        self.assertEqual(self.current.read_bytes(), self.source.read_bytes())
        self.assertTrue(self.hash_path.samefile(self.current))
        self.assertEqual(list(self.current.parent.glob(".download-*.zip")), [])
        self.assertEqual(list(self.current.parent.glob(".state-*.json")), [])

    def test_http_error_304_after_unsupported_head_reuses_valid_archive(self) -> None:
        methods = []
        response_body = io.BytesIO()

        def respond(request, timeout=0):
            methods.append(request.get_method())
            if request.get_method() == "HEAD":
                raise urllib.error.HTTPError(self.url, 404, "HEAD unsupported", {}, io.BytesIO())
            self.assertEqual(request.get_header("If-none-match"), '"old"')
            self.assertEqual(request.get_header("If-modified-since"), self.validators["Last-Modified"])
            raise urllib.error.HTTPError(self.url, 304, "Not Modified", {}, response_body)

        with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=respond), patch(
            "gtfs_source_cache._now", return_value="2026-11-17T00:40:00Z"
        ), patch.object(self.cache, "_cleanup_after_activation") as cleanup:
            result = self.cache.resolve("source", self.url, allow_stale=True)

        self.assertEqual(methods, ["HEAD", "GET"])
        self.assertEqual((result.status, result.reason), ("unchanged", "HTTP 304"))
        for field in ["sha256", "size", "downloadedAt", "validatedAt"]:
            self.assertEqual(result.state[field], self.original.state[field])
        self.assertEqual(result.state["sourceCheckedAt"], "2026-11-17T00:40:00Z")
        self.assertEqual(result.state["sourceCheckStatus"], "success")
        self.assertTrue(response_body.closed)
        cleanup.assert_not_called()
        self.assert_original_cache_preserved()

    def test_head_http_error_304_reuses_valid_archive(self) -> None:
        response_body = io.BytesIO()
        error = urllib.error.HTTPError(self.url, 304, "Not Modified", {}, response_body)
        with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=error) as request:
            result = self.cache.resolve("source", self.url)
        self.assertEqual((result.status, result.reason), ("unchanged", "HTTP 304"))
        request.assert_called_once()
        self.assertTrue(response_body.closed)
        self.assert_original_cache_preserved()

    def test_absent_validators_are_not_sent(self) -> None:
        for value in [None, "", "  ", "None"]:
            with self.subTest(value=value):
                state = dict(self.original.state, etag=value, lastModified=value)
                self.state_path.write_text(json.dumps(state))

                def respond(request, timeout=0):
                    self.assertIsNone(request.get_header("If-none-match"))
                    self.assertIsNone(request.get_header("If-modified-since"))
                    return FakeResponse(self.source.read_bytes())

                with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=respond):
                    result = self.cache.resolve("source", self.url, metadata_probe=False)
                self.assertEqual((result.status, result.reason), ("unchanged", "checksum"))
                self.assert_original_cache_preserved()

    def test_each_available_validator_is_sent_independently(self) -> None:
        cases = [
            ('"old"', None, '"old"', None),
            (None, self.validators["Last-Modified"], None, self.validators["Last-Modified"]),
        ]
        for etag, modified, expected_etag, expected_modified in cases:
            with self.subTest(etag=etag, modified=modified):
                self.state_path.write_text(json.dumps(dict(self.original.state, etag=etag, lastModified=modified)))

                def respond(request, timeout=0):
                    self.assertEqual(request.get_header("If-none-match"), expected_etag)
                    self.assertEqual(request.get_header("If-modified-since"), expected_modified)
                    return FakeResponse(self.source.read_bytes())

                with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=respond):
                    result = self.cache.resolve("source", self.url, metadata_probe=False)
                self.assertEqual(result.status, "unchanged")

    def test_changed_head_metadata_downloads_before_updating_state(self) -> None:
        cases = [
            {"ETag": '"new"'},
            {"ETag": '"new"', "Last-Modified": self.validators["Last-Modified"]},
            {"Last-Modified": "Tue, 17 Nov 2026 00:00:00 GMT"},
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                self.state_path.write_bytes(self.state_bytes)
                self.current.write_bytes(self.source.read_bytes())
                methods = []

                def respond(request, timeout=0):
                    methods.append(request.get_method())
                    if request.get_method() == "HEAD":
                        return FakeResponse(headers=headers)
                    self.assertEqual(self.state_path.read_bytes(), self.state_bytes)
                    self.assertEqual(request.get_header("If-none-match"), '"old"')
                    return FakeResponse(self.new_source.read_bytes(), headers=headers)

                with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=respond):
                    result = self.cache.resolve("source", self.url)
                self.assertEqual(methods, ["HEAD", "GET"])
                self.assertEqual(result.status, "updated")
                self.assertEqual(self.current.read_bytes(), self.new_source.read_bytes())
                self.assertEqual(result.state["etag"], headers.get("ETag"))
                self.assertEqual(result.state["lastModified"], headers.get("Last-Modified"))
                self.assertNotEqual(result.state["sha256"], self.original.state["sha256"])

    def test_failed_download_after_head_change_preserves_metadata_and_retention_pair(self) -> None:
        for failure in ["http", "invalid-zip"]:
            with self.subTest(failure=failure):
                def respond(request, timeout=0):
                    if request.get_method() == "HEAD":
                        return FakeResponse(headers={"ETag": '"new"'})
                    self.assertEqual(self.state_path.read_bytes(), self.state_bytes)
                    if failure == "http":
                        raise urllib.error.HTTPError(self.url, 503, "Unavailable", {}, io.BytesIO())
                    return FakeResponse(b"invalid zip", headers={"ETag": '"new"'})

                with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=respond):
                    result = self.cache.resolve("source", self.url, allow_stale=True)
                self.assertEqual(result.status, "preserved-stale")
                self.assertEqual(self.state_path.read_bytes(), self.state_bytes)
                self.assert_original_cache_preserved()

    def test_missing_archive_is_downloaded_without_cached_validators(self) -> None:
        self.current.unlink()
        methods = []

        def respond(request, timeout=0):
            methods.append(request.get_method())
            self.assertIsNone(request.get_header("If-none-match"))
            self.assertIsNone(request.get_header("If-modified-since"))
            if request.get_method() == "HEAD":
                raise urllib.error.HTTPError(self.url, 304, "Not Modified", {}, io.BytesIO())
            return FakeResponse(self.new_source.read_bytes(), headers={"ETag": '"new"'})

        with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=respond):
            result = self.cache.resolve("source", self.url)
        self.assertEqual(methods, ["HEAD", "GET"])
        self.assertEqual(result.status, "updated")
        self.assertEqual(self.current.read_bytes(), self.new_source.read_bytes())

    def test_http_error_304_without_valid_archive_fails_closed(self) -> None:
        for archive_kind in ["missing", "corrupt"]:
            with self.subTest(archive_kind=archive_kind):
                if archive_kind == "missing":
                    self.current.unlink()
                else:
                    self.current.write_bytes(b"corrupt zip")
                error = urllib.error.HTTPError(self.url, 304, "Not Modified", {}, io.BytesIO())
                with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=error):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        self.cache.resolve("source", self.url, metadata_probe=False, allow_stale=True)
                self.assertEqual(raised.exception.code, 304)
                self.assertEqual(self.state_path.read_bytes(), self.state_bytes)
                self.assertEqual(list(self.current.parent.glob(".download-*.zip")), [])
                if archive_kind == "missing":
                    self.assertFalse(self.current.exists())
                else:
                    self.assertEqual(self.current.read_bytes(), b"corrupt zip")
                error.close()

    def test_forced_download_does_not_accept_304_as_unchanged(self) -> None:
        error = urllib.error.HTTPError(self.url, 304, "Not Modified", {}, io.BytesIO())

        def respond(request, timeout=0):
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.get_header("If-none-match"))
            self.assertIsNone(request.get_header("If-modified-since"))
            raise error

        with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=respond):
            with self.assertRaises(urllib.error.HTTPError):
                self.cache.resolve("source", self.url, force_download=True)
        error.close()
        self.assertEqual(self.state_path.read_bytes(), self.state_bytes)
        self.assert_original_cache_preserved()


if __name__ == "__main__":
    unittest.main()
