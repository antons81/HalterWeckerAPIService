import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from austrian_artifacts import resolve_austrian_artifacts
from build_stop_packages import parse_build_stop_packages_args
from build_german_departure_index import connect
import download_austrian_gtfs as downloader
import build_stop_packages as builder
import import_static_departures_database as importer
import static_departures_scoped as scoped


class AustrianArtifactSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = {"id": "kaernten", "datasetId": 55, "identifierPrefix": "kaernten:", "cities": ["klagenfurt"]}
        self.registry = [self.source]
        self.manifest = self.root / "run.json"

    def archive(self, year):
        path = self.root / f"kaernten-{year}.zip"
        with zipfile.ZipFile(path, "w") as feed:
            feed.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\na,First,46.6,14.3\nb,Last,46.61,14.31\n")
            feed.writestr("routes.txt", "route_id,route_short_name,route_long_name,route_type\nr,1,Fixture,3\n")
            feed.writestr("trips.txt", f"route_id,service_id,trip_id\nr,service-{year},t\n")
            feed.writestr("stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt,08:00:00,08:00:00,a,1\nt,08:10:00,08:10:00,b,2\n")
            feed.writestr("calendar_dates.txt", f"service_id,date,exception_type\nservice-{year},{year}1212,1\n")
        return path

    def entry(self, path, year):
        return {"source": "kaernten", "datasetId": 55, "year": year,
                "path": str(path), "immutablePath": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}

    def publish(self, entry, path=None):
        (path or self.manifest).write_text(json.dumps({"sources": [entry]}))

    def resolve(self):
        return resolve_austrian_artifacts(self.registry, self.root, self.manifest)["kaernten"]

    def test_current_manifest_wins_over_higher_valid_and_broken_years(self):
        current = self.archive(2026)
        self.archive(2027)
        broken = self.root / "kaernten-2028.zip"
        broken.symlink_to(self.root / "removed.zip")
        self.publish(self.entry(current, 2026))
        before = current.read_bytes()
        self.assertEqual(self.resolve(), current)
        self.assertEqual(current.read_bytes(), before)
        self.assertTrue(broken.is_symlink())

    def test_future_years_are_used_when_the_manifest_selects_them(self):
        self.archive(2026)
        for year in (2027, 2028, 2030):
            with self.subTest(year=year):
                path = self.archive(year)
                self.publish(self.entry(path, year))
                self.assertEqual(self.resolve(), path)
                with zipfile.ZipFile(self.resolve()) as feed:
                    self.assertIn(f"service-{year}".encode(), feed.read("trips.txt"))
                    self.assertIn(f"{year}1212".encode(), feed.read("calendar_dates.txt"))

    def test_immutable_path_wins_even_if_display_alias_is_retargeted(self):
        current = self.archive(2026)
        other = self.archive(2027)
        entry = self.entry(current, 2026)
        entry["path"] = str(other)
        self.publish(entry)
        self.assertEqual(self.resolve(), current)

    def test_explicit_manifest_overrides_standalone_current_manifest(self):
        current = self.archive(2026)
        other = self.archive(2027)
        self.publish(self.entry(other, 2027), self.root / "artifacts.json")
        self.publish(self.entry(current, 2026))
        self.assertEqual(self.resolve(), current)
        self.assertEqual(resolve_austrian_artifacts(self.registry, self.root)["kaernten"], other)

    def test_missing_or_malformed_explicit_manifest_never_falls_back(self):
        self.archive(2026)
        for content in (None, "{", "[]", '{"sources": []}'):
            with self.subTest(content=content):
                self.manifest.unlink(missing_ok=True)
                if content is not None:
                    self.manifest.write_text(content)
                with self.assertRaises(ValueError):
                    self.resolve()

    def test_wrong_provenance_and_missing_files_fail_with_source_diagnostic(self):
        path = self.archive(2026)
        for field, value in (("sha256", "0" * 64), ("size", 1), ("datasetId", 999),
                             ("year", True), ("year", -1), ("immutablePath", str(self.root / "missing.zip"))):
            with self.subTest(field=field):
                entry = self.entry(path, 2026)
                entry[field] = value
                self.publish(entry)
                with self.assertRaisesRegex(ValueError, "kaernten"):
                    self.resolve()

    def test_duplicate_source_and_invalid_zip_fail_closed(self):
        path = self.archive(2026)
        entry = self.entry(path, 2026)
        self.manifest.write_text(json.dumps({"sources": [entry, entry]}))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.resolve()
        path.write_bytes(b"not a GTFS archive")
        self.publish(self.entry(path, 2026))
        with self.assertRaisesRegex(ValueError, "kaernten"):
            self.resolve()

    def test_legacy_directory_skips_broken_alias_but_rejects_ambiguity(self):
        path = self.archive(2026)
        (self.root / "kaernten-2027.zip").symlink_to(self.root / "removed.zip")
        with warnings.catch_warnings(record=True) as notices:
            self.assertEqual(resolve_austrian_artifacts(self.registry, self.root)["kaernten"], path)
        self.assertEqual(len(notices), 1)
        (self.root / "kaernten-2027.zip").unlink()
        self.archive(2027)
        with self.assertRaisesRegex(ValueError, "unambiguous"):
            resolve_austrian_artifacts(self.registry, self.root)

    def test_scoped_consumer_uses_manifest_and_legacy_single_provider(self):
        current = self.archive(2026)
        self.archive(2027)
        self.publish(self.entry(current, 2026))
        provider = scoped.StaticProvider("kaernten", "AT", "austrian", self.source)
        with mock.patch.object(scoped, "load_austrian_sources", return_value=self.registry):
            self.assertEqual(scoped.resolve_austrian_archive(provider, self.root, {
                "AUSTRIAN_GTFS_DIR": str(self.root), "AUSTRIAN_GTFS_ARTIFACTS_JSON": str(self.manifest),
            }), current)
        (self.root / "kaernten-2027.zip").unlink()
        extra = self.registry + [{"id": "vor", "datasetId": 52}]
        self.assertEqual(resolve_austrian_artifacts(extra, self.root, source_ids={"kaernten"}), {"kaernten": current})

    def test_static_import_preserves_selected_service_ids_and_dates(self):
        current = self.archive(2026)
        self.archive(2027)
        self.publish(self.entry(current, 2026))
        connection = connect(self.root / "test.sqlite")
        self.addCleanup(connection.close)
        with mock.patch.object(importer, "load_austrian_sources", return_value=self.registry), \
             mock.patch.object(importer, "configured_austrian_static_city_ids", return_value={"klagenfurt"}), \
             mock.patch.object(importer, "populate_provider_city_memberships", return_value={"klagenfurt"}), \
             mock.patch.object(importer, "register_city_mode"):
            self.assertEqual(importer.import_austrian_gtfs(connection, self.root, self.root, self.root, self.manifest, self.manifest), {"klagenfurt"})
        self.assertEqual(connection.execute("SELECT service_id FROM trips").fetchall(), [("kaernten:service-2026",)])
        self.assertEqual(connection.execute("SELECT service_date FROM calendar_dates").fetchall(), [("20261212",)])

    def test_stop_package_consumer_reads_the_selected_plan(self):
        class SelectedArchiveRead(Exception):
            pass

        for year in (2026, 2027):
            with self.subTest(year=year):
                self.archive(2026)
                self.archive(2027)
                self.publish(self.entry(self.root / f"kaernten-{year}.zip", year))

                def inspect(archives, *_args):
                    try:
                        self.assertIn(f"service-{year}".encode(), archives["kaernten"].read("trips.txt"))
                    finally:
                        for archive in archives.values():
                            archive.close()
                    raise SelectedArchiveRead()

                with mock.patch.object(builder, "load_austrian_sources", return_value=self.registry), \
                     mock.patch.object(builder, "load_cities", return_value=[]), \
                     mock.patch.object(builder, "build_austrian_stop_packages_from_sources", side_effect=inspect):
                    with self.assertRaises(SelectedArchiveRead):
                        builder.main(["--skip-german", "--austrian-gtfs-dir", str(self.root),
                                      "--austrian-artifacts", str(self.manifest), "--output", str(self.root / "output")])

    def test_stop_builder_accepts_current_run_manifest_without_network(self):
        args = parse_build_stop_packages_args(["--skip-german", "--austrian-gtfs-dir", str(self.root),
                                              "--austrian-artifacts", str(self.manifest)])
        self.assertEqual(args.austrian_artifacts, str(self.manifest))

    def test_downloader_publishes_atomic_standalone_and_release_manifests(self):
        path = self.archive(2027)
        entry = self.entry(path, 2027)
        env = dict(MVO_USERNAME="fixture", MVO_PASSWORD="fixture", MVO_TOKEN_URL="fixture", MVO_API_BASE="fixture")
        argv = ["download_austrian_gtfs.py", "--output", str(self.root), "--output-json", str(self.manifest)]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(downloader, "read_env", return_value=env), \
             mock.patch.object(downloader, "refresh_sources", return_value=[entry]):
            downloader.main()
        self.assertEqual(json.loads((self.root / "artifacts.json").read_text()), json.loads(self.manifest.read_text()))
        with mock.patch.object(sys, "argv", argv), mock.patch.object(downloader, "read_env", return_value=env), \
             mock.patch.object(downloader, "refresh_sources", return_value=[entry]), \
             mock.patch.object(downloader.os, "replace", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                downloader.main()
        self.assertEqual(list(self.root.glob(".*.next")), [])
        self.assertEqual(json.loads((self.root / "artifacts.json").read_text()), {"sources": [entry]})
