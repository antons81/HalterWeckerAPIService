import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from artifact_provenance import artifact_provenance  # noqa: E402
from release_integrity import (  # noqa: E402
    validate_previous_release_city_retirements,
)


class PreviousReleaseCityRetirementTests(unittest.TestCase):
    def _write_gtfs(
        self,
        path: Path,
        stop_ids: list[str],
        stop_overrides: dict[str, tuple[str, float, float]] | None = None,
    ) -> tuple[str, int]:
        stop_overrides = stop_overrides or {}
        stop_rows = []
        for stop_id in stop_ids:
            name, latitude, longitude = stop_overrides.get(
                stop_id, (f"Stop {stop_id}", 50.0, 19.0)
            )
            stop_rows.append(f"{stop_id},{name},{latitude},{longitude}\n")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "stops.txt",
                "stop_id,stop_name,stop_lat,stop_lon\n"
                + "".join(stop_rows),
            )
            archive.writestr(
                "routes.txt",
                "route_id,route_short_name,route_type\nroute-1,1,3\n",
            )
            archive.writestr(
                "trips.txt",
                "route_id,service_id,trip_id\nroute-1,service-1,trip-1\n",
            )
            archive.writestr(
                "stop_times.txt",
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "trip-1,08:00:00,08:00:00,unrelated-stop,1\n",
            )
        return artifact_provenance(path)

    def _city(self, city_id: str, *, country: str = "DE") -> dict[str, object]:
        return {
            "id": city_id,
            "name": city_id,
            "url": f"stops/{city_id}.json",
            "country": country,
        }

    def _fixture(
        self,
        root: Path,
        *,
        candidate_stop_ids: list[str],
        candidate_status: str = "updated",
        old_cities: list[dict[str, object]] | None = None,
        service_city_ids: set[str] | None = None,
        candidate_city_ids: list[str] | None = None,
        candidate_stop_overrides: dict[str, tuple[str, float, float]] | None = None,
    ) -> tuple[
        tuple[dict[str, object], dict[str, object], dict[str, object], Path, Path],
        dict[str, object],
        list[dict[str, object]],
        Path,
        Path,
    ]:
        active_root = root / "active" / "stop-data"
        candidate_root = root / "candidate" / "stop-data"
        (active_root / "stops").mkdir(parents=True)
        (candidate_root / "stops").mkdir(parents=True)
        old_cities = old_cities or [self._city("fixture-city")]
        service_city_ids = service_city_ids or set()
        candidate_city_ids = candidate_city_ids or ["other-city"]
        for city in old_cities:
            city_id = str(city["id"])
            (active_root / str(city["url"])).write_text(
                json.dumps([{
                    "id": f"{city_id}-stop",
                    "name": f"Stop {city_id}-stop",
                    "latitude": 50.0,
                    "longitude": 19.0,
                }]),
                encoding="utf-8",
            )
            if city_id in service_city_ids:
                (active_root / "routes").mkdir(exist_ok=True)
                (active_root / "trips").mkdir(exist_ok=True)
                (active_root / "departures").mkdir(exist_ok=True)
                (active_root / "routes" / f"{city_id}.json").write_text(
                    json.dumps({"route-1": {"shortName": "1"}}), encoding="utf-8"
                )
                (active_root / "trips" / f"{city_id}.json").write_text(
                    json.dumps({"trip-1": {"routeID": "route-1"}}), encoding="utf-8"
                )
                (active_root / "departures" / f"{city_id}.json").write_text(
                    json.dumps({"stops": {f"{city_id}-stop": [{"time": "08:00"}]}}),
                    encoding="utf-8",
                )
        for city_id in candidate_city_ids:
            city = self._city(city_id)
            (candidate_root / str(city["url"])).write_text(
                json.dumps([{"id": f"{city_id}-stop", "name": city_id}]),
                encoding="utf-8",
            )

        active_gtfs = root / "active-germany.zip"
        candidate_gtfs = root / "candidate-germany.zip"
        active_digest, active_size = self._write_gtfs(active_gtfs, ["fixture-city-stop"])
        candidate_digest, candidate_size = self._write_gtfs(
            candidate_gtfs, candidate_stop_ids, candidate_stop_overrides
        )
        registry = [{
            "id": "germany",
            "cities": "config/germany-cities.json",
            "country": "DE",
        }]
        (root / "config").mkdir()
        (root / "config" / "germany-cities.json").write_text(
            json.dumps([self._city(city_id) for city_id in candidate_city_ids]),
            encoding="utf-8",
        )
        active_artifacts = {
            "sources": {
                "germany": {
                    "path": str(active_gtfs),
                    "sha256": active_digest,
                    "size": active_size,
                    "status": "unchanged",
                }
            }
        }
        candidate_artifacts = {
            "sources": {
                "germany": {
                    "path": str(candidate_gtfs),
                    "sha256": candidate_digest,
                    "size": candidate_size,
                    "status": candidate_status,
                }
            }
        }
        return (
            {"cities": old_cities},
            {"cities": [self._city(city_id) for city_id in candidate_city_ids]},
            active_artifacts,
            candidate_root,
            active_root,
        ), candidate_artifacts, registry, active_gtfs, candidate_gtfs

    def _validate(self, fixture):
        release_data, candidate_artifacts, registry, _active_gtfs, _candidate_gtfs = fixture
        old_manifest, candidate_manifest, active_artifacts, candidate_root, active_root = release_data
        return validate_previous_release_city_retirements(
            old_manifest=old_manifest,
            candidate_manifest=candidate_manifest,
            active_stop_data=active_root,
            candidate_stop_data=candidate_root,
            active_artifacts=active_artifacts,
            candidate_artifacts=candidate_artifacts,
            registry=registry,
            repository_root=Path(candidate_root).parent.parent,
            candidate_artifacts_root=Path(candidate_artifacts["sources"]["germany"]["path"]).parent,
        )

    def test_orphan_stop_removed_by_changed_upstream_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary), candidate_stop_ids=["new-stop"])
            retirements = self._validate(fixture)
            self.assertEqual(retirements[0]["cityID"], "fixture-city")

    def test_reused_source_id_with_different_identity_is_retired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary),
                candidate_stop_ids=["fixture-city-stop"],
                candidate_stop_overrides={
                    "fixture-city-stop": ("Replacement Stop", 51.0, 20.0),
                },
            )
            retirements = self._validate(fixture)
            self.assertEqual(retirements[0]["cityID"], "fixture-city")

    def test_city_with_service_cannot_be_retired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary),
                candidate_stop_ids=["new-stop"],
                service_city_ids={"fixture-city"},
            )
            with self.assertRaisesRegex(ValueError, "active city has service"):
                self._validate(fixture)

    def test_missing_or_failed_source_cannot_be_retired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary), candidate_stop_ids=["new-stop"])
            release_data, _candidate_artifacts, registry, _active, _candidate = fixture
            old_manifest, candidate_manifest, active_artifacts, candidate_root, active_root = release_data
            with self.assertRaisesRegex(ValueError, "missing from release metadata"):
                validate_previous_release_city_retirements(
                    old_manifest=old_manifest,
                    candidate_manifest=candidate_manifest,
                    active_stop_data=active_root,
                    candidate_stop_data=candidate_root,
                    active_artifacts=active_artifacts,
                    candidate_artifacts={"sources": {}},
                    registry=registry,
                    repository_root=Path(temporary),
                )

            failed_fixture = self._fixture(
                Path(temporary) / "failed", candidate_stop_ids=["new-stop"], candidate_status="preserved-stale"
            )
            with self.assertRaisesRegex(ValueError, "source import status"):
                self._validate(failed_fixture)

    def test_unchanged_source_cannot_explain_city_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary), candidate_stop_ids=["new-stop"])
            release_data, candidate_artifacts, registry, active_gtfs, _candidate_gtfs = fixture
            candidate_artifacts["sources"]["germany"]["path"] = str(active_gtfs)
            digest, size = artifact_provenance(active_gtfs)
            candidate_artifacts["sources"]["germany"].update(sha256=digest, size=size)
            with self.assertRaisesRegex(ValueError, "artifact did not change"):
                self._validate((release_data, candidate_artifacts, registry, active_gtfs, active_gtfs))

    def test_stop_still_in_changed_source_is_not_legitimate_retirement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(
                Path(temporary), candidate_stop_ids=["fixture-city-stop", "new-stop"]
            )
            with self.assertRaisesRegex(ValueError, "active stop still exists"):
                self._validate(fixture)

    def test_multiple_losses_require_proof_for_every_city(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            old_cities = [self._city("fixture-city"), self._city("second-city")]
            fixture = self._fixture(
                Path(temporary),
                candidate_stop_ids=["second-city-stop", "new-stop"],
                old_cities=old_cities,
            )
            with self.assertRaisesRegex(ValueError, "second-city"):
                self._validate(fixture)


if __name__ == "__main__":
    unittest.main()
