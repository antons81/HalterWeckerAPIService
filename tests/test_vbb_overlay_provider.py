import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "services"))

from static_departures_runtime import HybridStaticDeparturesBackend, RuntimeUnavailable
from vbb_overlay_provider import VBBOverlayProviderAdapter, VBBOverlayUnavailable


class _Snapshot:
    def __init__(self, root: Path):
        self.release_id = "vbb-test-release"
        self.stop_data_root = root
        self.catalog = object()


class _Lease:
    def __init__(self, snapshot: _Snapshot):
        self.snapshot = snapshot
        self.release_id = snapshot.release_id

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return None


class _Manager:
    def __init__(self, snapshot: _Snapshot):
        self.snapshot = snapshot
        self.closed = False

    def acquire_snapshot(self):
        return _Lease(self.snapshot)

    def close(self):
        self.closed = True


class _Legacy:
    def __init__(self):
        self.calls = 0

    def _called(self):
        self.calls += 1
        raise AssertionError("VBB Berlin must not call legacy runtime")

    def city_has_stop(self, *_args, **_kwargs):
        return self._called()

    def city_departure_mode(self, *_args, **_kwargs):
        return self._called()

    def city_departure_prefixes(self, *_args, **_kwargs):
        return self._called()

    def lines(self, *_args, **_kwargs):
        return self._called()

    def external_departures_for(self, *_args, **_kwargs):
        return self._called()

    def board(self, *_args, **_kwargs):
        return self._called()

    def trip_details(self, *_args, **_kwargs):
        return self._called()

    def close(self):
        return None


class VBBOverlayProviderTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[datetime, str, str]:
        zone = ZoneInfo("Europe/Berlin")
        anchor = datetime.now(zone).replace(minute=0, second=0, microsecond=0)
        service_date = anchor.date().isoformat()
        filename = anchor.strftime("%Y%m%d-%H.json")
        (root / "stops").mkdir(parents=True)
        (root / "transit" / "city-lines").mkdir(parents=True)
        (root / "transit" / "vbb" / "berlin").mkdir(parents=True)
        (root / "stops" / "berlin.json").write_text(json.dumps([
            {
                "id": "public-stop",
                "name": "Alexanderplatz",
                "latitude": 52.5219,
                "longitude": 13.4132,
            }
        ]), encoding="utf-8")
        (root / "transit" / "city-lines" / "berlin.json").write_text(json.dumps({
            "version": 1,
            "cityID": "berlin",
            "lines": [{"routeID": "route-1", "agencyID": "VBB", "names": ["M1"], "routeType": 3}],
        }), encoding="utf-8")
        payload = {
            "version": 10,
            "stops": [
                {"id": "native-stop", "name": "Alexanderplatz", "latitude": 52.5219, "longitude": 13.4132},
                {"id": "native-terminal", "name": "Ziel", "latitude": 52.53, "longitude": 13.42},
            ],
            "trips": [{
                "id": "trip-1",
                "routeID": "route-1",
                "lineName": "M1",
                "routeType": 3,
                "directionName": "Ziel",
                "serviceDate": anchor.strftime("%Y%m%d"),
                "stopTimes": [
                    {"stopID": "native-stop", "stopSequence": 0, "arrivalSeconds": anchor.hour * 3600 + 900, "departureSeconds": anchor.hour * 3600 + 900},
                    {"stopID": "native-terminal", "stopSequence": 1, "arrivalSeconds": anchor.hour * 3600 + 1200, "departureSeconds": anchor.hour * 3600 + 1200},
                ],
            }],
        }
        (root / "transit" / "vbb" / "berlin" / filename).write_text(json.dumps(payload), encoding="utf-8")
        return anchor, service_date, filename

    def _family_fixture(self, root: Path, anchor: datetime, filename: str) -> None:
        public_path = root / "stops" / "berlin.json"
        public_stops = json.loads(public_path.read_text(encoding="utf-8"))
        public_stops.extend([
            {"id": "193456", "name": "U Hallesches Tor", "latitude": 52.497772, "longitude": 13.39176},
            {"id": "462651", "name": "U Hausvogteiplatz", "latitude": 52.5135, "longitude": 13.3956},
            {"id": "476861", "name": "Zionskirchplatz", "latitude": 52.5325, "longitude": 13.412},
        ])
        public_path.write_text(json.dumps(public_stops), encoding="utf-8")

        routes = [
            ("route-u3", "U3", 400), ("route-u6", "U6", 400),
            ("route-m41", "M41", 700), ("route-248", "248", 700),
            ("route-u2", "U2", 400), ("route-m1", "M1", 700),
            ("route-tram", "M10", 900),
        ]
        line_path = root / "transit" / "city-lines" / "berlin.json"
        line_data = json.loads(line_path.read_text(encoding="utf-8"))
        line_data["lines"].extend({
            "routeID": route_id, "agencyID": "VBB", "names": [name], "routeType": route_type
        } for route_id, name, route_type in routes)
        line_path.write_text(json.dumps(line_data), encoding="utf-8")

        overlay_path = root / "transit" / "vbb" / "berlin" / filename
        payload = json.loads(overlay_path.read_text(encoding="utf-8"))
        stop_ids = {
            "halles-1": "de:11000:900012103::1",
            "halles-3": "de:11000:900012103::3",
            "halles-5": "de:11000:900012103::5",
            "halles-7": "de:11000:900012103::7",
            "halles-8": "de:11000:900012103::8",
            "haus-1": "de:11000:900100012::1",
            "haus-2": "de:11000:900100012::2",
            "zions-regular": "de:11000:900100042::1",
            "zions-replacement": "de:11000:900110505::7",
        }
        native_names = {
            "halles-1": "U Hallesches Tor", "halles-3": "U Hallesches Tor",
            "halles-5": "U Hallesches Tor", "halles-7": "U Hallesches Tor",
            "halles-8": "U Hallesches Tor", "haus-1": "U Hausvogteiplatz",
            "haus-2": "U Hausvogteiplatz", "zions-regular": "Zionskirchplatz",
            "zions-replacement": "Zionskirchplatz Ersatzhalt",
        }
        coordinates = {
            "halles": (52.4978, 13.3918),
            "haus": (52.5135, 13.3956),
            "zions": (52.5325, 13.4120),
        }
        payload["stops"].extend({
            "id": stop_id,
            "name": native_names[key],
            "latitude": coordinates[key.split("-")[0]][0],
            "longitude": coordinates[key.split("-")[0]][1],
        } for key, stop_id in stop_ids.items())
        seconds = anchor.hour * 3600 + 900
        trip_specs = [
            ("trip-u3", "route-u3", "U3", 400, "halles-1"),
            ("trip-u6", "route-u6", "U6", 400, "halles-3"),
            ("trip-m41", "route-m41", "M41", 700, "halles-5"),
            ("trip-248-7", "route-248", "248", 700, "halles-7"),
            ("trip-248-8", "route-248", "248", 700, "halles-8"),
            ("trip-u2-1", "route-u2", "U2", 400, "haus-1"),
            ("trip-u2-2", "route-u2", "U2", 400, "haus-2"),
            ("trip-m1-regular", "route-m1", "M1", 700, "zions-regular"),
            ("trip-m1-replacement", "route-m1", "M1", 700, "zions-replacement"),
            ("trip-m10", "route-tram", "M10", 900, "halles-5"),
        ]
        payload["trips"].extend({
            "id": trip_id,
            "routeID": route_id,
            "lineName": line,
            "routeType": route_type,
            "directionName": "Test destination",
            "serviceDate": anchor.strftime("%Y%m%d"),
            "stopTimes": [{"stopID": stop_ids[stop_key], "stopSequence": 1,
                           "arrivalSeconds": seconds, "departureSeconds": seconds}],
        } for trip_id, route_id, line, route_type, stop_key in trip_specs)
        overlay_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_adapter_serves_city_lines_board_and_trip_without_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor, service_date, _filename = self._fixture(root)
            adapter = VBBOverlayProviderAdapter(root, now_provider=lambda: anchor)

            self.assertEqual(adapter.resolve_city("berlin-de"), "berlin")
            self.assertTrue(adapter.city_has_stop("berlin", "public-stop"))
            self.assertEqual(adapter.city_departure_mode()[0], "vbb-overlay")
            self.assertEqual(adapter.city_departure_prefixes(), ((), ()))
            self.assertEqual(adapter.lines("berlin", "public-stop")[0]["providerID"], "vbb-berlin")
            board = adapter.board("berlin", "public-stop", 10, anchor, anchor + timedelta(hours=1))
            self.assertEqual(board[0]["source"], "vbb-overlay")
            self.assertEqual(board[0]["tripID"], "trip-1")
            trip = adapter.trip_details("berlin", "trip-1", str(root), service_date)
            self.assertEqual(trip["providerID"], "vbb-berlin")
            self.assertEqual(len(trip["stops"]), 2)

            legacy = _Legacy()
            backend = HybridStaticDeparturesBackend(
                legacy,
                _Manager(_Snapshot(root)),
                ("norway",),
            )
            self.assertEqual(backend.resolve_city("berlin"), "berlin")
            self.assertTrue(backend.city_has_stop("berlin", "public-stop"))
            self.assertEqual(backend.lines("berlin", "public-stop")[0]["providerID"], "vbb-berlin")
            self.assertTrue(backend.board("berlin", "public-stop", 10, anchor, anchor + timedelta(hours=1)))
            self.assertEqual(backend.trip_details("berlin", "trip-1", str(root), service_date)["providerID"], "vbb-berlin")
            self.assertEqual(legacy.calls, 0)
            backend.close()

    def test_stale_overlay_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _anchor, _service_date, _filename = self._fixture(root)
            stale = datetime.now(ZoneInfo("Europe/Berlin")) + timedelta(days=7)
            adapter = VBBOverlayProviderAdapter(root, now_provider=lambda: stale)
            with self.assertRaises(VBBOverlayUnavailable):
                adapter.board("berlin", "public-stop", 10)

    def test_unknown_city_and_stop_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor, _service_date, _filename = self._fixture(root)
            adapter = VBBOverlayProviderAdapter(root, now_provider=lambda: anchor)
            self.assertFalse(adapter.city_has_stop("hamburg", "public-stop"))
            self.assertFalse(adapter.city_has_stop("berlin", "unknown-stop"))
            with self.assertRaises(VBBOverlayUnavailable):
                adapter.lines("berlin", "unknown-stop")

    def test_hallesches_public_and_native_children_aggregate_only_station_family(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor, _service_date, filename = self._fixture(root)
            self._family_fixture(root, anchor, filename)
            adapter = VBBOverlayProviderAdapter(root, now_provider=lambda: anchor)
            end = anchor + timedelta(hours=2)

            public = adapter.board("berlin", "193456", 100, anchor, end)
            native_7 = adapter.board("berlin", "de:11000:900012103::7", 100, anchor, end)
            native_8 = adapter.board("berlin", "de:11000:900012103::8", 100, anchor, end)
            public_trips = {item["tripID"] for item in public}

            self.assertEqual(public_trips, {"trip-u3", "trip-u6", "trip-m41", "trip-248-7", "trip-248-8", "trip-m10"})
            self.assertEqual({item["tripID"] for item in native_7}, public_trips)
            self.assertEqual({item["tripID"] for item in native_8}, public_trips)
            self.assertEqual({item["line"] for item in public}, {"U3", "U6", "M41", "248", "M10"})
            self.assertNotIn("trip-u2-1", public_trips)
            self.assertNotIn("trip-m1-replacement", public_trips)

    def test_hausvogteiplatz_native_child_still_resolves_its_own_family(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor, _service_date, filename = self._fixture(root)
            self._family_fixture(root, anchor, filename)
            adapter = VBBOverlayProviderAdapter(root, now_provider=lambda: anchor)
            public = adapter.board("berlin", "462651", 20, anchor, anchor + timedelta(hours=2))
            native = adapter.board("berlin", "de:11000:900100012::1", 20, anchor, anchor + timedelta(hours=2))
            self.assertEqual({item["tripID"] for item in public}, {"trip-u2-1", "trip-u2-2"})
            self.assertEqual({item["tripID"] for item in native}, {item["tripID"] for item in public})

    def test_zionskirchplatz_uses_only_explicit_replacement_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor, _service_date, filename = self._fixture(root)
            self._family_fixture(root, anchor, filename)
            adapter = VBBOverlayProviderAdapter(root, now_provider=lambda: anchor)
            board = adapter.board("berlin", "476861", 20, anchor, anchor + timedelta(hours=2))
            self.assertEqual({item["tripID"] for item in board}, {"trip-m1-replacement"})
            self.assertTrue(all(item["line"] == "M1" for item in board))
            self.assertTrue(all(item["platformStopID"] == "de:11000:900110505::7" for item in board))
            self.assertFalse(adapter.city_has_stop("berlin", "unmapped-ersatzhalt"))

    def test_extended_vbb_route_types_map_to_transport_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor, _service_date, filename = self._fixture(root)
            self._family_fixture(root, anchor, filename)
            adapter = VBBOverlayProviderAdapter(root, now_provider=lambda: anchor)
            cases = [
                ("de:11000:900012103::1", "trip-u3", "subway"),
                ("de:11000:900012103::7", "trip-248-7", "bus"),
                ("de:11000:900012103::5", "trip-m10", "tram"),
            ]
            for stop_id, trip_id, expected_mode in cases:
                board = adapter.board("berlin", stop_id, 20, anchor, anchor + timedelta(hours=2))
                item = next(value for value in board if value["tripID"] == trip_id)
                self.assertEqual(item["transportMode"], expected_mode)
                trip = adapter.trip_details("berlin", trip_id, str(root), anchor.strftime("%Y-%m-%d"))
                self.assertEqual(trip["transportMode"], expected_mode)


if __name__ == "__main__":
    unittest.main()
