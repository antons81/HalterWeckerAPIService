#!/usr/bin/env python3
"""Share identical immutable payloads after candidate validation.

Created by Anton on 2026-10-10.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import uuid
from pathlib import Path

PAYLOAD_DIRECTORIES = frozenset({
    "stops", "routes", "trips", "transit", "radar", "swiss-static",
    "departures", "departures-v2", "trip-index-base",
})
CHUNK_BYTES = 1024 * 1024


def identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def same_bytes(source: Path, candidate: Path) -> bool:
    with source.open("rb") as previous, candidate.open("rb") as current:
        while True:
            left, right = previous.read(CHUNK_BYTES), current.read(CHUNK_BYTES)
            if left != right:
                return False
            if not left:
                return True


def deduplicate(source: Path, candidate: Path) -> dict[str, int]:
    source, candidate = source.resolve(strict=True), candidate.resolve(strict=True)
    if source == candidate or source in candidate.parents or candidate in source.parents:
        raise ValueError("source and candidate must be separate release trees")
    old = json.loads((source / "manifest.json").read_text())
    new = json.loads((candidate / "manifest.json").read_text())
    if not old.get("releaseID") or not new.get("releaseID") or old["releaseID"] == new["releaseID"]:
        raise ValueError("deduplication requires distinct release generations")
    result = {"linkedFiles": 0, "reclaimedBytes": 0, "comparedBytes": 0}
    for path in candidate.rglob("*.json"):
        relative = path.relative_to(candidate)
        if relative.parts[0] not in PAYLOAD_DIRECTORIES or "manifest" in path.name:
            continue
        previous = source / relative
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError(f"candidate payload contains a symlink: {relative}")
        if not previous.exists() or any(parent.is_symlink() for parent in (previous, *previous.parents)):
            continue
        current_stat, old_stat = path.stat(), previous.stat()
        if not stat.S_ISREG(current_stat.st_mode) or not stat.S_ISREG(old_stat.st_mode):
            continue
        # Already shared cache partitions consume no additional physical bytes.
        if current_stat.st_nlink != 1 or current_stat.st_dev != old_stat.st_dev:
            continue
        if current_stat.st_size != old_stat.st_size:
            continue
        result["comparedBytes"] += current_stat.st_size
        if not same_bytes(previous, path):
            continue
        if identity(path.stat()) != identity(current_stat) or identity(previous.stat()) != identity(old_stat):
            raise ValueError(f"payload changed during comparison: {relative}")
        temporary = path.with_name(f".{path.name}.dedup-{uuid.uuid4().hex}")
        try:
            try:
                os.link(previous, temporary, follow_symlinks=False)
            except OSError as error:
                if error.errno == errno.EXDEV:
                    continue
                raise
            # Linking changes ctime; validate every other source identity field.
            linked = temporary.stat()
            if (linked.st_dev, linked.st_ino, linked.st_size, linked.st_mtime_ns) != identity(old_stat)[:4]:
                raise ValueError(f"source changed before link: {relative}")
            if identity(path.stat()) != identity(current_stat):
                raise ValueError(f"candidate changed before replacement: {relative}")
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            result["linkedFiles"] += 1
            result["reclaimedBytes"] += current_stat.st_blocks * 512
        finally:
            temporary.unlink(missing_ok=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    if not (args.source / "manifest.json").is_file():
        print("[StopDataDedup] status=SKIPPED reason=no-published-generation", flush=True)
        return
    print("[StopDataDedup] " + json.dumps(deduplicate(args.source, args.candidate)), flush=True)


if __name__ == "__main__":
    main()
