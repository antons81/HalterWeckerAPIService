#!/usr/bin/env python3
"""Compare legacy and Israel provider-shard query paths locally."""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "services"))
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from static_departures_api import Database, parse_iso_boundary  # noqa: E402
from static_departures_runtime import (  # noqa: E402
    ISRAEL_PROVIDER_ID,
    ReleaseSnapshot,
    compare_results,
)


def _rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _call_query(
    query: str,
    database: object,
    snapshot: ReleaseSnapshot,
    args: argparse.Namespace,
    timezone_name: str,
) -> object:
    city_id = args.city
    stop_id = args.stop
    provider = None if isinstance(database, Database) else snapshot.provider(ISRAEL_PROVIDER_ID)
    if query == "lines":
        return database.lines(city_id, stop_id) if isinstance(database, Database) else provider.lines(city_id, stop_id)
    if query == "departures":
        boundary = parse_iso_boundary(args.from_value, timezone_name)
        if boundary is None:
            boundary = datetime.now(ZoneInfo(timezone_name))
        return (
            database.external_departures_for(
                city_id,
                stop_id,
                args.limit,
                boundary,
                timezone_name,
                now_provider=lambda: boundary,
            )
            if isinstance(database, Database)
            else provider.external_departures_for(
                city_id,
                stop_id,
                args.limit,
                boundary,
                timezone_name,
                now_provider=lambda: boundary,
            )
        )
    if query == "board":
        from_date = parse_iso_boundary(args.from_value, timezone_name)
        to_date = parse_iso_boundary(args.to_value, timezone_name)
        return database.board(city_id, stop_id, args.limit, from_date, to_date) if isinstance(database, Database) else provider.board(city_id, stop_id, args.limit, from_date, to_date)
    if query == "trip-details":
        if not args.trip:
            raise ValueError("--trip is required for trip-details")
        return database.trip_details(city_id, args.trip, str(args.static_root), args.service_date) if isinstance(database, Database) else provider.trip_details(city_id, args.trip, str(args.static_root), args.service_date)
    if query == "trip-registry":
        return database.provider_trip_registry(ISRAEL_PROVIDER_ID) if isinstance(database, Database) else provider.trip_registry()
    if query == "route-registry":
        return database.provider_route_type_registry(ISRAEL_PROVIDER_ID) if isinstance(database, Database) else provider.route_type_registry()
    if query == "stop-registry":
        return database.provider_stop_registry(ISRAEL_PROVIDER_ID) if isinstance(database, Database) else provider.stop_registry()
    if query == "trip-stop-registry":
        trip_ids = set(filter(None, (args.trip or "").split(",")))
        if not trip_ids:
            raise ValueError("--trip is required for trip-stop-registry")
        return database.provider_trip_stop_registry(ISRAEL_PROVIDER_ID, trip_ids) if isinstance(database, Database) else provider.trip_stop_registry(trip_ids)
    raise ValueError(f"unknown query: {query}")


def _measure(label: str, callback: Callable[[], object]) -> dict[str, object]:
    started = time.perf_counter()
    cpu_started = time.process_time()
    try:
        value = callback()
        error = None
    except Exception as exception:
        value = None
        error = f"{type(exception).__name__}: {exception}"
    return {
        "label": label,
        "wallSeconds": time.perf_counter() - started,
        "cpuSeconds": time.process_time() - cpu_started,
        "rssBytes": _rss_bytes(),
        "error": error,
        "value": value,
    }


def _serialize(value: object) -> object:
    if isinstance(value, (set, frozenset)):
        return sorted((_serialize(item) for item in value), key=repr)
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--legacy-database", type=Path, required=True)
    parser.add_argument("--static-root", type=Path, required=True)
    parser.add_argument("--city", default="israel")
    parser.add_argument("--stop", default="")
    parser.add_argument("--trip", default="")
    parser.add_argument("--service-date", default=None)
    parser.add_argument("--from", dest="from_value", default=None)
    parser.add_argument("--to", dest="to_value", default=None)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument(
        "--query",
        choices=("lines", "departures", "board", "trip-details", "trip-registry", "route-registry", "stop-registry", "trip-stop-registry", "all"),
        default="all",
    )
    args = parser.parse_args()
    queries = (
        ("lines", "departures", "board", "trip-details", "trip-registry", "route-registry", "stop-registry", "trip-stop-registry")
        if args.query == "all" else (args.query,)
    )
    legacy = Database(str(args.legacy_database))
    snapshot = ReleaseSnapshot.open(args.release_root, trace_queries=True)
    try:
        timezone_name = snapshot.catalog.provider_mode(args.city, ISRAEL_PROVIDER_ID).timezone
        results: list[dict[str, object]] = []
        for query in queries:
            legacy_connection = legacy._connection()
            legacy_query_count = [0]
            legacy_connection.set_trace_callback(
                lambda _sql: legacy_query_count.__setitem__(0, legacy_query_count[0] + 1)
            )
            legacy_result = _measure(
                "legacy",
                lambda query=query: _call_query(query, legacy, snapshot, args, timezone_name),
            )
            provider = snapshot.provider(ISRAEL_PROVIDER_ID)
            provider.query_count = 0
            shard_result = _measure(
                "shard",
                lambda query=query: _call_query(query, provider, snapshot, args, timezone_name),
            )
            comparison = None
            if legacy_result["error"] is None and shard_result["error"] is None:
                comparison = compare_results(legacy_result["value"], shard_result["value"])
            results.append({
                "query": query,
                "legacy": {key: value for key, value in legacy_result.items() if key != "value"},
                "shard": {key: value for key, value in shard_result.items() if key != "value"},
                "comparison": {
                    "status": comparison.status if comparison else "ERROR",
                    "diagnostic": comparison.diagnostic if comparison else "query raised an exception",
                    "legacyCount": comparison.legacy_count if comparison else None,
                    "shardCount": comparison.shard_count if comparison else None,
                },
                "shardQueryCount": provider.query_count,
                "legacyQueryCount": legacy_query_count[0],
                "legacyConnections": 1 if legacy.connection is not None else 0,
                "shardConnections": snapshot.connection_count,
            })
        print(json.dumps(_serialize({"releaseID": snapshot.release_id, "results": results}), indent=2, ensure_ascii=False))
    finally:
        snapshot.close()
        legacy.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
