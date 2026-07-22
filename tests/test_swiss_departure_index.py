#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_swiss_departure_index import transport_type


class SwissDepartureIndexTests(unittest.TestCase):
    def test_maps_standard_and_extended_swiss_route_types(self) -> None:
        self.assertEqual(transport_type("0"), "tram")
        self.assertEqual(transport_type("109"), "train")
        self.assertEqual(transport_type("700"), "bus")
        self.assertEqual(transport_type("900"), "tram")

    def test_rejects_unknown_route_type(self) -> None:
        self.assertIsNone(transport_type("not-a-route-type"))
        self.assertIsNone(transport_type("1700"))


if __name__ == "__main__":
    unittest.main()
