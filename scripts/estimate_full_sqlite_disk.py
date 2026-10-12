#!/usr/bin/env python3
"""Estimate a full-SQLite candidate from the active release's allocated blocks."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

GIB = 1024**3
DATABASE_GROWTH_NUMERATOR = 5
DATABASE_GROWTH_DENOMINATOR = 4


def sqlite_cities(database: Path) -> set[str]:
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT city_id FROM provider_city_modes WHERE city_id<>''"
                )
            }
    except (OSError, sqlite3.Error) as error:
        raise ValueError(f"cannot read active full SQLite city mappings: {error}") from error


def allocated_stop_data_bytes(stop_data: Path, sqlite_cities: set[str]) -> tuple[int, int]:
    root = stop_data.resolve(strict=True)
    all_blocks: dict[tuple[int, int], int] = {}
    omitted_schedule_inodes: set[tuple[int, int]] = set()
    retained_inodes: set[tuple[int, int]] = set()

    for current, directories, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in directories:
            path = base / name
            if path.is_symlink():
                info = path.stat(follow_symlinks=False)
                key = (info.st_dev, info.st_ino)
                all_blocks[key] = max(all_blocks.get(key, 0), info.st_blocks * 512)
                retained_inodes.add(key)
        for name in files:
            path = base / name
            try:
                info = path.stat(follow_symlinks=False)
            except OSError as error:
                raise ValueError(f"cannot inspect stop-data file {path}: {error}") from error
            key = (info.st_dev, info.st_ino)
            all_blocks[key] = max(all_blocks.get(key, 0), info.st_blocks * 512)
            relative = path.relative_to(root)
            is_sql_schedule = (
                len(relative.parts) == 2
                and relative.parts[0] == "departures"
                and path.stem in sqlite_cities
            ) or (
                len(relative.parts) >= 3
                and relative.parts[0] == "departures-v2"
                and relative.parts[1] in sqlite_cities
            )
            (omitted_schedule_inodes if is_sql_schedule else retained_inodes).add(key)

    total = sum(all_blocks.values())
    replaceable = sum(
        all_blocks[key] for key in omitted_schedule_inodes - retained_inodes
    )
    return total, min(total, replaceable)


def estimate(stop_data: Path, database: Path) -> dict[str, int]:
    if not database.is_file():
        raise ValueError(f"active departures SQLite is missing: {database}")
    active_database_bytes = database.stat().st_size
    if active_database_bytes <= 0:
        raise ValueError("active departures SQLite is empty")
    total_stop_data_bytes, replaced_schedule_bytes = allocated_stop_data_bytes(
        stop_data, sqlite_cities(database)
    )
    candidate_database_bytes = (
        active_database_bytes * DATABASE_GROWTH_NUMERATOR
        + DATABASE_GROWTH_DENOMINATOR
        - 1
    ) // DATABASE_GROWTH_DENOMINATOR
    candidate_stop_data_bytes = total_stop_data_bytes - replaced_schedule_bytes
    return {
        "activeDatabaseBytes": active_database_bytes,
        "candidateDatabaseBytes": candidate_database_bytes,
        "activeStopDataAllocatedBytes": total_stop_data_bytes,
        "replacedScheduleAllocatedBytes": replaced_schedule_bytes,
        "candidateStopDataBytes": candidate_stop_data_bytes,
        "databaseGrowthPercent": 25,
        "candidateDatabaseKiB": (candidate_database_bytes + 1023) // 1024,
        "candidateStopDataKiB": (candidate_stop_data_bytes + 1023) // 1024,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop-data", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(estimate(args.stop_data, args.database), sort_keys=True))
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
