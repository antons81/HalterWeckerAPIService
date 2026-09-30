"""Measure full and transformed-cache HIT builds without downloading feeds."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import tempfile
import time
import zipfile
from pathlib import Path

from build_stop_packages import load_gtfs_archive
from external_build_cache import BUILDER_INPUTS, TRANSFORMED_CACHE_PROVIDER_IDS
from external_gtfs import load_external_gtfs_sources, process_external_gtfs_sources
from gtfs_source_cache import GTFSArtifactCache

STAGE_DURATION = re.compile(
    r"source=(?P<source>[^ ]+) stage=(?P<stage>[^ ]+) "
    r".*?duration=(?P<duration>[0-9.]+)s"
)
DEFAULT_PROVIDERS = tuple(sorted(TRANSFORMED_CACHE_PROVIDER_IDS))


def _write_fixture(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "agency.txt",
            "agency_id,agency_name,agency_url,agency_timezone\n"
            "1,Fixture,https://fixture.invalid,UTC\n",
        )
        archive.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type\n"
            "station,Station,0.000000,0.000000,,1\n"
            "platform,Platform,0.000000,0.000000,station,0\n"
            "street,Street Stop,0.001000,-0.001000,,0\n",
        )
        archive.writestr(
            "routes.txt",
            "route_id,route_short_name,route_long_name,route_type,agency_id\n"
            "R1,1,Fixture Line,3,1\n",
        )
        archive.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,trip_headsign,direction_id\n"
            "R1,S1,T1,Fixture Terminal,0\n",
        )
        archive.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,08:00:00,08:00:00,platform,1\n"
            "T1,08:10:00,08:10:00,street,2\n",
        )
        archive.writestr(
            "calendar.txt",
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "S1,1,1,1,1,1,1,1,20200101,20301231\n",
        )


def _parse_stage_durations(logs: str, provider_id: str) -> dict[str, float]:
    durations: dict[str, float] = {}
    for match in STAGE_DURATION.finditer(logs):
        if match.group("source") == provider_id:
            durations[match.group("stage")] = float(match.group("duration"))
    return durations


def _source_for_fixture(
    source: dict[str, object],
    *,
    provider_id: str,
    fixture: Path,
    cities_path: Path,
) -> dict[str, object]:
    configured = dict(source)
    configured["cities"] = f"config/{cities_path.name}"
    configured["url"] = str(fixture)
    configured["timezone"] = str(configured.get("timezone") or "UTC")
    configured.pop("dynamicResource", None)
    cities_path.write_text(
        json.dumps(
            [
                {
                    "id": "fixture-city",
                    "name": provider_id,
                    "aliases": [],
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "radiusMeters": 100_000,
                    "timezone": configured["timezone"],
                    "packageMode": "external",
                    "externalGTFSProvider": provider_id,
                    "externalGTFSProviders": [provider_id],
                }
            ]
        ),
        encoding="utf-8",
    )
    return configured


def _run(
    *,
    repository_root: Path,
    root: Path,
    provider_id: str,
    source: dict[str, object],
    output_name: str,
    use_normalized_context: bool,
) -> tuple[float, dict[str, float], str]:
    sources_path = root / f"sources-{output_name}.json"
    sources_path.write_text(json.dumps([source]), encoding="utf-8")
    environment = {
        "HALTEWECKER_EXTERNAL_BUILD_CACHE": "1",
        "HALTEWECKER_EXTERNAL_TRANSFORMED_BUILD_CACHE": "1",
        "HALTEWECKER_EXTERNAL_BUILD_CACHE_PROVIDERS": provider_id,
    }
    stream = io.StringIO()
    started = time.perf_counter()
    with contextlib.redirect_stdout(stream):
        process_external_gtfs_sources(
            repository_root=repository_root,
            sources_path=sources_path,
            url_by_provider={},
            output=root / output_name,
            load_gtfs_archive=load_gtfs_archive,
            environ=environment,
            gtfs_cache=GTFSArtifactCache(root / "gtfs-cache"),
            use_normalized_context=use_normalized_context,
        )
    elapsed = time.perf_counter() - started
    logs = stream.getvalue()
    return elapsed, _parse_stage_durations(logs, provider_id), logs


def _immutable_output_bytes(output: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for relative in (
        "stops/fixture-city.json",
        "routes/fixture-city.json",
        "trips/fixture-city.json",
    ):
        path = output / relative
        if path.is_file():
            result[relative] = path.read_bytes()
    return result


def _benchmark_provider(
    *,
    repository_root: Path,
    root: Path,
    source: dict[str, object],
    provider_id: str,
    fixture: Path,
) -> dict[str, object]:
    cities_path = repository_root / "config" / f"{provider_id}-cities.json"
    configured = _source_for_fixture(
        source,
        provider_id=provider_id,
        fixture=fixture,
        cities_path=cities_path,
    )
    baseline_root = root / "baseline" / provider_id
    d1_root = root / "d1" / provider_id
    baseline_root.mkdir(parents=True)
    d1_root.mkdir(parents=True)
    baseline_full, baseline_full_stages, _baseline_full_logs = _run(
        repository_root=repository_root,
        root=baseline_root,
        provider_id=provider_id,
        source=configured,
        output_name=f"{provider_id}-full",
        use_normalized_context=False,
    )
    baseline_hit, baseline_hit_stages, baseline_hit_logs = _run(
        repository_root=repository_root,
        root=baseline_root,
        provider_id=provider_id,
        source=configured,
        output_name=f"{provider_id}-hit",
        use_normalized_context=False,
    )
    d1_full, d1_full_stages, _d1_full_logs = _run(
        repository_root=repository_root,
        root=d1_root,
        provider_id=provider_id,
        source=configured,
        output_name=f"{provider_id}-full",
        use_normalized_context=True,
    )
    d1_hit, d1_hit_stages, d1_hit_logs = _run(
        repository_root=repository_root,
        root=d1_root,
        provider_id=provider_id,
        source=configured,
        output_name=f"{provider_id}-hit",
        use_normalized_context=True,
    )
    if "status=HIT" not in baseline_hit_logs or "status=HIT" not in d1_hit_logs:
        raise RuntimeError(
            f"{provider_id} did not produce cache HITs; logs:\n{baseline_hit_logs}\n{d1_hit_logs}"
        )
    if _immutable_output_bytes(
        baseline_root / f"{provider_id}-full"
    ) != _immutable_output_bytes(
        baseline_root / f"{provider_id}-hit"
    ) or _immutable_output_bytes(
        d1_root / f"{provider_id}-full"
    ) != _immutable_output_bytes(d1_root / f"{provider_id}-hit"):
        raise RuntimeError(f"{provider_id} immutable output parity failed")
    return {
        "provider": provider_id,
        "confidence": "fixture-derived",
        "baseline_full": baseline_full,
        "baseline_hit": baseline_hit,
        "d1_full": d1_full,
        "d1_hit": d1_hit,
        "c12_hit_to_d1_hit_saving": baseline_hit - d1_hit,
        "d1_unique_full_saving": baseline_full - d1_full,
        "baseline_departures": baseline_full_stages.get("departures", 0.0),
        "d1_departures": d1_full_stages.get("departures", 0.0),
        "d1_context": d1_full_stages.get("normalized-provider-context", 0.0),
        "d1_trip_index_enrichment": d1_hit_stages.get(
            "trip-index-headsign-enrichment", 0.0
        ),
        "baseline_hit_stages": baseline_hit_stages,
        "d1_hit_stages": d1_hit_stages,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--provider",
        action="append",
        dest="provider_ids",
        choices=DEFAULT_PROVIDERS,
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        help="Use an existing local GTFS archive; no feed is downloaded.",
    )
    args = parser.parse_args()

    repository_root = args.repository_root.resolve()
    provider_ids = tuple(args.provider_ids or DEFAULT_PROVIDERS)
    registry = {
        str(source["id"]): source
        for source in load_external_gtfs_sources(
            repository_root / "config" / "external-gtfs-sources.json"
        )
    }
    with tempfile.TemporaryDirectory(prefix="external-cache-benchmark-") as temporary:
        root = Path(temporary)
        runtime_repository_root = root / "repository"
        (runtime_repository_root / "scripts").mkdir(parents=True)
        (runtime_repository_root / "config").mkdir()
        for relative_script in BUILDER_INPUTS:
            source_path = repository_root / relative_script
            destination = runtime_repository_root / relative_script
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source_path.read_bytes())
        fixture = args.fixture.resolve() if args.fixture else root / "fixture.zip"
        if not fixture.is_file():
            if args.fixture:
                raise FileNotFoundError(fixture)
            _write_fixture(fixture)
        rows = [
            _benchmark_provider(
                repository_root=runtime_repository_root,
                root=root,
                source=registry[provider_id],
                provider_id=provider_id,
                fixture=fixture,
            )
            for provider_id in provider_ids
        ]

    print("provider | confidence | C1.2 HIT | D1 HIT | HIT delta | D1 full saving")
    for row in rows:
        print(
            f"{row['provider']} | {row['confidence']} | "
            f"{row['baseline_hit']:.4f}s | {row['d1_hit']:.4f}s | "
            f"{row['c12_hit_to_d1_hit_saving']:.4f}s | "
            f"{row['d1_unique_full_saving']:.4f}s"
        )
    print(
        "totals | fixture-derived | "
        f"D1_unique_full_saving={sum(float(row['d1_unique_full_saving']) for row in rows):.4f}s "
        f"C1.2_to_D1_HIT={sum(float(row['c12_hit_to_d1_hit_saving']) for row in rows):.4f}s"
    )
    print(
        "stage metrics: baseline_departures, d1_departures, d1_context, "
        "d1_trip_index_enrichment"
    )
    for row in rows:
        print(
            f"{row['provider']} stages: baseline_departures={row['baseline_departures']:.4f}s "
            f"d1_departures={row['d1_departures']:.4f}s "
            f"d1_context={row['d1_context']:.4f}s "
            f"d1_trip_index_enrichment={row['d1_trip_index_enrichment']:.4f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
