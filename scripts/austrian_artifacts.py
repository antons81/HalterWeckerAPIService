"""Resolve Austrian GTFS inputs from explicit provenance, never from filename year."""

from __future__ import annotations

import json
import re
import warnings
import zipfile
from pathlib import Path
from typing import Iterable, Mapping

try:
    from .gtfs_source_cache import REQUIRED_GTFS_FILES
    from .artifact_provenance import file_artifact_provenance
except ImportError:
    from gtfs_source_cache import REQUIRED_GTFS_FILES
    from artifact_provenance import file_artifact_provenance


def resolve_austrian_artifacts(
    sources: Iterable[Mapping[str, object]],
    directory: Path,
    manifest_path: Path | None = None,
    *, source_ids: set[str] | None = None,
) -> dict[str, Path]:
    registry = {str(source["id"]): source for source in sources}
    selected = set(registry) if source_ids is None else source_ids
    if not selected.issubset(registry):
        raise ValueError(f"Unknown Austrian GTFS sources: {sorted(selected - set(registry))}")
    automatic_manifest = directory / "artifacts.json"
    if manifest_path is None and (automatic_manifest.exists() or automatic_manifest.is_symlink()):
        manifest_path = automatic_manifest
    if manifest_path is None:
        result = {}
        for source_id in sorted(selected):
            candidates = sorted(directory.glob(f"{source_id}-*.zip"))
            valid = [path for path in candidates if path.is_file()]
            for path in candidates:
                if not path.is_file():
                    warnings.warn(f"Ignoring unavailable Austrian GTFS alias: {path}", RuntimeWarning)
            if len(valid) != 1:
                raise ValueError(
                    f"Austrian GTFS source {source_id}: expected one unambiguous archive, found {len(valid)}; "
                    "refresh MVO inputs or supply --austrian-artifacts manifest"
                )
            result[source_id] = valid[0]
        return result
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read Austrian GTFS manifest {manifest_path}: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise ValueError(f"Invalid Austrian GTFS manifest: {manifest_path}")
    entries = {}
    for entry in payload["sources"]:
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid Austrian GTFS entry in {manifest_path}")
        source_id = entry.get("source")
        if not isinstance(source_id, str) or source_id not in registry or source_id in entries:
            raise ValueError(f"Unexpected or duplicate Austrian GTFS source {source_id!r} in {manifest_path}")
        entries[source_id] = entry
    if set(entries) != set(registry):
        raise ValueError(f"Incomplete Austrian GTFS manifest {manifest_path}: missing {sorted(set(registry) - set(entries))}")
    result = {}
    for source_id in sorted(selected):
        source = registry[source_id]
        entry = entries[source_id]
        if (entry.get("datasetId") != source["datasetId"]
                or isinstance(entry.get("year"), bool) or not isinstance(entry.get("year"), int)
                or entry["year"] <= 0 or not isinstance(entry.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
                or isinstance(entry.get("size"), bool)):
            raise ValueError(f"Invalid Austrian GTFS provenance for source {source_id} in {manifest_path}")
        # The immutable input remains authoritative even if a display alias changes.
        raw_path = entry.get("immutablePath") or entry.get("path")
        if not isinstance(raw_path, str) or not raw_path or not isinstance(entry.get("size"), int) or entry["size"] <= 0:
            raise ValueError(f"Incomplete Austrian GTFS provenance for source {source_id}")
        path = Path(raw_path)
        if not path.is_absolute():
            path = manifest_path.parent / path
        try:
            if not path.is_file():
                raise ValueError(f"archive is missing: {path}")
            digest, size = file_artifact_provenance(path)
            if digest != entry["sha256"] or size != entry["size"]:
                raise ValueError(f"archive checksum/size mismatch: {path}")
            with zipfile.ZipFile(path) as archive:
                if not REQUIRED_GTFS_FILES.issubset(archive.namelist()):
                    raise ValueError("archive is missing required GTFS tables")
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            raise ValueError(f"Austrian GTFS source {source_id} failed input validation: {error}") from error
        result[source_id] = path
    return result
