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


if __name__ == "__main__":
    unittest.main()
