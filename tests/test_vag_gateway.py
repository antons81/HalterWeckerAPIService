#!/usr/bin/env python3

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.vag_gateway import compute_vehicle, interpolated_position


class VAGGatewayTests(unittest.TestCase):
    def test_interpolates_position_from_realtime_stop_predictions(self) -> None:
        stops = [
            {
                "Latitude": 49.0,
                "Longitude": 11.0,
                "AbfahrtszeitSoll": "2026-07-16T10:00:00+02:00",
                "AbfahrtszeitIst": "2026-07-16T10:02:00+02:00",
            },
            {
                "Latitude": 50.0,
                "Longitude": 12.0,
                "AnkunftszeitSoll": "2026-07-16T10:08:00+02:00",
                "AnkunftszeitIst": "2026-07-16T10:12:00+02:00",
            },
        ]

        position = interpolated_position(
            stops,
            datetime.fromisoformat("2026-07-16T10:07:00+02:00"),
        )

        self.assertIsNotNone(position)
        latitude, longitude, delay = position
        self.assertAlmostEqual(latitude, 49.5)
        self.assertAlmostEqual(longitude, 11.5)
        self.assertEqual(delay, 240)

    def test_vehicle_uses_stable_vehicle_number_and_marks_bus_mode(self) -> None:
        detail = {
            "Fahrtnummer": 42,
            "Fahrzeugnummer": "234",
            "Linienname": "45",
            "Richtungstext": "Ziegelstein",
            "Fahrtverlauf": [
                {
                    "Latitude": 49.45,
                    "Longitude": 11.08,
                    "AbfahrtszeitIst": "2026-07-16T10:00:00+02:00",
                }
            ],
        }

        vehicle = compute_vehicle(
            detail,
            {},
            datetime.fromisoformat("2026-07-16T10:00:00+02:00"),
        )

        self.assertEqual(vehicle["id"], "234")
        self.assertEqual(vehicle["lineName"], "45")
        self.assertEqual(vehicle["mode"], "bus")


if __name__ == "__main__":
    unittest.main()
