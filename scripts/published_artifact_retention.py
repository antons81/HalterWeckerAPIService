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


def physical_accounting(paths: tuple[Path, ...], *, retained_names: frozenset[str] = frozenset()) -> dict:
    """Measure allocated bytes across candidate trees without double-counting inodes."""
    normalized: list[Path] = []
    for path in sorted({path.absolute() for path in paths}, key=lambda item: (len(item.parts), str(item))):
        if any(path == parent or parent in path.parents for parent in normalized):
            continue
        normalized.append(path)

    roots: dict[Path, dict[str, object]] = {}
    for root in normalized:
        if root.is_symlink() or not root.is_dir():
            raise ValueError(f"candidate root is missing or not a directory: {root}")
        inodes: dict[Inode, tuple[int, int, int]] = {}
        directory_bytes = 0
        directory_allocations: dict[Path, int] = {}
        retained_directories: set[Path] = set()

        def fail_walk(error: OSError) -> None:
            raise error

        for directory, directories, names in os.walk(root, followlinks=False, onerror=fail_walk):
            parent = Path(directory)
            if any(name in retained_names for name in directories + names):
                retained_directories.add(parent)
            directories[:] = [name for name in directories
                              if name not in retained_names and not (parent / name).is_symlink()]
            names = [name for name in names if name not in retained_names]
            directory_value = Path(directory).lstat()
            if not stat.S_ISDIR(directory_value.st_mode):
                raise ValueError(f"directory changed during scan: {directory}")
            directory_bytes += directory_value.st_blocks * 512
            directory_allocations[parent] = directory_value.st_blocks * 512
            for name in names:
                path = Path(directory) / name
                value = path.lstat()
                if not stat.S_ISREG(value.st_mode):
                    continue
                key = (value.st_dev, value.st_ino)
                previous = inodes.get(key)
                if previous is None:
                    inodes[key] = (value.st_blocks * 512, value.st_nlink, 1)
                else:
                    if previous[:2] != (value.st_blocks * 512, value.st_nlink):
                        raise ValueError(f"inode identity changed during scan: {path}")
                    inodes[key] = (previous[0], previous[1], previous[2] + 1)
        reclaimable_directory_bytes = sum(
            allocated for directory, allocated in directory_allocations.items()
            if not any(directory == retained or directory in retained.parents
                       for retained in retained_directories)
        )
        roots[root] = {"inodes": inodes, "directory_bytes": directory_bytes,
                       "reclaimable_directory_bytes": reclaimable_directory_bytes}

    global_links: collections.Counter[Inode] = collections.Counter()
    global_allocated: dict[Inode, tuple[int, int]] = {}
    for root_data in roots.values():
        inodes = root_data["inodes"]
        for key, (allocated, links, count) in inodes.items():
            global_links[key] += count
            previous = global_allocated.setdefault(key, (allocated, links))
            if previous != (allocated, links):
                raise ValueError(f"inode identity changed across candidate roots: {key}")

    per_root = []
    union_directory_bytes = 0
    union_reclaimable_directory_bytes = 0
    for root, root_data in roots.items():
        inodes = root_data["inodes"]
        directory_bytes = root_data["directory_bytes"]
        reclaimable_directory_bytes = root_data["reclaimable_directory_bytes"]
        union_directory_bytes += directory_bytes
        union_reclaimable_directory_bytes += reclaimable_directory_bytes
        allocated_bytes = sum(value[0] for value in inodes.values())
        shared_candidate_bytes = sum(
            allocated for key, (allocated, _, _) in inodes.items() if global_links[key] > inodes[key][2]
        )
        reclaimable_bytes = sum(
            allocated
            for key, (allocated, links, _) in inodes.items()
            if global_links[key] == links and global_links[key] == inodes[key][2]
        )
        shared_reclaimable_bytes = sum(
            allocated
            for key, (allocated, links, _) in inodes.items()
            if global_links[key] > inodes[key][2] and global_links[key] == links
        )
        shared_anywhere_bytes = sum(
            allocated
            for key, (allocated, links, _) in inodes.items()
            if links > inodes[key][2]
        )
        per_root.append(dict(
            path=str(root),
            allocated_bytes=allocated_bytes,
            directory_allocated_bytes=directory_bytes,
            reclaimable_directory_bytes=reclaimable_directory_bytes,
            shared_with_other_candidate_roots_bytes=shared_candidate_bytes,
            shared_anywhere_bytes=shared_anywhere_bytes,
            shared_union_reclaimable_bytes=shared_reclaimable_bytes,
            exclusive_reclaimable_bytes=reclaimable_bytes,
            standalone_reclaimable_bytes=sum(
                allocated for allocated, links, count in inodes.values() if links == count
            ) + reclaimable_directory_bytes,
            inode_count=len(inodes),
        ))

    union_allocated = sum(value[0] for value in global_allocated.values())
    union_shared = sum(
        allocated for key, (allocated, links) in global_allocated.items() if global_links[key] < links
    )
    union_reclaimable = sum(
        allocated for key, (allocated, links) in global_allocated.items() if global_links[key] == links
    )
    return dict(
        candidate_roots=per_root,
        union_allocated_bytes=union_allocated,
        union_directory_allocated_bytes=union_directory_bytes,
        shared_within_candidate_roots_bytes=sum(
            allocated for key, (allocated, _) in global_allocated.items() if global_links[key] > 1
        ),
        shared_outside_candidate_roots_bytes=union_shared,
        union_reclaimable_bytes=union_reclaimable + union_reclaimable_directory_bytes,
        union_inode_count=len(global_allocated),
    )


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
    cwd_permission_denied: int = 0
    relevant_pids: int = 0
    ignored_unrelated_pids: int = 0
    permission_denied_pids: int = 0
    permission_denied_checks: dict[str, int] = field(default_factory=dict)
    _permission_denied_pid_ids: set[str] = field(default_factory=set, repr=False)
    _recorded_permission_errors: set[tuple[str, str]] = field(default_factory=set, repr=False)
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
        cleaner_uid = os.getuid()
        try:
            processes = list(root.iterdir())
        except OSError as error:
            result.errors.append(str(error))
            return result
        for process in processes:
            if not process.name.isdigit():
                continue
            try:
                status = (process / "status").read_text(encoding="utf-8", errors="replace")
                uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), None)
                if uid_line is None:
                    raise ValueError("missing Uid in proc status")
                uid_values = uid_line.split()[1:]
                if len(uid_values) != 4:
                    raise ValueError("invalid Uid in proc status")
                process_uids = {int(value) for value in uid_values}
                if cleaner_uid != 0 and cleaner_uid not in process_uids:
                    result.ignored_unrelated_pids += 1
                    continue
                result.relevant_pids += 1
                try:
                    result.commands.append((process / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"))
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except OSError as error:
                    cls.record_process_error(result, process.name, process / "cmdline", error)
                try:
                    result.workspaces.append(os.readlink(process / "cwd"))
                except FileNotFoundError:
                    pass
                except OSError as error:
                    cls.record_process_error(result, process.name, process / "cwd", error)
                try:
                    descriptors = (process / "fd").iterdir()
                    for descriptor in descriptors:
                        try:
                            value = descriptor.stat()
                            if stat.S_ISREG(value.st_mode):
                                result.inodes.add((value.st_dev, value.st_ino))
                            elif stat.S_ISDIR(value.st_mode):
                                result.workspaces.append(os.readlink(descriptor))
                        except FileNotFoundError:
                            pass
                        except OSError as error:
                            cls.record_process_error(result, process.name, descriptor, error)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    cls.record_process_error(result, process.name, process / "fd", error)
                try:
                    maps = (process / "maps").read_text().splitlines()
                except FileNotFoundError:
                    maps = []
                except OSError as error:
                    cls.record_process_error(result, process.name, process / "maps", error)
                    maps = []
                for line in maps:
                    try:
                        fields = line.split(None, 5)
                        if len(fields) >= 5 and int(fields[4]):
                            major, minor = (int(v, 16) for v in fields[3].split(":"))
                            result.inodes.add((os.makedev(major, minor), int(fields[4])))
                    except (ValueError, IndexError) as error:
                        cls.record_process_error(result, process.name, process / "maps", error)
            except (FileNotFoundError, ProcessLookupError):
                pass
            except (OSError, ValueError) as error:
                cls.record_process_error(result, process.name, process, error)
        return result

    @staticmethod
    def record_process_error(result: Processes, pid: str, path: Path, error: OSError | ValueError) -> None:
        if isinstance(error, PermissionError):
            check = "fd" if "fd" in path.parts else path.name
            result.permission_denied_checks[check] = result.permission_denied_checks.get(check, 0) + 1
            if pid not in result._permission_denied_pid_ids:
                result._permission_denied_pid_ids.add(pid)
                result.permission_denied_pids += 1
            if check == "cwd":
                result.cwd_permission_denied += 1
            key = pid, check
            if key in result._recorded_permission_errors:
                return
            result._recorded_permission_errors.add(key)
        result.errors.append(f"pid={pid}:{error}")


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
    directories: list[tuple[Path, int, int]] = field(default_factory=list)
    complete: bool = False
    safely_scannable: bool = False
    reason: str = "incomplete-or-unreadable"
    candidate_reason: str = ""
    candidate: bool = False
    delete: bool = False


class Retention:
    def __init__(self, data_root: Path, cache_root: Path, *, now: float | None = None,
                 proc_root: Path = Path("/proc"), runtime_paths: tuple[Path, ...] = (),
                 processes: Processes | None = None, ttl_hours: float = 12,
                 filesystem_prefix: str = ""):
        self.data = data_root.resolve()
        self.cache = cache_root.resolve()
        self.static = self.data / "provider-artifacts" / "static"
        self.transformed = self.cache.parent / "external-build"
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
        self.acquired_locks: set[Path] = set()
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
        if path in self.acquired_locks:
            return False
        if path in self.checked_locks:
            return False
        self.checked_locks.add(path)
        try:
            if path.is_symlink() or not path.is_file():
                return False
            handle = self.stack.enter_context(path.open("rb"))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired_locks.add(path)
        except BlockingIOError:
            return True
        except OSError as error:
            self.errors.append(f"lock-check:{path}:{error}")
        return False

    def lock_busy_now(self, path: Path, lock_stack: ExitStack) -> bool:
        if path in self.acquired_locks or path.is_symlink() or not path.is_file():
            return False
        handle = path.open("rb")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return True
        except OSError:
            handle.close()
            raise
        lock_stack.enter_context(handle)
        self.acquired_locks.add(path)
        return False

    def provider_locks(self, provider: str, directory: Path) -> None:
        for path in {self.cache.parent / provider / ".lock", directory / ".lock"}:
            if self.lock_busy(path):
                self.locked_providers.add(provider)

    def capture(self, path: Path, provider: str, kind: str) -> Entry:
        files = []
        captured_directories: list[tuple[Path, int, int]] = []
        manifest = {}
        published = 0.0
        observed = 0.0
        safely_scannable = False
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
                directory_path = Path(directory)
                directory_stat = directory_path.lstat()
                if not stat.S_ISDIR(directory_stat.st_mode):
                    raise ValueError(f"non-directory entry: {directory_path}")
                captured_directories.append((directory_path, directory_stat.st_dev, directory_stat.st_ino))
                observed = max(observed, directory_stat.st_mtime_ns / 1e9)
                files.extend(File.capture(Path(directory) / name) for name in names)
                if files:
                    observed = max(observed, max(file.mtime_ns / 1e9 for file in files))
            safely_scannable = True
            published = max((f.mtime_ns / 1e9 for f in files), default=path.stat().st_mtime)
            observed = max(observed, published)
            manifest = read_object(path / "manifest.json")
            if manifest.get("providerID") != provider or manifest.get("status") != "complete":
                raise ValueError("incomplete or provider mismatch")
            if kind == "transformed":
                outputs = manifest.get("cachedOutputs")
                if not isinstance(outputs, list) or not outputs:
                    raise ValueError("transformed outputs are missing")
                for output in outputs:
                    if not isinstance(output, dict) or not isinstance(output.get("path"), str):
                        raise ValueError("invalid transformed output")
                    relative = Path(output["path"])
                    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                        raise ValueError("unsafe transformed output path")
                    main = path / relative
                    expected = output.get("size")
                    if (not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0
                            or not main.is_file() or main.stat().st_size != expected):
                        raise ValueError("transformed output size mismatch")
            else:
                main = path / ("partition.json" if kind == "cache" else "provider.sqlite")
                expected = manifest.get("size") if kind == "cache" else manifest.get("sqlite", {}).get("size")
                if not main.is_file() or main.stat().st_size != expected:
                    raise ValueError("artifact size mismatch")
            published = max((f.mtime_ns / 1e9 for f in files), default=path.stat().st_mtime)
            if kind == "static":
                # New temporal children do not make an old structural version newer.
                published = (path / "manifest.json").stat().st_mtime
            entry = Entry(path, provider, kind, manifest, files, published,
                          captured_directories, True, True, "complete")
        except (OSError, ValueError, TypeError) as error:
            # If tree enumeration completed, an unreadable/missing manifest is still
            # a safely enumerable orphan. Symlinks and walk/stat failures stay protected.
            entry = Entry(path, provider, kind, manifest, files, max(published, observed),
                          captured_directories, False, safely_scannable,
                          f"incomplete-or-unreadable:{error}")
        return entry

    def collect(self) -> None:
        for kind, root in (("cache", self.cache), ("static", self.static),
                           ("transformed", self.transformed)):
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
            if self.errors or (os.getuid() == 0 and processes.errors):
                entry.reason = "safety-scan-incomplete"
            elif entry.provider in self.locked_providers or entry.path in self.locked_entries or processes.uses(entry.path):
                entry.reason = "running-build"
            elif any(f.key in processes.inodes for f in entry.files):
                entry.reason = "open-fd-or-mmap"
            elif any(entry.path == p or entry.path.is_relative_to(p) or p.is_relative_to(entry.path)
                     for p in self.runtime_roots):
                entry.reason = "runtime-path-reference"
            elif entry.kind in {"cache", "transformed"} and any(f.key in self.runtime_inodes for f in entry.files):
                entry.reason = "runtime-inode-reference"
            elif entry.kind == "static" and (entry.provider, str(entry.manifest.get("artifactKey", ""))) in self.active_keys:
                entry.reason = "active-artifact-key"
            elif entry.kind == "static" and (entry.provider, entry.path.name) in self.active_keys:
                entry.reason = "active-artifact-key"
            elif not entry.complete:
                if entry.kind == "transformed":
                    # Only validated superseded generations qualify for this policy.
                    entry.reason = "unvalidated-transformed-generation"
                    continue
                if not entry.safely_scannable:
                    continue
                if entry.kind == "cache":
                    try:
                        day = datetime.strptime(entry.path.parent.name, "%Y%m%d").date()
                    except ValueError:
                        entry.reason = "invalid-service-date"
                        continue
                    if day >= self.current_date(entry.provider):
                        entry.reason = "current-or-future-service-date"
                        continue
                if self.now - entry.published < self.ttl:
                    entry.reason = "incomplete-too-new"
                else:
                    entry.candidate = True
                    entry.candidate_reason = "stale-incomplete-orphan"
                    entry.reason = entry.candidate_reason
            elif entry.complete:
                if entry.kind == "cache":
                    try:
                        day = datetime.strptime(str(entry.manifest["serviceDate"]), "%Y%m%d").date()
                        if day >= self.current_date(entry.provider):
                            entry.reason = "current-or-future-service-date"
                        elif self.now - entry.published < self.ttl:
                            entry.reason = "inside-cache-ttl"
                        else:
                            entry.candidate = True
                            entry.candidate_reason = "expired-unreferenced-cache"
                            entry.reason = entry.candidate_reason
                    except (KeyError, ValueError):
                        entry.reason = "invalid-service-date"
                elif entry.kind == "transformed":
                    if self.now - entry.published < self.ttl:
                        entry.reason = "inside-transformed-ttl"
                    else:
                        entry.candidate = True
                        entry.candidate_reason = "superseded-unreferenced-transformed"
                        entry.reason = entry.candidate_reason
                else:
                    entry.candidate = True
                    entry.candidate_reason = "superseded-unreferenced-static"
                    entry.reason = entry.candidate_reason
        for provider in {e.provider for e in self.entries if e.kind == "static"}:
            candidates = [e for e in self.entries
                          if e.kind == "static" and e.provider == provider and e.candidate and e.complete]
            if candidates:
                fallback = max(candidates, key=lambda e: (e.published, str(e.path)))
                fallback.candidate = False
                fallback.candidate_reason = ""
                fallback.delete = False
                fallback.reason = "latest-fallback"
        for provider in {entry.provider for entry in self.entries if entry.kind == "transformed"}:
            complete = [entry for entry in self.entries
                        if entry.kind == "transformed" and entry.provider == provider and entry.complete]
            if complete:
                latest = max(complete, key=lambda entry: (entry.published, str(entry.path)))
                if latest.candidate:
                    latest.candidate = False
                    latest.candidate_reason = ""
                    latest.reason = "latest-transformed-generation"
        for entry in self.entries:
            if entry.candidate:
                entry.delete = True

    def plan(self, physical_roots: tuple[Path, ...] = ()) -> dict:
        counts = collections.Counter(f.key for e in self.entries if e.candidate for f in e.files)
        seen = set()
        rows = []
        for entry in self.entries:
            physical = 0
            for item in entry.files:
                if entry.candidate and item.key not in seen and counts[item.key] == item.links:
                    physical += item.allocated
                    seen.add(item.key)
            rows.append(dict(path=str(entry.path), provider=entry.provider, kind=entry.kind,
                decision="DELETE" if entry.delete else "BLOCKED" if entry.candidate else "KEEP",
                candidate=entry.candidate, candidate_reason=entry.candidate_reason,
                reason=entry.reason,
                logical_bytes=sum(f.size for f in entry.files), reclaimable_bytes=physical,
                files=[dict(path=str(f.path), device=f.device, inode=f.inode, links=f.links,
                            logical_bytes=f.size, allocated_bytes=f.allocated) for f in entry.files]))
        reasons = collections.Counter(e.reason for e in self.entries if not e.delete)
        summary = dict(candidates=sum(e.candidate for e in self.entries),
            deletable_candidates=sum(e.delete for e in self.entries),
            safety_blocked_candidates=sum(e.candidate and not e.delete for e in self.entries),
            candidate_files=sum(len(e.files) for e in self.entries if e.candidate),
            logical_bytes=sum(sum(f.size for f in e.files) for e in self.entries if e.candidate),
            reclaimable_bytes=sum(row["reclaimable_bytes"] for row in rows),
            skipped_by_reason=dict(sorted(reasons.items())),
            process_scan=dict(
                relevant_pids=self.processes.relevant_pids if self.processes else 0,
                ignored_unrelated_pids=self.processes.ignored_unrelated_pids if self.processes else 0,
                permission_denied_pids=self.processes.permission_denied_pids if self.processes else 0,
                permission_denied_checks=self.processes.permission_denied_checks if self.processes else {},
                cwd_permission_denied_pids=self.processes.cwd_permission_denied if self.processes else 0,
            ),
            errors=self.errors + (self.processes.errors if self.processes else []))
        try:
            deleted_entries = [entry.path for entry in self.entries if entry.delete]
            candidate_paths = set(deleted_entries)
            reclaimable_date_directories = []
            date_directory_candidates = []
            if self.cache.is_dir() and not self.cache.is_symlink():
                for provider in sorted(self.cache.iterdir()):
                    if provider.is_symlink() or not provider.is_dir():
                        continue
                    for city in sorted(provider.iterdir()):
                        if city.is_symlink() or not city.is_dir():
                            continue
                        for day_directory in sorted(city.iterdir()):
                            if day_directory.is_symlink() or not day_directory.is_dir():
                                continue
                            if provider.name in self.locked_providers or (self.processes and self.processes.uses(day_directory)):
                                continue
                            if any(day_directory == root or day_directory.is_relative_to(root)
                                   or root.is_relative_to(day_directory)
                                   for root in self.runtime_roots):
                                continue
                            try:
                                service_date = datetime.strptime(day_directory.name, "%Y%m%d").date()
                            except ValueError:
                                continue
                            if service_date >= self.current_date(provider.name):
                                continue
                            children = list(day_directory.iterdir())
                            if not children or all(
                                child in candidate_paths and child.is_dir() and not child.is_symlink()
                                for child in children
                            ):
                                reclaimable_date_directories.append(day_directory)
                                value = day_directory.lstat()
                                date_directory_candidates.append(dict(
                                    path=str(day_directory), device=value.st_dev, inode=value.st_ino
                                ))
            summary["physical_accounting"] = physical_accounting(
                tuple(deleted_entries + reclaimable_date_directories) + physical_roots,
                retained_names=frozenset({".lock"}),
            )
        except (OSError, ValueError) as error:
            summary["physical_accounting"] = dict(errors=[str(error)])
            summary["errors"].append(f"physical-accounting:{error}")
        return dict(entries=rows, summary=summary,
                    reclaimable_date_directories=date_directory_candidates)

    def apply(self, report: dict) -> None:
        # Process scan results are diagnostic only; filesystem references and locks
        # are refreshed before applying the immutable plan.
        fresh = Processes.scan(self.proc_root)
        fresh.filesystem_prefix = self.filesystem_prefix
        refreshed_state = Retention(
            self.data, self.cache, now=self.now, proc_root=self.proc_root,
            runtime_paths=self.runtime_paths, processes=fresh,
            ttl_hours=self.ttl / 3600, filesystem_prefix=self.filesystem_prefix,
        )
        refreshed_state.acquired_locks = self.acquired_locks.copy()
        for operation in (refreshed_state.references, refreshed_state.collect):
            try:
                operation()
            except (OSError, ValueError, TypeError, AttributeError) as error:
                refreshed_state.errors.append(str(error))
        refreshed_state.classify()
        current_entries = {entry.path: entry for entry in refreshed_state.entries}
        apply_locks = refreshed_state.stack.pop_all()
        self.acquired_locks.update(refreshed_state.acquired_locks)
        removed = collections.Counter()
        released = 0
        deleted = []
        deleted_directories = []
        skipped = []
        retained_directories = []
        report["summary"].setdefault("process_scan", {})["apply_cwd_permission_denied_pids"] = fresh.cwd_permission_denied
        report["summary"]["process_scan"].update(
            apply_relevant_pids=fresh.relevant_pids,
            apply_ignored_unrelated_pids=fresh.ignored_unrelated_pids,
            apply_permission_denied_pids=fresh.permission_denied_pids,
            apply_permission_denied_checks=fresh.permission_denied_checks,
        )
        for entry in self.entries:
            if not entry.delete:
                continue
            try:
                current = current_entries.get(entry.path)
                if refreshed_state.errors or (os.getuid() == 0 and fresh.errors):
                    raise ValueError("filesystem-safety-state-incomplete")
                if current is None or not current.delete:
                    raise ValueError("candidate-no-longer-eligible")
                provider_root = {"cache": self.cache, "static": self.static,
                                 "transformed": self.transformed}[entry.kind]
                provider_directory = provider_root / entry.provider
                lock_paths = {
                    self.cache.parent / entry.provider / ".lock",
                    provider_directory / ".lock",
                    entry.path / ".lock",
                }
                if any(self.lock_busy_now(path, apply_locks) for path in lock_paths):
                    raise ValueError("lock-became-active")
                if any(p.resolve(strict=True) != target for p, target in self.pointer_snapshot.items()):
                    raise ValueError("runtime-pointer-changed")
                if entry.path.resolve(strict=True) != entry.path:
                    raise ValueError("entry-path-changed")
                refreshed = self.capture(entry.path, entry.provider, entry.kind)
                refreshed_is_eligible = (
                    refreshed.complete if entry.complete else
                    refreshed.safely_scannable and not refreshed.complete
                    and self.now - refreshed.published >= self.ttl
                )
                if not refreshed_is_eligible or {f.path for f in refreshed.files} != {f.path for f in entry.files}:
                    raise ValueError("entry-contents-changed")
                if not current.candidate:
                    raise ValueError("candidate-no-longer-eligible")
                if refreshed.directories != entry.directories:
                    raise ValueError("entry-directories-changed")
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
                for directory, device, inode in sorted(
                    entry.directories, key=lambda value: len(value[0].parts), reverse=True
                ):
                    try:
                        latest_directory = directory.lstat()
                        if (latest_directory.st_dev, latest_directory.st_ino) != (device, inode):
                            retained_directories.append(dict(path=str(directory), reason="directory-identity-changed"))
                            continue
                        directory.rmdir()
                        released += latest_directory.st_blocks * 512
                        deleted_directories.append(str(directory))
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        # Non-empty directories can contain lock metadata or files
                        # introduced after the snapshot; never recurse into them.
                        retained_directories.append(dict(path=str(directory), reason=f"directory-not-empty-or-unavailable:{error}"))
            except (OSError, ValueError) as error:
                skipped.append(dict(path=str(entry.path), reason=str(error)))
        if not refreshed_state.errors and not fresh.errors:
            for candidate in report.get("reclaimable_date_directories", []):
                day_directory = Path(candidate["path"])
                try:
                    provider = day_directory.parents[1]
                    provider_name = provider.name
                    service_date = datetime.strptime(day_directory.name, "%Y%m%d").date()
                    value = day_directory.lstat()
                    if (day_directory.is_symlink() or not stat.S_ISDIR(value.st_mode)
                            or (value.st_dev, value.st_ino) != (candidate["device"], candidate["inode"])
                            or not day_directory.is_relative_to(self.cache)
                            or service_date >= refreshed_state.current_date(provider_name)
                            or provider_name in refreshed_state.locked_providers
                            or any(day_directory == root or day_directory.is_relative_to(root)
                                   or root.is_relative_to(day_directory)
                                   for root in refreshed_state.runtime_roots)
                            or fresh.uses(day_directory)
                            or any(self.lock_busy_now(path, apply_locks) for path in (
                                self.cache.parent / provider_name / ".lock", provider / ".lock"
                            ))):
                        continue
                    day_directory.rmdir()
                    released += value.st_blocks * 512
                    deleted_directories.append(str(day_directory))
                except (OSError, ValueError):
                    # Only remove empty date containers; nested data is never traversed here.
                    continue
        report["applied"] = dict(deleted=deleted, skipped=skipped,
                                  deleted_directories=deleted_directories,
                                  retained_directories=retained_directories, reclaimed_bytes=released)
        apply_locks.close()

    def run(self, *, dry_run: bool = True, physical_roots: tuple[Path, ...] = ()) -> dict:
        with self.stack:
            if not any(root.exists() for root in (self.cache, self.static, self.transformed)):
                try:
                    physical = physical_accounting(physical_roots)
                    errors = []
                except (OSError, ValueError) as error:
                    physical = dict(errors=[str(error)])
                    errors = [f"physical-accounting:{error}"]
                return dict(entries=[], summary=dict(candidates=0, candidate_files=0,
                    logical_bytes=0, reclaimable_bytes=0, physical_accounting=physical,
                    errors=errors))
            for operation in (self.references, self.collect):
                try:
                    operation()
                except (OSError, ValueError, TypeError, AttributeError) as error:
                    self.errors.append(str(error))
            self.classify()
            report = self.plan(physical_roots)
            if not dry_run:
                self.apply(report)
            return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    parser.add_argument("--runtime-path", action="append", default=[], type=Path)
    parser.add_argument("--physical-root", action="append", default=[], type=Path,
                        help="Additional candidate tree included in inode-union accounting")
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
        filesystem_prefix=args.filesystem_prefix).run(dry_run=args.dry_run,
            physical_roots=tuple(args.physical_root))
    for entry in report["entries"]:
        print("[ArtifactRetention] " + json.dumps(entry, separators=(",", ":")))
    summary = report["summary"] | {"dry_run": args.dry_run}
    if "applied" in report:
        print("[ArtifactRetention] applied=" + json.dumps(report["applied"], separators=(",", ":")))
        summary["reclaimable_bytes"] = report["applied"]["reclaimed_bytes"]
    print("[ArtifactRetention] summary=" + json.dumps(summary, separators=(",", ":")))


if __name__ == "__main__":
    main()
