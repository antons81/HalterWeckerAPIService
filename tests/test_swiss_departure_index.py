#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_swiss_departure_index import stop_time_targets, transport_type


class SwissDepartureIndexTests(unittest.TestCase):
    def test_maps_standard_and_extended_swiss_route_types(self) -> None:
        self.assertEqual(transport_type("0"), "tram")
        self.assertEqual(transport_type("109"), "train")
        self.assertEqual(transport_type("700"), "bus")
        self.assertEqual(transport_type("900"), "tram")

    def test_rejects_unknown_route_type(self) -> None:
        self.assertIsNone(transport_type("not-a-route-type"))
        self.assertIsNone(transport_type("1700"))

    def test_base_stop_consumes_station_group_stop_times(self) -> None:
        stops = {
            "ch:1:sloid:7785": {
                "parent_station": "Parentch:1:sloid:7785",
                "platform_code": "",
            },
            "ch:1:sloid:7785:0:75435": {
                "parent_station": "Parentch:1:sloid:7785",
                "platform_code": "3",
            },
        }

        targets = stop_time_targets(stops, stops)

        self.assertEqual(targets["ch:1:sloid:7785"], ["ch:1:sloid:7785"])
        self.assertEqual(
            targets["ch:1:sloid:7785:0:75435"],
            ["ch:1:sloid:7785", "ch:1:sloid:7785:0:75435"],
        )

    def test_platform_stop_does_not_consume_sibling_stop_times(self) -> None:
        stops = {
            "ch:1:sloid:7785:0:75435": {
                "parent_station": "Parentch:1:sloid:7785",
                "platform_code": "3",
            },
            "ch:1:sloid:7785:0:530727": {
                "parent_station": "Parentch:1:sloid:7785",
                "platform_code": "4",
            },
        }

        targets = stop_time_targets(stops, stops)

        self.assertEqual(targets["ch:1:sloid:7785:0:75435"], ["ch:1:sloid:7785:0:75435"])
        self.assertEqual(targets["ch:1:sloid:7785:0:530727"], ["ch:1:sloid:7785:0:530727"])


if __name__ == "__main__":
    unittest.main()
