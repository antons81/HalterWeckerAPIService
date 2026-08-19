"""Deterministic fingerprint for future derived-artifact reuse."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path


DEFAULT_INPUTS = (
    "config/cities.json",
    "config/swiss-cities.json",
    "config/austrian-sources.json",
    "config/external-gtfs-sources.json",
    "config/ireland-cities.json",
    "config/city-id-aliases.json",
    "scripts/build_stop_packages.py",
    "scripts/kyiv_open_data.py",
    "scripts/external_gtfs.py",
    "scripts/build_swiss_departure_index.py",
    "scripts/build_german_departure_index.py",
    "scripts/import_static_departures_database.py",
    "scripts/static_departures_ownership.py",
)


def git_revision(repository: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def compute(repository: Path, inputs: tuple[str, ...] = DEFAULT_INPUTS) -> str:
    digest = hashlib.sha256()
    digest.update(f"git:{git_revision(repository)}\n".encode())
    for relative in inputs:
        path = repository / relative
        digest.update(f"path:{relative}\n".encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(compute(args.repository))


if __name__ == "__main__":
    main()
