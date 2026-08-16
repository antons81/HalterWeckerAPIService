#!/usr/bin/env python3
"""Validate that a staged release references one consistent stop-data version."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path.name}.")
    return payload


def validate_release(release_dir: Path) -> None:
    metadata = _read_json(release_dir / "release-metadata.json")
    manifest = _read_json(release_dir / "stop-data" / "manifest.json")
    problems: list[str] = []

    release_id = metadata.get("releaseID")
    manifest_release_id = manifest.get("releaseID")
    if not isinstance(release_id, str) or not release_id:
        problems.append("release-metadata.releaseID is missing")
    if manifest_release_id != release_id:
        problems.append("manifest.releaseID does not match release-metadata.releaseID")
    if metadata.get("stopManifestVersion") != manifest.get("version"):
        problems.append("stopManifestVersion does not match manifest.version")
    if metadata.get("sourceArtifacts") != manifest.get("sourceArtifacts"):
        problems.append("sourceArtifacts do not match between release metadata and manifest")

    database_path = release_dir / "departures.sqlite"
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        try:
            database_metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as error:
        problems.append(f"departures.sqlite metadata is unavailable: {type(error).__name__}")
    else:
        if database_metadata.get("releaseID") != release_id:
            problems.append("departures.sqlite releaseID does not match release metadata")
        if database_metadata.get("stopDataReleaseID") != release_id:
            problems.append("departures.sqlite stopDataReleaseID does not match release metadata")
        if database_metadata.get("stopDataManifestVersion") != manifest.get("version"):
            problems.append("departures.sqlite stopDataManifestVersion does not match manifest.version")

    if problems:
        raise ValueError("Release consistency validation failed: " + "; ".join(problems))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    args = parser.parse_args()
    validate_release(args.release_dir)
    print(f"[StopData] release={args.release_dir.name} consistency=ok")


if __name__ == "__main__":
    main()
