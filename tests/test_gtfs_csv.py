import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_stop_packages import iter_table  # noqa: E402
from gtfs_csv import GTFSHeaderError, normalized_dict_reader  # noqa: E402


class GTFSCSVHeaderTests(unittest.TestCase):
    def test_normal_header_is_unchanged(self) -> None:
        reader = normalized_dict_reader(
            io.StringIO("stop_id,stop_lat,stop_lon\nS1,-27.4,153.0\n")
        )

        self.assertEqual(reader.fieldnames, ["stop_id", "stop_lat", "stop_lon"])
        self.assertEqual(
            next(reader),
            {"stop_id": "S1", "stop_lat": "-27.4", "stop_lon": "153.0"},
        )

    def test_leading_and_trailing_header_spaces_are_stripped(self) -> None:
        reader = normalized_dict_reader(
            io.StringIO(" stop_id , stop_lat, stop_lon \nS1,-27.4,153.0\n")
        )

        self.assertEqual(reader.fieldnames, ["stop_id", "stop_lat", "stop_lon"])
        self.assertEqual(next(reader)["stop_lat"], "-27.4")

    def test_mixed_ascii_header_whitespace_is_stripped_without_trimming_values(self) -> None:
        reader = normalized_dict_reader(
            io.StringIO("\t stop_id\t,\tstop_lat , stop_lon\n S1 ,-27.4 ,153.0 \n")
        )

        row = next(reader)
        self.assertEqual(reader.fieldnames, ["stop_id", "stop_lat", "stop_lon"])
        self.assertEqual(row["stop_id"], " S1 ")
        self.assertEqual(row["stop_lat"], "-27.4 ")

    def test_duplicate_headers_after_normalization_fail_closed(self) -> None:
        with self.assertRaisesRegex(GTFSHeaderError, "stop_id"):
            normalized_dict_reader(io.StringIO("stop_id, stop_id\nS1,S2\n"))

    def test_transperth_style_header_works_through_stop_package_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "transperth.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "stops.txt",
                    "location_type, parent_station, stop_id, stop_lat, stop_lon\n"
                    "0,,10000,-32.1479,116.0202\n",
                )

            with zipfile.ZipFile(archive_path) as archive:
                rows = list(iter_table(archive, "stops.txt"))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["stop_id"], "10000")
            self.assertEqual(rows[0]["stop_lat"], "-32.1479")
            self.assertEqual(rows[0]["stop_lon"], "116.0202")


if __name__ == "__main__":
    unittest.main()
