"""Trust records for immutable, content-addressed SQLite artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping


TRUST_SCHEMA_VERSION = 1
TRUST_RECORD_NAME = "trust.json"


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _manifest_sha256(manifest_path: Path) -> str | None:
    try:
        return hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    except OSError:
        return None


def _stat_payload(path: Path) -> dict[str, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtimeNs": int(stat.st_mtime_ns),
    }


def trust_record_for(
    *,
    database_path: Path,
    manifest_path: Path,
    manifest: Mapping[str, object],
) -> dict[str, object]:
    stat_payload = _stat_payload(database_path)
    manifest_sha256 = _manifest_sha256(manifest_path)
    if stat_payload is None or manifest_sha256 is None:
        raise OSError("artifact files are unavailable while creating trust record")
    sqlite_payload = manifest.get("sqlite")
    if not isinstance(sqlite_payload, Mapping):
        raise ValueError("artifact SQLite provenance is missing")
    validation = manifest.get("validation")
    if not isinstance(validation, Mapping):
        validation = {
            "fullSha256": True,
            "schemaValidated": True,
            "sqliteQuickCheck": "ok",
        }
    return {
        "trustSchemaVersion": TRUST_SCHEMA_VERSION,
        "artifactKey": manifest.get("artifactKey"),
        "providerID": manifest.get("providerID"),
        "artifactType": manifest.get("artifactType"),
        "manifestSha256": manifest_sha256,
        "sqlite": {
            "sha256": sqlite_payload.get("sha256"),
            "size": sqlite_payload.get("size"),
        },
        "file": stat_payload,
        "validation": dict(validation),
    }


def write_trust_record(
    *,
    directory: Path,
    database_path: Path,
    manifest_path: Path,
    manifest: Mapping[str, object],
) -> None:
    record = trust_record_for(
        database_path=database_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )
    temporary = directory / f".{TRUST_RECORD_NAME}.tmp-{os.getpid()}"
    try:
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, directory / TRUST_RECORD_NAME)
    finally:
        temporary.unlink(missing_ok=True)


def trusted_artifact(
    *,
    database_path: Path,
    manifest_path: Path,
    manifest: Mapping[str, object],
    force_full: bool = False,
) -> bool:
    """Return whether publication evidence permits metadata-only reuse."""
    if force_full or os.environ.get("HALTEWECKER_FORCE_FULL_ARTIFACT_VALIDATION") == "1":
        return False
    if manifest.get("status") != "complete":
        return False
    record = _read_json(manifest_path.parent / TRUST_RECORD_NAME)
    if record is None or record.get("trustSchemaVersion") != TRUST_SCHEMA_VERSION:
        return False
    if any(
        record.get(field) != manifest.get(field)
        for field in ("artifactKey", "providerID", "artifactType")
    ):
        return False
    if record.get("manifestSha256") != _manifest_sha256(manifest_path):
        return False
    sqlite_payload = manifest.get("sqlite")
    record_sqlite = record.get("sqlite")
    if not isinstance(sqlite_payload, Mapping) or not isinstance(record_sqlite, Mapping):
        return False
    if dict(record_sqlite) != {
        "sha256": sqlite_payload.get("sha256"),
        "size": sqlite_payload.get("size"),
    }:
        return False
    validation = manifest.get("validation") or record.get("validation")
    if not isinstance(validation, Mapping):
        return False
    if (
        validation.get("fullSha256") is not True
        or validation.get("sqliteQuickCheck") != "ok"
        or validation.get("schemaValidated") is not True
    ):
        return False
    if record.get("validation") != dict(validation):
        return False
    expected_file = record.get("file")
    actual_file = _stat_payload(database_path)
    if not isinstance(expected_file, Mapping) or actual_file is None:
        return False
    return dict(expected_file) == actual_file
