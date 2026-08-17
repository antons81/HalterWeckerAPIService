"""Deterministic provenance helpers for release input artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def file_artifact_provenance(path: Path) -> tuple[str, int]:
    digest_builder = hashlib.sha256()
    total_size = path.stat().st_size
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest_builder.update(chunk)
    return digest_builder.hexdigest(), total_size


def directory_artifact_provenance(path: Path) -> tuple[str, int]:
    digest_builder = hashlib.sha256()
    total_size = 0
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        raise ValueError(f"Artifact directory is empty: {path}")
    for file_path in files:
        relative_path = file_path.relative_to(path).as_posix().encode("utf-8")
        digest_builder.update(len(relative_path).to_bytes(8, "big"))
        digest_builder.update(relative_path)
        file_size = file_path.stat().st_size
        total_size += file_size
        digest_builder.update(file_size.to_bytes(8, "big"))
        with file_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest_builder.update(chunk)
    return digest_builder.hexdigest(), total_size


def artifact_provenance(path: Path) -> tuple[str, int]:
    if path.is_dir():
        return directory_artifact_provenance(path)
    if path.is_file():
        return file_artifact_provenance(path)
    raise ValueError(f"Artifact path does not exist: {path}")


def canonical_content_provenance(
    payload: Any,
    *,
    identity: str,
) -> tuple[str, int]:
    encoded = json.dumps(
        {"identity": identity, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def immutable_file_path(path: Path, digest: str) -> Path:
    """Return a digest-named hardlink without copying large downloaded files."""
    if not path.is_file():
        return path
    immutable = path.with_name(f"{digest}{path.suffix or '.artifact'}")
    if immutable.resolve() == path.resolve():
        return immutable
    if immutable.exists():
        return immutable
    try:
        os.link(path, immutable)
    except FileExistsError:
        pass
    except OSError:
        # Filesystems without hardlink support still get a deterministic path;
        # the release validator will detect a later content mutation.
        return path
    return immutable


def provenance_record(
    *,
    source_id: str,
    path: str,
    digest: str,
    size: int,
    origin: str,
    status: str = "used",
) -> dict[str, object]:
    return {
        "sourceID": source_id,
        "path": path,
        "sha256": digest,
        "size": size,
        "origin": origin,
        "status": status,
    }
