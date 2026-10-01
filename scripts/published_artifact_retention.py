#!/usr/bin/env python3
"""Reference- and inode-aware retention for published partition/static artifacts.

Created by Anton Stremovskiy on 01.10.2026.
The shell owner holds the existing pipeline locks throughout this operation.
"""

from __future__ import annotations

import argparse
import collections
import fcntl
import json
import math
import os
import stat
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

Inode = tuple[int, int]


def identity(path: Path) -> Inode:
    value = path.stat()
    return value.st_dev, value.st_ino


def read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"not a JSON object: {path}")
    return value


@dataclass
class Processes:
    inodes: set[Inode] = field(default_factory=set)
    workspaces: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    filesystem_prefix: str = ""

    def uses(self, path: Path) -> bool:
        name = str(path)
        if self.filesystem_prefix and name.startswith(self.filesystem_prefix + "/"):
            name = name[len(self.filesystem_prefix):]
        return any(p == name or p.startswith(name + "/") for p in self.workspaces) or any(
            name in command for command in self.commands
        )

    @classmethod
    def scan(cls, root: Path) -> Processes:
        result = cls()
        try:
            processes = list(root.iterdir())
        except OSError as error:
            result.errors.append(str(error))
            return result
        for process in processes:
            if not process.name.isdigit():
                continue
            try:
                result.commands.append((process / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"))
                try:
                    result.workspaces.append(os.readlink(process / "cwd"))
                except FileNotFoundError:
                    pass
                for descriptor in (process / "fd").iterdir():
                    try:
                        value = descriptor.stat()
                        if stat.S_ISREG(value.st_mode):
                            result.inodes.add((value.st_dev, value.st_ino))
                        elif stat.S_ISDIR(value.st_mode):
                            result.workspaces.append(os.readlink(descriptor))
                    except FileNotFoundError:
                        pass
                for line in (process / "maps").read_text().splitlines():
                    fields = line.split(None, 5)
                    if len(fields) >= 5 and int(fields[4]):
                        major, minor = (int(v, 16) for v in fields[3].split(":"))
                        result.inodes.add((os.makedev(major, minor), int(fields[4])))
            except (FileNotFoundError, ProcessLookupError):
                pass
            except (OSError, ValueError) as error:
                result.errors.append(f"pid={process.name}:{error}")
        return result


@dataclass
class File:
    path: Path
    device: int
    inode: int
    links: int
    size: int
    allocated: int
    mtime_ns: int

    @property
    def key(self) -> Inode:
        return self.device, self.inode

    @classmethod
    def capture(cls, path: Path) -> File:
        value = path.lstat()
        if not stat.S_ISREG(value.st_mode):
            raise ValueError(f"nonregular entry: {path}")
        return cls(path, value.st_dev, value.st_ino, value.st_nlink, value.st_size,
                   value.st_blocks * 512, value.st_mtime_ns)


@dataclass
class Entry:
    path: Path
    provider: str
    kind: str
    manifest: dict
    files: list[File]
    published: float
    reason: str = "incomplete-or-unreadable"
    delete: bool = False


class Retention:
    def __init__(self, data_root: Path, cache_root: Path, *, now: float | None = None,
                 proc_root: Path = Path("/proc"), runtime_paths: tuple[Path, ...] = (),
                 processes: Processes | None = None, ttl_hours: float = 12,
                 filesystem_prefix: str = ""):
        self.data = data_root.resolve()
        self.cache = cache_root.resolve()
        self.static = self.data / "provider-artifacts" / "static"
        self.now = time.time() if now is None else now
        self.proc_root = proc_root
        self.processes = processes
        self.ttl = ttl_hours * 3600
        self.runtime_paths = runtime_paths
        self.runtime_inodes: set[Inode] = set()
        self.runtime_roots: set[Path] = set()
        self.active_keys: set[tuple[str, str]] = set()
        self.timezones: dict[str, str] = {}
        self.errors: list[str] = []
        self.entries: list[Entry] = []
        self.stack = ExitStack()
        self.locked_providers: set[str] = set()
        self.pointer_snapshot: dict[Path, Path] = {}
        self.checked_locks: set[Path] = set()
        self.locked_entries: set[Path] = set()
        self.filesystem_prefix = filesystem_prefix.rstrip("/")

    def runtime_path(self, value: str, root: Path) -> Path:
        if value.startswith("/"):
            return Path(self.filesystem_prefix + value)
        return root / value

    def reference(self, path: Path) -> None:
        resolved = path.resolve(strict=True)
        self.runtime_roots.add(resolved)
        queue = [resolved]
        seen = set()
        while queue:
            item = queue.pop()
            key = identity(item)
            if item.is_file():
                self.runtime_inodes.add(key)
            elif item.is_dir() and key not in seen:
                seen.add(key)
                queue.extend(item.iterdir())

    def references(self) -> None:
        for pointer in (self.data / "current-release", self.data / "rollback", self.data / "pilot-current"):
            if not pointer.exists() and not pointer.is_symlink():
                continue
            root = pointer.resolve(strict=True)
            self.pointer_snapshot[pointer] = root
            self.reference(root)
            manifest = root / "release.json"
            if not manifest.exists():
                continue
            payload = read_object(manifest)
            for provider, value in payload.get("providers", {}).items():
                structural = value.get("structural", {})
                self.active_keys.add((provider, str(structural.get("artifactKey", ""))))
                temporal = value.get("temporal", {})
                temporal_path = temporal.get("manifestPath")
                if temporal_path:
                    details = read_object(self.runtime_path(temporal_path, root))
                    self.timezones[provider] = str(details.get("dependencies", {}).get("timezone", ""))
                    self.active_keys.add((provider, str(details.get("structuralArtifactKey", ""))))
            # Only published runtime fields are traversed, never input provenance.
            def walk(value, historical=False, key=""):
                if isinstance(value, dict):
                    for name, item in value.items():
                        flag = historical or name in {"inputProvenance", "provenance", "inputArtifacts"}
                        walk(item, False if name == "runtimeDependencies" else flag, name)
                elif isinstance(value, list):
                    for item in value:
                        walk(item, historical, key)
                elif not historical and isinstance(value, str) and key in {"path", "manifestPath"}:
                    self.reference(self.runtime_path(value, root))
            walk(payload)
        for path in self.runtime_paths:
            self.reference(path)

    def lock_busy(self, path: Path) -> bool:
        if path in self.checked_locks:
            return False
        self.checked_locks.add(path)
        try:
            if path.is_symlink() or not path.is_file():
                return False
            handle = self.stack.enter_context(path.open("rb"))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as error:
            self.errors.append(f"lock-check:{path}:{error}")
        return False

    def provider_locks(self, provider: str, directory: Path) -> None:
        for path in {self.cache.parent / provider / ".lock", directory / ".lock"}:
            if self.lock_busy(path):
                self.locked_providers.add(provider)

    def capture(self, path: Path, provider: str, kind: str) -> Entry:
        files = []
        manifest = {}
        published = 0.0
        try:
            def fail_walk(error):
                raise error
            if path.resolve() != path:
                raise ValueError("symlink ancestor")
            for directory, directories, names in os.walk(path, followlinks=False, onerror=fail_walk):
                # Lock metadata is not part of the immutable published payload.
                directories[:] = [name for name in directories if name != ".lock"]
                names = [name for name in names if name != ".lock"]
                if any((Path(directory) / name).is_symlink() for name in directories):
                    raise ValueError("symlink directory")
                files.extend(File.capture(Path(directory) / name) for name in names)
            manifest = read_object(path / "manifest.json")
            if manifest.get("providerID") != provider or manifest.get("status") != "complete":
                raise ValueError("incomplete or provider mismatch")
            main = path / ("partition.json" if kind == "cache" else "provider.sqlite")
            expected = manifest.get("size") if kind == "cache" else manifest.get("sqlite", {}).get("size")
            if not main.is_file() or main.stat().st_size != expected:
                raise ValueError("artifact size mismatch")
            published = max((f.mtime_ns / 1e9 for f in files), default=path.stat().st_mtime)
            if kind == "static":
                # New temporal children do not make an old structural version newer.
                published = (path / "manifest.json").stat().st_mtime
            entry = Entry(path, provider, kind, manifest, files, published, "complete")
        except (OSError, ValueError, TypeError) as error:
            entry = Entry(path, provider, kind, manifest, files, published, f"incomplete-or-unreadable:{error}")
        return entry

    def collect(self) -> None:
        for kind, root in (("cache", self.cache), ("static", self.static)):
            if not root.exists():
                continue
            if root.is_symlink() or not root.is_dir():
                self.errors.append(f"nonregular-root:{root}")
                continue
            for provider in sorted(root.iterdir()):
                if not provider.is_dir() or provider.is_symlink():
                    continue
                self.provider_locks(provider.name, provider)
                paths = provider.glob("*/*/*") if kind == "cache" else provider.iterdir()
                for path in sorted(paths):
                    if path.is_dir() and not path.is_symlink():
                        if self.lock_busy(path / ".lock"):
                            self.locked_entries.add(path)
                        self.entries.append(self.capture(path, provider.name, kind))

    def current_date(self, provider: str):
        try:
            zone = ZoneInfo(self.timezones[provider])
            return datetime.fromtimestamp(self.now, zone).date()
        except (KeyError, ValueError, ZoneInfoNotFoundError):
            # Unknown providers retain the earliest possible current local date.
            return (datetime.fromtimestamp(self.now, timezone.utc) - timedelta(hours=12)).date()

    def classify(self) -> None:
        processes = self.processes or Processes.scan(self.proc_root)
        processes.filesystem_prefix = self.filesystem_prefix
        self.processes = processes
        for entry in self.entries:
            if self.errors or processes.errors:
                entry.reason = "safety-scan-incomplete"
            elif entry.provider in self.locked_providers or entry.path in self.locked_entries or processes.uses(entry.path):
                entry.reason = "running-build"
            elif any(f.key in processes.inodes for f in entry.files):
                entry.reason = "open-fd-or-mmap"
            elif any(entry.path == p or entry.path.is_relative_to(p) or p.is_relative_to(entry.path)
                     for p in self.runtime_roots):
                entry.reason = "runtime-path-reference"
            elif entry.kind == "cache" and any(f.key in self.runtime_inodes for f in entry.files):
                entry.reason = "runtime-inode-reference"
            elif entry.kind == "static" and (entry.provider, str(entry.manifest.get("artifactKey", ""))) in self.active_keys:
                entry.reason = "active-artifact-key"
            elif entry.reason == "complete":
                if entry.kind == "cache":
                    try:
                        day = datetime.strptime(str(entry.manifest["serviceDate"]), "%Y%m%d").date()
                        if day >= self.current_date(entry.provider):
                            entry.reason = "current-or-future-service-date"
                        elif self.now - entry.published < self.ttl:
                            entry.reason = "inside-cache-ttl"
                        else:
                            entry.reason = "expired-unreferenced-cache"
                            entry.delete = True
                    except (KeyError, ValueError):
                        entry.reason = "invalid-service-date"
                else:
                    entry.reason = "superseded-unreferenced-static"
                    entry.delete = True
        for provider in {e.provider for e in self.entries if e.kind == "static"}:
            candidates = [e for e in self.entries if e.kind == "static" and e.provider == provider and e.delete]
            if candidates:
                fallback = max(candidates, key=lambda e: (e.published, str(e.path)))
                fallback.delete = False
                fallback.reason = "latest-fallback"

    def plan(self) -> dict:
        counts = collections.Counter(f.key for e in self.entries if e.delete for f in e.files)
        seen = set()
        rows = []
        for entry in self.entries:
            physical = 0
            for item in entry.files:
                if entry.delete and item.key not in seen and counts[item.key] == item.links:
                    physical += item.allocated
                    seen.add(item.key)
            rows.append(dict(path=str(entry.path), provider=entry.provider, kind=entry.kind,
                decision="DELETE" if entry.delete else "KEEP", reason=entry.reason,
                logical_bytes=sum(f.size for f in entry.files), reclaimable_bytes=physical,
                files=[dict(path=str(f.path), device=f.device, inode=f.inode, links=f.links,
                            logical_bytes=f.size, allocated_bytes=f.allocated) for f in entry.files]))
        return dict(entries=rows, summary=dict(candidates=sum(e.delete for e in self.entries),
            candidate_files=sum(len(e.files) for e in self.entries if e.delete),
            logical_bytes=sum(sum(f.size for f in e.files) for e in self.entries if e.delete),
            reclaimable_bytes=sum(row["reclaimable_bytes"] for row in rows),
            errors=self.errors + (self.processes.errors if self.processes else [])))

    def apply(self, report: dict) -> None:
        # Recheck immutable entries and process state after planning, before unlinking.
        fresh = Processes.scan(self.proc_root)
        fresh.filesystem_prefix = self.filesystem_prefix
        removed = collections.Counter()
        released = 0
        deleted = []
        skipped = []
        for entry in self.entries:
            if not entry.delete:
                continue
            try:
                if fresh.errors or fresh.uses(entry.path) or any(f.key in fresh.inodes for f in entry.files):
                    raise ValueError("process-state-changed")
                if any(p.resolve(strict=True) != target for p, target in self.pointer_snapshot.items()):
                    raise ValueError("runtime-pointer-changed")
                if entry.path.resolve(strict=True) != entry.path:
                    raise ValueError("entry-path-changed")
                refreshed = self.capture(entry.path, entry.provider, entry.kind)
                if refreshed.reason != "complete" or {f.path for f in refreshed.files} != {f.path for f in entry.files}:
                    raise ValueError("entry-contents-changed")
                for item in entry.files:
                    latest = File.capture(item.path)
                    expected_links = item.links - removed[item.key]
                    if latest.key != item.key or latest.links != expected_links or latest.mtime_ns != item.mtime_ns or latest.size != item.size:
                        raise ValueError("entry-identity-changed")
                for item in entry.files:
                    latest = File.capture(item.path)
                    if latest.key != item.key or latest.links != item.links - removed[item.key]:
                        raise ValueError("identity-changed-before-unlink")
                    item.path.unlink()
                    removed[item.key] += 1
                    released += latest.allocated if latest.links == 1 else 0
                    deleted.append(str(item.path))
            except (OSError, ValueError) as error:
                skipped.append(dict(path=str(entry.path), reason=str(error)))
        report["applied"] = dict(deleted=deleted, skipped=skipped, reclaimed_bytes=released)

    def run(self, *, dry_run: bool = True) -> dict:
        with self.stack:
            if not self.cache.exists() and not self.static.exists():
                return dict(entries=[], summary=dict(candidates=0, candidate_files=0, logical_bytes=0, reclaimable_bytes=0, errors=[]))
            for operation in (self.references, self.collect):
                try:
                    operation()
                except (OSError, ValueError, TypeError, AttributeError) as error:
                    self.errors.append(str(error))
            self.classify()
            report = self.plan()
            if not dry_run:
                self.apply(report)
            return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    parser.add_argument("--runtime-path", action="append", default=[], type=Path)
    parser.add_argument("--filesystem-prefix", default="", help="Read-only host filesystem mount prefix")
    parser.add_argument("--cache-ttl-hours", type=float, default=12)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.filesystem_prefix and not args.dry_run:
        parser.error("a host filesystem prefix is supported only for dry-run")
    if args.cache_ttl_hours < 0 or not math.isfinite(args.cache_ttl_hours):
        parser.error("cache TTL must be finite and nonnegative")
    cache = args.cache_root or args.data_root / "cache/gtfs/external-departure-partitions"
    report = Retention(args.data_root, cache, proc_root=args.proc_root,
        runtime_paths=tuple(args.runtime_path), ttl_hours=args.cache_ttl_hours,
        filesystem_prefix=args.filesystem_prefix).run(dry_run=args.dry_run)
    for entry in report["entries"]:
        print("[ArtifactRetention] " + json.dumps(entry, separators=(",", ":")))
    summary = report["summary"] | {"dry_run": args.dry_run}
    if "applied" in report:
        print("[ArtifactRetention] applied=" + json.dumps(report["applied"], separators=(",", ":")))
        summary["reclaimable_bytes"] = report["applied"]["reclaimed_bytes"]
    print("[ArtifactRetention] summary=" + json.dumps(summary, separators=(",", ":")))


if __name__ == "__main__":
    main()
