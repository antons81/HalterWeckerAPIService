import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_stop_packages import main, parse_build_stop_packages_args  # noqa: E402


def _write_sweden_fixture(root: Path) -> Path:
    archive_path = root / "sweden.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "9022001000001001,T-Centralen,59.331,18.058\n"
            "9022001000002002,Slussen,59.320,18.072\n",
        )
        archive.writestr(
            "routes.txt",
            "route_id,route_short_name,route_long_name,route_type\nR1,17,,0\n",
        )
        archive.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,trip_headsign,direction_id\n"
            "R1,S1,T1,Towards Slussen,0\n",
        )
        archive.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,08:00:00,08:00:00,9022001000001001,1\n"
            "T1,08:10:00,08:10:00,9022001000002002,2\n",
        )
        archive.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
            "start_date,end_date\n"
            "S1,1,1,1,1,1,1,1,20200101,20301231\n",
        )
    return archive_path


def _write_sweden_source_config(root: Path) -> Path:
    source_cities = json.loads(
        (
            Path(__file__).resolve().parents[1] / "config" / "sweden-cities.json"
        ).read_text(encoding="utf-8")
    )
    cities_path = root / "sweden-cities.json"
    cities_path.write_text(json.dumps([source_cities[0]]), encoding="utf-8")
    source_config = root / "external-gtfs-sources.json"
    source_config.write_text(
        json.dumps(
            [
                {
                    "id": "sweden",
                    "cities": str(cities_path),
                    "timezone": "Europe/Stockholm",
                    "identifierPrefix": "se:",
                    "stopIDMode": "exact",
                    "buildStops": True,
                    "buildRoutes": True,
                    "buildDepartures": True,
                    "country": "SE",
                }
            ]
        ),
        encoding="utf-8",
    )
    return source_config


class SkipGermanBuildTests(unittest.TestCase):
    def test_gtfs_url_required_without_skip_german(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            parse_build_stop_packages_args([
                "--external-gtfs-url",
                "sweden=/tmp/sweden.zip",
            ])
        self.assertNotEqual(raised.exception.code, 0)

    def test_skip_german_requires_another_source(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            parse_build_stop_packages_args(["--skip-german"])
        self.assertNotEqual(raised.exception.code, 0)

    def test_skip_german_with_external_provider_succeeds_without_wuppertal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = _write_sweden_fixture(root)
            source_config = _write_sweden_source_config(root)
            output = root / "out"
            repo = Path(__file__).resolve().parents[1]

            # German GTFS must not be touched.
            def fail_german_load(url: str, headers=None):
                raise AssertionError(f"German/other unexpected archive load: {url}")

            # Only allow the Sweden fixture path through load_gtfs_archive.
            from build_stop_packages import load_gtfs_archive as real_load

            def selective_load(url: str, headers=None):
                if Path(url).resolve() == archive_path.resolve():
                    return real_load(url, headers=headers)
                raise AssertionError(f"Unexpected GTFS load while --skip-german: {url}")

            with mock.patch(
                "build_stop_packages.load_gtfs_archive",
                side_effect=selective_load,
            ):
                main([
                    "--skip-german",
                    "--cities",
                    str(repo / "config" / "cities.json"),
                    "--external-gtfs-sources",
                    str(source_config),
                    "--external-gtfs-url",
                    f"sweden={archive_path}",
                    "--output",
                    str(output),
                ])

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            ids = [city["id"] for city in manifest["cities"]]
            self.assertEqual(ids, ["stockholm"])
            self.assertNotIn("wuppertal", ids)
            self.assertTrue((output / "stops" / "stockholm.json").exists())
            self.assertTrue((output / "departures" / "stockholm.json").exists())
            radar = json.loads(
                (output / "transit-radar-cities.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [city["appCityID"] for city in radar["cities"]],
                ["stockholm"],
            )

    def test_skip_german_does_not_resolve_configured_german_cities(self) -> None:
        """Regression: Sweden-only must never search German city radii."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = _write_sweden_fixture(root)
            source_config = _write_sweden_source_config(root)
            output = root / "out"
            repo = Path(__file__).resolve().parents[1]

            calls: list[str] = []

            from build_stop_packages import (
                build_stop_packages as real_build_stop_packages,
                german_branch_cities as real_german_branch_cities,
                load_gtfs_archive as real_load,
            )

            def tracking_german_branch(cities):
                selected = real_german_branch_cities(cities)
                calls.append(
                    "german_branch:" + ",".join(str(city["id"]) for city in selected)
                )
                return selected

            def tracking_build_stop_packages(*args, **kwargs):
                calls.append("build_stop_packages")
                return real_build_stop_packages(*args, **kwargs)

            def selective_load(url: str, headers=None):
                if Path(url).resolve() == archive_path.resolve():
                    return real_load(url, headers=headers)
                raise AssertionError(f"Unexpected GTFS load: {url}")

            with mock.patch(
                "build_stop_packages.german_branch_cities",
                side_effect=tracking_german_branch,
            ), mock.patch(
                "build_stop_packages.build_stop_packages",
                side_effect=tracking_build_stop_packages,
            ), mock.patch(
                "build_stop_packages.load_gtfs_archive",
                side_effect=selective_load,
            ):
                main([
                    "--skip-german",
                    "--cities",
                    str(repo / "config" / "cities.json"),
                    "--external-gtfs-sources",
                    str(source_config),
                    "--external-gtfs-url",
                    f"sweden={archive_path}",
                    "--output",
                    str(output),
                ])

            self.assertEqual(calls, [])
            self.assertFalse((output / "stops" / "wuppertal.json").exists())


if __name__ == "__main__":
    unittest.main()
