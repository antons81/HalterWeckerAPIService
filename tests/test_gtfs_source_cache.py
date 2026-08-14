import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from gtfs_source_cache import GTFSArtifactCache
from gtfs_source_cache import ArtifactResult
from download_austrian_gtfs import download_source


def write_gtfs(path: Path, marker: str = "1") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            archive.writestr(name, f"marker,{marker}\n")


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


class GTFSArtifactCacheTests(unittest.TestCase):
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
            state_path.write_text(json.dumps(state))

            def fake_urlopen(request, timeout=0):
                self.assertEqual(request.get_method(), "HEAD")
                return FakeResponse(status=200, headers={"ETag": '"same"'})

            with patch("gtfs_source_cache.urllib.request.urlopen", side_effect=fake_urlopen) as mocked:
                result = cache.resolve("swiss", "https://example.invalid/swiss.zip")

            self.assertEqual(result.status, "unchanged")
            mocked.assert_called_once()

    def test_austria_uses_mvo_version_without_downloading_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            artifact = root / "current.zip"
            write_gtfs(artifact)

            class FakeCache:
                def resolve(self, source_id, url, **kwargs):
                    self.kwargs = kwargs
                    return ArtifactResult(source_id, artifact, "unchanged", "source-version", {"size": artifact.stat().st_size})

            source = {"id": "vor", "datasetId": 52}
            catalog = [{"id": 52, "year": 2026, "originalName": "vor.zip", "size": artifact.stat().st_size}]
            result = download_source(
                source,
                catalog,
                "token",
                root / "austria",
                {"MVO_API_BASE": "https://example.invalid"},
                FakeCache(),
            )

            self.assertEqual(result["status"], "unchanged")
            self.assertEqual(result["year"], 2026)
            self.assertTrue((root / "austria" / "vor-2026.zip").samefile(artifact))


if __name__ == "__main__":
    unittest.main()
