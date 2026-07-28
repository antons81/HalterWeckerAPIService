#!/usr/bin/env python3
"""Validate and atomically activate the next static departures SQLite database."""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def validate_database(path: Path) -> str:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Missing or empty database: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        version = metadata.get("databaseVersion", "").strip()
        if not version:
            raise ValueError("Missing databaseVersion metadata.")
        required = {"raw_stops", "city_stops", "routes", "trips", "stop_times", "active_services"}
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = required - tables
        if missing:
            raise ValueError(f"Static departures database is missing tables: {', '.join(sorted(missing))}")
        if connection.execute("SELECT COUNT(*) FROM active_services").fetchone()[0] == 0:
            raise ValueError("Static departures database has no active service dates.")
        if connection.execute("SELECT COUNT(*) FROM city_stops").fetchone()[0] == 0:
            raise ValueError("Static departures database has no city stop memberships.")
        return version
    finally:
        connection.close()


def activate_database(data_root: Path) -> str:
    next_path = data_root / "staging" / "departures-next.sqlite"
    releases = data_root / "departures" / "releases"
    current = data_root / "departures-current.sqlite"
    version = validate_database(next_path)

    releases.mkdir(parents=True, exist_ok=True)
    release = releases / f"departures-{version}.sqlite"
    if release.exists():
        raise FileExistsError(f"Release already exists: {release}")

    next_path.replace(release)
    temporary_link = data_root / "departures-current.next"
    temporary_link.unlink(missing_ok=True)
    temporary_link.symlink_to(Path("departures") / "releases" / release.name)
    os.replace(temporary_link, current)
    return version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default=os.environ.get("DATA_ROOT", "/srv/haltewecker/data"))
    args = parser.parse_args()
    print(activate_database(Path(args.data_root)))


if __name__ == "__main__":
    main()
