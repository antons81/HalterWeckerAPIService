"""Compact fallback retains non-pilot feeds and mixed provider cities.

Created by Anton on 2026-10-10.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts import import_static_departures_database as importer
from scripts.static_departures_fallback import excluded_fallback_providers

REPOSITORY = Path(__file__).resolve().parents[1]
PROVIDERS = tuple("israel-mot,ttc-surface,ttc-subway,norway,sweden,poland-warsaw,poland-wkd,511-bay-area,australia-translink-seq,australia-transport-nsw,cta-chicago,mbta-boston,stm-montreal,germany".split(","))


class CompactFallbackTests(unittest.TestCase):
    def test_existing_shards_do_not_get_imported_twice(self):
        self.assertEqual(set(excluded_fallback_providers(REPOSITORY, PROVIDERS)), set(PROVIDERS))

    def test_unknown_shard_cannot_remove_a_feed(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            excluded_fallback_providers(REPOSITORY, ("unknown",))

    def test_mixed_city_retains_complete_feed(self):
        sources = [{"id": "pilot", "importIntoStaticDepartures": True, "cities": [{"id": "mixed"}]},
                   {"id": "other", "importIntoStaticDepartures": True, "cities": [{"id": "mixed"}]}]
        with patch("scripts.static_departures_fallback.load_external_gtfs_sources", return_value=sources), \
             patch("scripts.static_departures_fallback.load_external_cities", side_effect=lambda source, _root: source["cities"]), \
             patch("scripts.static_departures_fallback.provider_capability", return_value=True):
            self.assertEqual(excluded_fallback_providers(REPOSITORY, ("pilot",)), ())

    def test_compact_import_keeps_austria_and_nonpilot_inputs_and_omits_shards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stops = root / "stop-data"
            stops.mkdir()
            (stops / "manifest.json").write_text(json.dumps({"releaseID": "candidate", "version": "2026-10-10", "cities": [{"id": "wien", "url": "wien.json"}]}))
            (stops / "wien.json").write_text('[{"id":"a"},{"id":"b"}]')
            cities = root / "cities.json"
            cities.write_text('[{"id":"wien","name":"Wien","latitude":48.2,"longitude":16.4,"radiusMeters":10000,"packageMode":"austrian","staticDepartures":true}]')
            aliases = root / "aliases.json"
            aliases.write_text('{"old-german-city":"bochum"}')
            feed = root / "austria.zip"
            with zipfile.ZipFile(feed, "w") as archive:
                archive.writestr("stops.txt", "stop_id,stop_name\na,Alpha\nb,Beta\n")
                archive.writestr("routes.txt", "route_id,route_short_name\nr,1\n")
                archive.writestr("trips.txt", "route_id,service_id,trip_id\nr,s,t\n")
                archive.writestr("stop_times.txt", "trip_id,stop_id,departure_time,arrival_time,stop_sequence\nt,a,08:00:00,08:00:00,1\nt,b,08:10:00,08:10:00,2\n")
                archive.writestr("calendar.txt", "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\ns,1,1,1,1,1,1,1,20260101,20301231\n")
            output = root / "fallback.sqlite"
            arguments = ["import", "--shard-provider-ids", ",".join(PROVIDERS), "--gtfs-url", "/missing-germany.zip", "--external-gtfs-url", "israel-mot=/missing-pilot.zip", "--austrian-gtfs", str(feed), "--cities", str(cities), "--city-id-aliases", str(aliases), "--stop-data", str(stops), "--next", str(output), "--release-id", "candidate"]
            arguments.extend(["--external-gtfs-url", "finland-hsl=/nonpilot-hsl.zip"])
            with patch.object(sys, "argv", arguments), patch.object(importer, "add_external_gtfs", return_value={"helsinki"}) as add_external:
                importer.main()
            self.assertEqual(add_external.call_args.args[2], {"finland-hsl": "/nonpilot-hsl.zip"})
            with closing(sqlite3.connect(output)) as connection:
                metadata = dict(connection.execute("SELECT key,value FROM metadata"))
                self.assertEqual(metadata["fallbackProfile"], "hybrid-only")
                self.assertEqual(set(json.loads(metadata["excludedProviderIDs"])), set(PROVIDERS))
                self.assertEqual(connection.execute("SELECT DISTINCT city_id FROM city_stops").fetchall(), [("wien",)])
                self.assertGreater(connection.execute("SELECT COUNT(*) FROM active_services").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM stop_times").fetchone()[0], 2)

    def test_compact_incremental_augmentation_is_rejected(self):
        with patch.object(sys, "argv", ["import", "--stop-data", "/missing", "--add-external", "--shard-provider-ids", "germany"]):
            with self.assertRaisesRegex(ValueError, "isolated fresh"):
                importer.main()
