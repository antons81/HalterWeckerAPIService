#!/usr/bin/env python3
"""Local synthetic 1/2/5-provider fan-out benchmark for Phase 5B."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "services"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tests"))

from static_departures_api import Database  # noqa: E402
from static_departures_runtime import ReleaseSnapshot  # noqa: E402
from test_static_departures_multi_provider import StaticDeparturesMultiProviderTests  # noqa: E402


def _rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _measure(call):
    started = time.perf_counter()
    value = call()
    return {
        "wallSeconds": time.perf_counter() - started,
        "rssBytes": _rss_bytes(),
        "resultCount": len(value),
    }, value


def _expand_fixture(release: Path, provider_count: int) -> None:
    """Give the tiny fixture enough rows to exercise all requested LIMITs."""
    provider_ids = ["israel-mot", *[f"synthetic-{index}" for index in range(2, provider_count + 1)]]
    release_manifest = json.loads((release / "release.json").read_text(encoding="utf-8"))
    for provider_id in provider_ids:
        structural_dir = release / "providers" / provider_id / "structural"
        database_path = structural_dir / "provider.sqlite"
        with sqlite3.connect(database_path) as connection:
            trip = connection.execute(
                "SELECT trip_id, service_id, route_id, headsign, direction_id, terminal_stop_id FROM trips ORDER BY trip_id LIMIT 1"
            ).fetchone()
            stop_times = connection.execute(
                "SELECT raw_stop_id, arrival_time, departure_time, departure_seconds, stop_sequence FROM stop_times WHERE trip_id=? ORDER BY stop_sequence",
                (trip[0],),
            ).fetchall()
            for index in range(1, 121):
                trip_id = f"israel:bench-{index:03d}"
                connection.execute("INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)", (trip_id, *trip[1:]))
                connection.execute(
                    "INSERT INTO provider_entities(entity_type, provider_id, key_1, key_2, key_3) VALUES ('trips', 'israel-mot', ?, '', '')",
                    (trip_id,),
                )
                for raw_stop_id, arrival, departure, _seconds, sequence in stop_times:
                    seconds = 8 * 3600 + index * 60 + (sequence - 1) * 60
                    scheduled = f"{seconds // 3600:02d}:{(seconds // 60) % 60:02d}:{seconds % 60:02d}"
                    connection.execute(
                        "INSERT INTO stop_times VALUES (?, ?, ?, ?, ?, ?)",
                        (trip_id, raw_stop_id, arrival, scheduled, seconds, sequence),
                    )
            connection.commit()
        digest = hashlib.sha256(database_path.read_bytes()).hexdigest()
        size = database_path.stat().st_size
        provider_manifest_path = structural_dir / "manifest.json"
        provider_manifest = json.loads(provider_manifest_path.read_text(encoding="utf-8"))
        provider_manifest["artifactKey"] = digest[:24]
        provider_manifest["sqlite"].update({"sha256": digest, "size": size})
        provider_manifest_path.write_text(json.dumps(provider_manifest, indent=2), encoding="utf-8")
        temporal_manifest_path = release / "providers" / provider_id / "temporal" / "manifest.json"
        temporal_manifest = json.loads(temporal_manifest_path.read_text(encoding="utf-8"))
        temporal_manifest["structuralArtifactKey"] = digest[:24]
        temporal_manifest_path.write_text(json.dumps(temporal_manifest, indent=2), encoding="utf-8")
        release_manifest["providers"][provider_id]["structural"].update({"artifactKey": digest[:24], "sha256": digest, "size": size})

    common_path = release / "common.sqlite"
    with sqlite3.connect(common_path) as connection:
        for provider_id in provider_ids:
            key = release_manifest["providers"][provider_id]["structural"]["artifactKey"]
            connection.execute("UPDATE provider_registry SET structural_artifact_key=? WHERE provider_id=?", (key, provider_id))
        connection.commit()
    release_manifest["common"].update({
        "sha256": hashlib.sha256(common_path.read_bytes()).hexdigest(),
        "size": common_path.stat().st_size,
    })
    (release / "release.json").write_text(json.dumps(release_manifest, indent=2), encoding="utf-8")


def _expand_legacy(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        trip = connection.execute(
            "SELECT trip_id, service_id, route_id, headsign, direction_id, terminal_stop_id FROM trips ORDER BY trip_id LIMIT 1"
        ).fetchone()
        stop_times = connection.execute(
            "SELECT raw_stop_id, arrival_time, departure_time, departure_seconds, stop_sequence FROM stop_times WHERE trip_id=? ORDER BY stop_sequence",
            (trip[0],),
        ).fetchall()
        for index in range(1, 121):
            trip_id = f"israel:bench-{index:03d}"
            connection.execute("INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)", (trip_id, *trip[1:]))
            connection.execute(
                "INSERT INTO provider_entities(entity_type, provider_id, key_1, key_2, key_3) VALUES ('trips', 'israel-mot', ?, '', '')",
                (trip_id,),
            )
            for raw_stop_id, arrival, _departure, _seconds, sequence in stop_times:
                seconds = 8 * 3600 + index * 60 + (sequence - 1) * 60
                scheduled = f"{seconds // 3600:02d}:{(seconds // 60) % 60:02d}:{seconds % 60:02d}"
                connection.execute(
                    "INSERT INTO stop_times VALUES (?, ?, ?, ?, ?, ?)",
                    (trip_id, raw_stop_id, arrival, scheduled, seconds, sequence),
                )
        connection.commit()


def run(provider_counts: tuple[int, ...], limits: tuple[int, ...]) -> dict[str, object]:
    now = datetime(2026, 1, 5, 7, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    rows: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="phase5b-runtime-benchmark-") as temporary:
        root = Path(temporary)
        for provider_count in provider_counts:
            helper = StaticDeparturesMultiProviderTests()
            fixture_root = root / f"providers-{provider_count}"
            fixture_root.mkdir()
            legacy_path, release = helper._build_fixture(fixture_root, provider_count)
            _expand_fixture(release, provider_count)
            _expand_legacy(legacy_path)
            legacy = Database(str(legacy_path))
            snapshot = ReleaseSnapshot.open(
                release,
                provider_ids=tuple(["israel-mot", *[f"synthetic-{index}" for index in range(2, provider_count + 1)]]),
                max_provider_connections=min(4, provider_count),
                max_parallel_provider_queries=min(4, provider_count),
            )
            try:
                for limit in limits:
                    legacy_metrics, legacy_value = _measure(
                        lambda limit=limit: legacy.external_departures_for(
                            "fixture-israel", "S1", limit, now, "Asia/Jerusalem", now_provider=lambda: now
                        )
                    )
                    shard_metrics, shard_value = _measure(
                        lambda limit=limit: snapshot.external_departures_for(
                            "fixture-israel", "S1", limit, now, "Asia/Jerusalem", now_provider=lambda: now
                        )
                    )
                    fanout = snapshot.last_fanout_metrics
                    rows.append(
                        {
                            "providers": provider_count,
                            "limit": limit,
                            "legacy": legacy_metrics,
                            "multiShard": shard_metrics,
                            "sameFinalCardinality": len(legacy_value) == len(shard_value),
                            "providerCount": fanout.provider_count if fanout else 0,
                            "rowsFetchedBeforeFinalLimit": fanout.rows_fetched if fanout else 0,
                            "fanoutSeconds": fanout.fanout_duration if fanout else 0.0,
                            "mergeSeconds": fanout.merge_duration if fanout else 0.0,
                            "connectionsOpened": snapshot.connection_count,
                            "peakProviderConnections": snapshot.peak_provider_connections,
                            "commonCatalogBytes": (release / "common.sqlite").stat().st_size,
                            "commonCatalogBuildSeconds": helper.last_common_build.build_seconds,
                        }
                    )
            finally:
                snapshot.close()
                legacy.close()
    return {"fixture": "synthetic-israel-shaped-provider-shards", "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", default="1,2,5")
    parser.add_argument("--limits", default="10,30,100")
    args = parser.parse_args()
    provider_counts = tuple(int(value) for value in args.providers.split(",") if value)
    limits = tuple(int(value) for value in args.limits.split(",") if value)
    print(json.dumps(run(provider_counts, limits), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
