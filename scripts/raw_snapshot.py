"""Validate immutable raw GTFS snapshot manifests."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Iterable, Mapping

try:
    from .artifact_provenance import artifact_provenance
except ImportError:
    from artifact_provenance import artifact_provenance


REQUIRED_GTFS_FILES = frozenset(
    {"agency.txt", "routes.txt", "stops.txt", "stop_times.txt", "trips.txt"}
)


class RawSnapshotError(ValueError):
    """Raised when a pinned raw snapshot cannot be trusted."""


def _validated_entry(provider_id: str, entry: Mapping[str, object]) -> dict[str, object]:
    if str(entry.get("providerID") or "") != provider_id:
        raise RawSnapshotError(f"provider={provider_id} snapshot identity mismatch")
    pinned_value = str(entry.get("pinnedPath") or "").strip()
    if not pinned_value:
        raise RawSnapshotError(f"provider={provider_id} snapshot path is missing")
    pinned_path = Path(pinned_value)
    if not pinned_path.is_absolute() or not pinned_path.is_file():
        raise RawSnapshotError(
            f"provider={provider_id} pinned raw artifact is missing: {pinned_path}"
        )
    declared_digest = str(entry.get("sha256") or "").lower()
    declared_size = entry.get("size")
    if len(declared_digest) != 64 or any(
        character not in "0123456789abcdef" for character in declared_digest
    ):
        raise RawSnapshotError(f"provider={provider_id} snapshot SHA is invalid")
    if not isinstance(declared_size, int) or declared_size <= 0:
        raise RawSnapshotError(f"provider={provider_id} snapshot size is invalid")
    digest, size = artifact_provenance(pinned_path)
    if digest != declared_digest:
        raise RawSnapshotError(f"provider={provider_id} pinned raw SHA mismatch")
    if size != declared_size:
        raise RawSnapshotError(f"provider={provider_id} pinned raw size mismatch")
    try:
        with zipfile.ZipFile(pinned_path) as archive:
            names = {
                Path(name).name
                for name in archive.namelist()
                if not name.endswith("/")
            }
    except (OSError, zipfile.BadZipFile) as error:
        raise RawSnapshotError(
            f"provider={provider_id} pinned raw archive is unreadable"
        ) from error
    missing = sorted(REQUIRED_GTFS_FILES - names)
    if missing:
        raise RawSnapshotError(
            f"provider={provider_id} pinned raw archive is missing GTFS files: {missing}"
        )
    return dict(entry)


def load_raw_snapshot_manifest(
    path: Path,
    *,
    required_provider_ids: Iterable[str] = (),
) -> dict[str, dict[str, object]]:
    """Load and validate every pinned entry required by the caller."""
    manifest_path = path.resolve()
    if not manifest_path.is_file():
        raise RawSnapshotError(f"raw snapshot manifest is missing: {manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise RawSnapshotError(
            f"raw snapshot manifest is unreadable: {manifest_path}"
        ) from error
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise RawSnapshotError("raw snapshot manifest schema is unsupported")
    raw_entries = payload.get("providers")
    if not isinstance(raw_entries, list):
        raise RawSnapshotError("raw snapshot manifest has no providers list")
    entries: dict[str, dict[str, object]] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            raise RawSnapshotError("raw snapshot manifest contains an invalid provider entry")
        provider_id = str(raw_entry.get("providerID") or "").strip()
        if not provider_id or provider_id in entries:
            raise RawSnapshotError("raw snapshot manifest contains duplicate provider IDs")
        entries[provider_id] = _validated_entry(provider_id, raw_entry)
    required = tuple(dict.fromkeys(str(provider_id) for provider_id in required_provider_ids))
    missing = sorted(set(required) - set(entries))
    if missing:
        raise RawSnapshotError(f"raw snapshot manifest is missing providers: {missing}")
    return entries


def validate_raw_snapshot_entry(
    provider_id: str,
    entry: Mapping[str, object],
) -> tuple[Path, str]:
    """Revalidate one already-loaded entry immediately before consumption."""
    validated = _validated_entry(provider_id, entry)
    path = Path(str(validated["pinnedPath"])).resolve()
    return path, str(validated["sha256"])
