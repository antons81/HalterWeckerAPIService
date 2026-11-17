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
from datetime import date
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

    def test_expired_non_vor_cache_retains_existing_outage_policy(self) -> None:
        self.sources = [source for source in self.sources if source["id"] != "vor"]
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


class VORSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="haltewecker-vor-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = GTFSArtifactCache(self.root / "cache")
        self.output = self.root / "output"
        self.source = {"id": "vor", "datasetId": 52}
        self.environment = {"MVO_API_BASE": "https://fixture.test/api"}
        self.today = date(2026, 11, 17)
        self.day_patch = mock.patch.object(downloader, "source_calendar_local_date", side_effect=lambda zone: self.today)
        self.date_mock = self.day_patch.start()
        self.addCleanup(self.day_patch.stop)
        self.requests = []
        self.feeds = {}

    @staticmethod
    def archive(start="20251214", end="20261212", *, exceptions="", weekday="1", trips=True, departures=True, revision=""):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.comment = b"fixture-padding" * 40
            archive.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n1,Fixture,48,16\n")
            archive.writestr("routes.txt", f"route_id,route_short_name,route_type\nr,U1{revision},1\n")
            archive.writestr("trips.txt", "route_id,service_id,trip_id\n" + ("r,s,t\n" if trips else ""))
            archive.writestr("stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" +
                             ("t,10:00:00,10:00:00,1,1\n" if departures else ""))
            archive.writestr("calendar.txt", "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n" +
                             f"s,{','.join([weekday] * 7)},{start},{end}\n")
            archive.writestr("calendar_dates.txt", "service_id,date,exception_type\n" + exceptions)
        return buffer.getvalue()

    @staticmethod
    def catalog(year=2027, *, created="2026-11-17T04:30:48Z", version_id="29225", size=None):
        return [{"id": "52", "activeVersions": [{"year": str(year), "dataSetVersion": {
            "id": version_id, "created": created,
            "file": {"originalName": f"fixture-{year}.zip", "size": size},
        }}]}]

    def request(self, request, **kwargs):
        year = int(request.full_url.split("/")[-2])
        self.requests.append((year, request.get_method()))
        payload = self.feeds.get(year)
        if isinstance(payload, Exception):
            raise payload
        if payload is None:
            raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, None)
        response = io.BytesIO(payload)
        response.status = 200
        response.headers = {}
        return response

    def download(self, catalog=None):
        with mock.patch.object(urllib.request, "urlopen", side_effect=self.request):
            return downloader.download_source(self.source, self.catalog() if catalog is None else catalog,
                                              "fixture-token", self.output, self.environment, self.cache)

    def write_archive(self, data):
        path = self.root / "fixture.zip"
        path.write_bytes(data)
        return path

    def test_current_year_missing_from_catalog_is_requested_and_validated(self):
        self.feeds[2026] = self.archive()
        result = self.download()
        self.assertEqual(result["year"], 2026)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(self.requests, [(2026, "GET")])
        self.date_mock.assert_called_with("Europe/Vienna")
        validate_artifact_entry("vor", result, base_dir=self.root)

    def test_future_feed_cannot_replace_cache_or_publish_alias(self):
        self.feeds[2026] = self.archive()
        result = self.download()
        state_before = self.cache.state("vor")
        contents_before = Path(result["immutablePath"]).read_bytes()
        # Make the existing cache ineligible without changing the raw archive.
        self.today = date(2026, 12, 13)
        self.feeds[2026] = self.archive("20271212", "20281210")
        self.feeds[2027] = self.feeds[2026]
        with self.assertRaises(downloader.NoCurrentVORService):
            self.download()
        self.assertEqual(self.cache.state("vor"), state_before)
        self.assertEqual(Path(result["immutablePath"]).read_bytes(), contents_before)
        self.assertTrue(Path(result["path"]).samefile(Path(result["immutablePath"])))
        self.assertFalse((self.output / "vor-2027.zip").exists())
        self.assertFalse(list(self.cache.root.rglob(".download-*")))

    def test_future_feed_without_local_archive_fails_closed(self):
        self.feeds[2027] = self.archive("20261213", "20271211")
        with self.assertRaises(downloader.NoCurrentVORService):
            self.download()
        self.assertIsNone(self.cache.state("vor"))
        self.assertFalse((self.cache.root / "vor" / "current.zip").exists())
        self.assertFalse(self.output.exists())

    def test_december_timetable_transition_uses_actual_service_day(self):
        self.feeds = {2026: self.archive(), 2027: self.archive("20261213", "20271211")}
        self.today = date(2026, 12, 12)
        self.assertEqual(self.download()["year"], 2026)
        self.today = date(2026, 12, 13)
        result = self.download()
        self.assertEqual(result["year"], 2027)
        self.assertEqual(self.cache.state("vor")["sourceVersion"]["year"], 2027)
        self.assertIn((2027, "GET"), self.requests)

    def test_next_year_feed_keeps_refreshing_after_december_transition(self):
        self.today = date(2026, 12, 13)
        self.feeds = {2026: self.archive(), 2027: self.archive("20261213", "20271211")}
        first = self.download()
        self.feeds[2027] = self.archive("20261213", "20271211", revision="2")
        updated = self.download(self.catalog(created="2026-12-14T04:30:48Z"))
        self.assertEqual(updated["year"], 2027)
        self.assertEqual(updated["status"], "updated")
        self.assertNotEqual(first["sha256"], updated["sha256"])

    def test_nested_metadata_change_forces_get_within_same_year(self):
        first = self.archive()
        self.feeds[2026] = first
        self.download(self.catalog(2026, size=str(len(first))))
        self.feeds[2026] = self.archive(revision="2")
        result = self.download(self.catalog(2026, created="2026-11-18T04:30:48Z", version_id="29226",
                                            size=str(len(self.feeds[2026]))))
        self.assertEqual(result["status"], "updated")
        version = self.cache.state("vor")["sourceVersion"]
        self.assertEqual(version["versionId"], "29226")
        self.assertEqual(version["created"], "2026-11-18T04:30:48Z")
        self.assertEqual(version["size"], str(len(self.feeds[2026])))
        self.assertEqual(result["originalName"], "fixture-2026.zip")
        self.assertEqual(self.requests, [(2026, "GET"), (2026, "GET")])

    def test_file_metadata_change_is_detected_even_when_version_timestamp_is_unchanged(self):
        self.feeds[2026] = self.archive()
        first = self.download(self.catalog(2026, size=str(len(self.feeds[2026]))))
        self.feeds[2026] = self.archive(revision="updated-file")
        second = self.download(self.catalog(2026, size=str(len(self.feeds[2026]))))
        self.assertEqual(second["status"], "updated")
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(self.requests, [(2026, "GET"), (2026, "GET")])

    def test_missing_metadata_never_freezes_yearly_cache(self):
        self.feeds[2026] = self.archive()
        first = self.download([])
        self.feeds[2026] = self.archive(revision="2")
        second = self.download([])
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(len(self.requests), 2)

    def test_latest_versions_metadata_is_used_without_active_versions(self):
        self.feeds[2026] = self.archive()
        catalog = self.catalog(2026)
        catalog[0]["latestVersions"] = catalog[0].pop("activeVersions")
        self.download(catalog)
        self.assertEqual(self.cache.state("vor")["sourceVersion"]["versionId"], "29225")

    def test_invalid_update_preserves_current_valid_cache_and_manifest_provenance(self):
        self.feeds[2026] = self.archive()
        first = self.download([])
        before = self.cache.state("vor")
        self.feeds[2026] = self.archive("20261213", "20271211")
        result = self.download([])
        self.assertEqual(result["status"], "preserved-stale")
        self.assertEqual(result["sha256"], first["sha256"])
        self.assertEqual(self.cache.state("vor"), before)
        self.assertTrue(Path(first["immutablePath"]).exists())
        validate_artifact_entry("vor", result, base_dir=self.root)

    def test_calendar_exceptions_and_referenced_departures(self):
        cases = [
            ({}, True),
            ({"weekday": "0"}, False),
            ({"exceptions": "s,20261117,2\n"}, False),
            ({"weekday": "0", "exceptions": "s,20261117,1\n"}, True),
            ({"exceptions": "s,20261118,2\n"}, True),
            ({"trips": False}, False),
            ({"departures": False}, False),
            ({"start": "20261213", "end": "20271211"}, False),
        ]
        for arguments, valid in cases:
            with self.subTest(arguments=arguments):
                path = self.write_archive(self.archive(**arguments))
                if valid:
                    downloader.require_vor_service(path, self.today)
                else:
                    with self.assertRaises(downloader.NoCurrentVORService):
                        downloader.require_vor_service(path, self.today)
        path = self.write_archive(self.archive(weekday="0", exceptions="s,20261117,1\n"))
        with zipfile.ZipFile(path, "r") as archive:
            entries = {name: archive.read(name) for name in archive.namelist() if name != "calendar.txt"}
        with zipfile.ZipFile(path, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        downloader.require_vor_service(path, self.today)

    def test_upstream_outage_preserves_valid_cache_and_provenance(self):
        self.feeds[2026] = self.archive()
        result = self.download([])
        before = self.cache.state("vor")
        self.feeds[2026] = urllib.error.HTTPError("https://fixture.test/file", 502, "unavailable", {}, None)
        fallback = self.download([])
        self.assertEqual(fallback["status"], "preserved-stale")
        self.assertEqual(fallback["sha256"], result["sha256"])
        self.assertEqual(self.cache.state("vor"), before)
        self.assertEqual(fallback["year"], 2026)

    def test_outage_rejects_ineligible_vor_before_publishing_any_alias(self):
        path = self.write_archive(self.archive("20261213", "20271211"))
        self.cache.resolve("vor", str(path), source_version={"datasetId": 52, "year": 2027})
        before = self.cache.state("vor")
        failure = urllib.error.HTTPError("https://fixture.test/catalog", 502, "unavailable", {}, None)
        with self.assertRaisesRegex(RuntimeError, "VOR has no scheduled departures"):
            downloader.preserve_cached_sources([self.source], self.cache, self.output, failure)
        self.assertEqual(self.cache.state("vor"), before)
        self.assertFalse(self.output.exists())


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
