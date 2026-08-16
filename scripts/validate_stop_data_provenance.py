#!/usr/bin/env python3
"""Fail closed when static departures inputs do not match published stop-data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}.")
    return payload


def _artifact_provenance(path: Path) -> dict[str, dict[str, object]]:
    payload = _object(path)
    result: dict[str, dict[str, object]] = {}
    for group in ("sources", "external"):
        entries = payload.get(group) or {}
        if not isinstance(entries, dict):
            raise ValueError(f"GTFS artifact manifest field {group} is invalid.")
        for source_id, entry in entries.items():
            if not isinstance(entry, dict) or not entry.get("path"):
                continue
            digest = entry.get("sha256")
            size = entry.get("size")
            if not isinstance(digest, str) or not digest:
                raise ValueError(f"GTFS artifact {source_id} has no SHA-256 provenance.")
            if not isinstance(size, int) or size <= 0:
                raise ValueError(f"GTFS artifact {source_id} has no size provenance.")
            result[str(source_id)] = {"sha256": digest, "size": size}
    if not result:
        raise ValueError("GTFS artifact manifest contains no usable provenance.")
    return result


def validate_stop_data_provenance(
    stop_data: Path,
    artifacts_manifest: Path,
    expected_release_id: str = "",
) -> None:
    manifest = _object(stop_data / "manifest.json")
    manifest_release_id = manifest.get("releaseID")
    if expected_release_id and manifest_release_id != expected_release_id:
        raise ValueError(
            "stop-data release mismatch: "
            f"expected {expected_release_id}, got {manifest_release_id or '<missing>'}"
        )

    expected = _artifact_provenance(artifacts_manifest)
    actual = manifest.get("sourceArtifacts")
    if not isinstance(actual, dict):
        raise ValueError(
            "stop-data manifest has no sourceArtifacts provenance; refusing stale release"
        )

    problems: list[str] = []
    for source_id, expected_entry in sorted(expected.items()):
        actual_entry = actual.get(source_id)
        if not isinstance(actual_entry, dict):
            problems.append(f"{source_id}: missing from stop-data manifest")
            continue
        if actual_entry.get("sha256") != expected_entry["sha256"]:
            problems.append(
                f"{source_id}: SHA-256 mismatch "
                f"stop-data={actual_entry.get('sha256', '<missing>')} "
                f"static={expected_entry['sha256']}"
            )
        if actual_entry.get("size") != expected_entry["size"]:
            problems.append(
                f"{source_id}: size mismatch "
                f"stop-data={actual_entry.get('size', '<missing>')} "
                f"static={expected_entry['size']}"
            )
    if problems:
        raise ValueError(
            "stop-data/GTFS provenance mismatch; refusing static-departures import: "
            + "; ".join(problems)
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop-data", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--release-id", default="")
    args = parser.parse_args()
    validate_stop_data_provenance(args.stop_data, args.artifacts, args.release_id)
    print(f"[StaticDepartures] stop-data provenance=ok release={args.release_id or 'unknown'}")


if __name__ == "__main__":
    main()
