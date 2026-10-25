import io
import json
import ssl
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from austrian_sources import load_austrian_sources, public_stop_id
from build_stop_packages import build_austrian_stop_packages_from_sources
from validate_austrian_stop_packages import validate
import download_austrian_gtfs as downloader
from gtfs_source_cache import GTFSArtifactCache
from release_integrity import validate_artifact_entry


class AustrianTLSVerificationTests(unittest.TestCase):
    def test_bundled_intermediate_keeps_hostname_and_trusted_root_verification(self) -> None:
        context = downloader.mvo_tls_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN)
        self.assertTrue(any(
            ("commonName", "Go Daddy Secure Certificate Authority - G2") in group
            for certificate in context.get_ca_certs()
            for group in certificate["subject"]
        ))

    def test_unexpected_intermediate_fails_closed(self) -> None:
        with mock.patch.object(downloader, "MVO_INTERMEDIATE_SHA256", "0" * 64):
            with self.assertRaisesRegex(RuntimeError, "fingerprint mismatch"):
                downloader.mvo_tls_context()

    def test_missing_intermediate_does_not_disable_certificate_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(downloader, "MVO_INTERMEDIATE_CERTIFICATE", Path(temporary) / "missing.pem"):
                with self.assertRaises(FileNotFoundError):
                    downloader.mvo_tls_context()

    def test_catalog_token_and_gtfs_cache_head_get_share_verified_opener(self) -> None:
        previous_opener = urllib.request._opener
        self.addCleanup(urllib.request.install_opener, previous_opener)
        requests = []
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as feed:
            for name in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
                feed.writestr(name, "id,name\n1,Fixture\n")

        def open_request(request, data=None, timeout=None):
            requests.append((request.full_url, request.get_method()))
            if request.full_url.endswith("/token"):
                body = b'{"access_token":"fixture-token"}'
            elif request.full_url.endswith("/catalog"):
                body = b'{"dataSets":[]}'
            else:
                body = archive.getvalue()
            response = io.BytesIO(body)
            response.status = 200
            response.headers = {"ETag": '"fixture-v1"', "Content-Length": str(len(body))}
            return response

        opener = mock.Mock()
        opener.open.side_effect = open_request
        with mock.patch.object(urllib.request, "build_opener", return_value=opener) as build:
            downloader.configure_mvo_https()
        context = build.call_args.args[0]._context
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(downloader.request_json("https://fixture.test/catalog"), {"dataSets": []})
        self.assertEqual(downloader.token({
            "MVO_USERNAME": "fixture", "MVO_PASSWORD": "fixture",
            "MVO_TOKEN_URL": "https://fixture.test/token",
        }), "fixture-token")
        with tempfile.TemporaryDirectory() as temporary:
            cache = GTFSArtifactCache(Path(temporary))
            cache.resolve("fixture", "https://fixture.test/feed.zip")
            cache.resolve("fixture", "https://fixture.test/feed.zip")
        self.assertIn(("https://fixture.test/feed.zip", "GET"), requests)
        self.assertIn(("https://fixture.test/feed.zip", "HEAD"), requests)
        self.assertIn(("https://fixture.test/token", "POST"), requests)

    def test_https_is_configured_before_first_catalog_request(self) -> None:
        environment = {
            "MVO_USERNAME": "fixture", "MVO_PASSWORD": "fixture",
            "MVO_TOKEN_URL": "https://fixture.test/token", "MVO_API_BASE": "https://fixture.test/api",
        }
        with mock.patch.object(sys, "argv", ["download_austrian_gtfs.py"]), \
                mock.patch.object(downloader, "read_env", return_value=environment), \
                mock.patch.object(downloader, "configure_mvo_https") as configure:
            def request(*args, **kwargs):
                configure.assert_called_once_with()
                raise ValueError("fixture catalog invalid")
            with mock.patch.object(downloader, "request_json", side_effect=request):
                with self.assertRaises(ValueError):
                    downloader.main()


class AustrianOutageFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = GTFSArtifactCache(self.root / "gtfs" / "austria")
        self.output = self.root / "output"
        self.sources = load_austrian_sources(downloader.DEFAULT_REGISTRY)
        self.environment = {
            "MVO_API_BASE": "https://fixture.test/api", "MVO_TOKEN_URL": "https://fixture.test/token",
            "MVO_USERNAME": "fixture", "MVO_PASSWORD": "fixture",
        }
        self.unavailable = urllib.error.HTTPError("https://fixture.test/catalog", 502, "Bad Gateway", {}, None)

    def seed(self, *, through="20301231") -> None:
        archive_path = self.root / "source.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n1,Fixture,48,16\n")
            archive.writestr("routes.txt", "route_id,route_short_name,route_type\nr,1,3\n")
            archive.writestr("trips.txt", "route_id,service_id,trip_id\nr,s,t\n")
            archive.writestr("stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt,10:00:00,10:00:00,1,1\n")
            archive.writestr("calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                f"s,1,1,1,1,1,1,1,20260101,{through}\n")
        for source in self.sources:
            self.cache.resolve(str(source["id"]), str(archive_path), source_version={
                "datasetId": source["datasetId"], "year": 2026, "originalName": "fixture-2026.zip",
            })

    def refresh(self):
        return downloader.refresh_sources(self.sources, self.environment, self.output, self.cache)

    def test_catalog_502_preserves_all_eight_feeds_with_valid_provenance(self) -> None:
        self.seed()
        before = {source["id"]: self.cache.state(str(source["id"])) for source in self.sources}
        with mock.patch.object(downloader, "request_json", side_effect=self.unavailable), \
                mock.patch.object(downloader, "token") as token, mock.patch("builtins.print") as output:
            results = self.refresh()
        token.assert_not_called()
        self.assertEqual({entry["source"] for entry in results}, {source["id"] for source in self.sources})
        for entry in results:
            self.assertEqual(entry["status"], "preserved-stale")
            self.assertIn("HTTP 502", entry["reason"])
            self.assertEqual(entry["downloadedAt"], before[entry["source"]]["downloadedAt"])
            self.assertEqual(self.cache.state(entry["source"]), before[entry["source"]])
            self.assertTrue(Path(entry["path"]).is_symlink())
            self.assertTrue(Path(entry["path"]).samefile(Path(entry["immutablePath"])))
            validate_artifact_entry(entry["source"], entry, base_dir=self.root)
        self.assertIn("status=WARN", str(output.call_args_list))
        self.assertEqual(list(self.output.glob("*.next")), [])

    def test_main_continues_and_writes_complete_artifact_manifest_after_502(self) -> None:
        self.seed()
        report_path = self.root / "austrian-artifacts.json"
        arguments = ["download_austrian_gtfs.py", "--cache-root", str(self.cache.root.parent),
                     "--output", str(self.output), "--output-json", str(report_path)]
        with mock.patch.object(sys, "argv", arguments), \
                mock.patch.object(downloader, "read_env", return_value=self.environment), \
                mock.patch.object(downloader, "configure_mvo_https"), \
                mock.patch.object(downloader, "request_json", side_effect=self.unavailable), \
                mock.patch("builtins.print"):
            downloader.main()
        entries = json.loads(report_path.read_text())["sources"]
        self.assertEqual(len(entries), 8)
        self.assertTrue(all(entry["status"] == "preserved-stale" for entry in entries))
        for entry in entries:
            validate_artifact_entry(entry["source"], entry, base_dir=self.root)

    def test_one_missing_or_damaged_or_foreign_cache_blocks_incomplete_fallback(self) -> None:
        self.seed()
        source_path = self.cache.root / "vor" / "current.zip"
        state_path = source_path.with_name("state.json")
        original_archive = source_path.read_bytes()
        original_state = state_path.read_text()
        for case in ("missing", "bad-zip", "checksum", "foreign-dataset"):
            with self.subTest(case=case):
                source_path.write_bytes(original_archive)
                state_path.write_text(original_state)
                if case == "missing":
                    source_path.unlink()
                elif case == "bad-zip":
                    source_path.write_bytes(b"corrupt archive")
                elif case == "checksum":
                    state = json.loads(original_state)
                    state["sha256"] = "0" * 64
                    state_path.write_text(json.dumps(state))
                else:
                    state = json.loads(original_state)
                    state["sourceVersion"]["datasetId"] = 53
                    state_path.write_text(json.dumps(state))
                with mock.patch.object(downloader, "request_json", side_effect=self.unavailable):
                    with self.assertRaisesRegex(RuntimeError, "required Austrian cache is unusable: vor"):
                        self.refresh()
                self.assertFalse(self.output.exists())

    def test_expired_cached_calendar_is_not_extended_or_rejected_for_source_outage(self) -> None:
        self.seed(through="20261024")
        with mock.patch.object(downloader, "request_json", side_effect=self.unavailable), \
                mock.patch("builtins.print"):
            results = self.refresh()
        self.assertTrue(all(row["serviceThrough"] == "2026-10-24" for row in results))
        for row in results:
            with zipfile.ZipFile(row["immutablePath"]) as archive:
                self.assertIn(b"20261024", archive.read("calendar.txt"))

    def test_unavailable_token_endpoint_preserves_valid_cached_feeds(self) -> None:
        self.seed()
        with mock.patch.object(downloader, "request_json", return_value=[]), \
                mock.patch.object(downloader, "token", side_effect=self.unavailable), \
                mock.patch("builtins.print"):
            self.assertEqual(len(self.refresh()), 8)

    def test_recovered_source_resumes_normal_refresh_instead_of_staying_on_fallback(self) -> None:
        self.seed()
        with mock.patch.object(downloader, "request_json", side_effect=self.unavailable), \
                mock.patch("builtins.print"):
            self.assertTrue(all(row["status"] == "preserved-stale" for row in self.refresh()))
        def download(source, *args):
            return {"source": source["id"], "status": "updated", "reason": "fresh download"}
        with mock.patch.object(downloader, "request_json", return_value=[]), \
                mock.patch.object(downloader, "token", return_value="fixture-token"), \
                mock.patch.object(downloader, "download_source", side_effect=download) as download, \
                mock.patch.object(downloader, "preserve_cached_sources") as preserve, \
                mock.patch("builtins.print"):
            results = self.refresh()
        preserve.assert_not_called()
        self.assertEqual(download.call_count, 8)
        self.assertTrue(all(row["status"] == "updated" for row in results))

    def test_failed_link_publication_removes_temporary_link(self) -> None:
        self.seed()
        with mock.patch.object(downloader.os, "replace", side_effect=OSError("fixture write failure")):
            with self.assertRaises(OSError):
                downloader.preserve_cached_sources(self.sources, self.cache, self.output, self.unavailable)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_unavailable_individual_feed_does_not_skip_other_sources(self) -> None:
        self.seed()
        def download(source, *args):
            if source["id"] == "vor":
                raise self.unavailable
            return {"source": source["id"], "status": "updated", "reason": "fresh download"}
        with mock.patch.object(downloader, "request_json", return_value=[]), \
                mock.patch.object(downloader, "token", return_value="fixture-token"), \
                mock.patch.object(downloader, "download_source", side_effect=download), \
                mock.patch("builtins.print"):
            results = self.refresh()
        self.assertEqual(len(results), 8)
        self.assertEqual(results[0]["status"], "preserved-stale")
        self.assertTrue(all(row["status"] == "updated" for row in results[1:]))

    def test_authentication_and_catalog_integrity_errors_are_not_swallowed(self) -> None:
        self.seed()
        for status in (401, 403, 404):
            with self.subTest(status=status), \
                    mock.patch.object(downloader, "request_json", side_effect=urllib.error.HTTPError(
                        "https://fixture.test/catalog", status, "fixture error", {}, None)):
                with self.assertRaises(urllib.error.HTTPError):
                    self.refresh()
        with mock.patch.object(downloader, "request_json", return_value={"invalid": True}):
            with self.assertRaisesRegex(RuntimeError, "no dataset list"):
                self.refresh()
        with mock.patch.object(downloader, "request_json", return_value=[]), \
                mock.patch.object(downloader, "token", return_value="fixture-token"), \
                mock.patch.object(downloader, "download_source", side_effect=ValueError("invalid GTFS")):
            with self.assertRaisesRegex(ValueError, "invalid GTFS"):
                self.refresh()

    def test_json_request_retries_transient_failure_but_not_invalid_credentials(self) -> None:
        success = io.BytesIO(b'{"dataSets":[]}')
        success.status = 200
        with mock.patch.object(urllib.request, "urlopen", side_effect=[self.unavailable, success]) as request, \
                mock.patch.object(downloader.time, "sleep") as sleep, mock.patch("builtins.print"):
            self.assertEqual(downloader.request_json("https://fixture.test/catalog"), {"dataSets": []})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)
        for status, attempts in ((502, 3), (401, 1)):
            with self.subTest(status=status), \
                    mock.patch.object(urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                        "https://fixture.test/catalog", status, "fixture error", {}, None)) as request, \
                    mock.patch.object(downloader.time, "sleep"), mock.patch("builtins.print"):
                with self.assertRaises(urllib.error.HTTPError):
                    downloader.request_json("https://fixture.test/catalog")
                self.assertEqual(request.call_count, attempts)


class AustrianSourceRegistryTests(unittest.TestCase):
    def test_registry_covers_eight_cities_and_linz_has_two_feeds(self) -> None:
        sources = load_austrian_sources(Path(__file__).resolve().parents[1] / "config" / "austrian-sources.json")
        self.assertEqual({city for source in sources for city in source["cities"]}, {
            "wien", "graz", "linz", "salzburg", "innsbruck", "klagenfurt", "st-poelten", "bregenz"
        })
        self.assertEqual([source["id"] for source in sources if "linz" in source["cities"]], ["ooevv", "linz-ag"])

    def test_regional_prefixes_do_not_collide_and_vor_ids_remain_compatible(self) -> None:
        sources = load_austrian_sources(Path(__file__).resolve().parents[1] / "config" / "austrian-sources.json")
        prefixes = [str(source["identifierPrefix"]) for source in sources]
        self.assertEqual(len(prefixes), len(set(prefixes)))
        vor = next(source for source in sources if source["id"] == "vor")
        steiermark = next(source for source in sources if source["id"] == "steiermark")
        self.assertEqual(public_stop_id(vor, "at:49:1349:23"), "at:49:1349:23")
        self.assertEqual(public_stop_id(steiermark, "123"), "steiermark:123")

    def test_all_city_packages_are_nonempty_and_validate(self) -> None:
        cities = [
            {"id": city, "name": city, "latitude": 48.0, "longitude": 16.0, "radiusMeters": 100_000, "packageMode": "austrian"}
            for city in ("wien", "graz", "linz", "salzburg", "innsbruck", "klagenfurt", "st-poelten", "bregenz")
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archives = {}
            for source_id in ("vor", "steiermark", "salzburg", "kaernten", "ooevv", "tirol", "vorarlberg", "linz-ag"):
                path = root / f"{source_id}.zip"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n1,Test,48.0,16.0\n")
                archives[source_id] = zipfile.ZipFile(path)
            output = root / "output"
            registry = load_austrian_sources(Path(__file__).resolve().parents[1] / "config" / "austrian-sources.json")
            build_austrian_stop_packages_from_sources(archives, cities, output, registry)
            # Validation uses the production registry and therefore checks all package files.
            counts = validate(output, Path(__file__).resolve().parents[1] / "config" / "austrian-sources.json")
            self.assertEqual(set(counts), {city["id"] for city in cities})
            self.assertTrue(all(count > 0 for count in counts.values()))
            for archive in archives.values():
                archive.close()


if __name__ == "__main__":
    unittest.main()
