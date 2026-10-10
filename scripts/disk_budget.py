#!/usr/bin/env python3
"""Stop an owned build process before it consumes the disk reserve.

Created by Anton on 2026-10-10.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from pathlib import Path

GIB = 1024 ** 3
MINIMUM_FREE_GIB = 20


def free_bytes(root: Path) -> int:
    filesystem = os.statvfs(root)
    return filesystem.f_bavail * filesystem.f_frsize


def terminate_build(process: subprocess.Popen) -> None:
    """Terminate only the process group created by this guard."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_guarded(command: list[str], root: Path, minimum_gib: int = 20, *, interval: float = 0.25) -> int:
    if minimum_gib < MINIMUM_FREE_GIB:
        raise ValueError("disk reserve cannot be lower than 20 GiB")
    # A buffer leaves room for buffered writes and process termination.
    threshold = (minimum_gib + 5) * GIB
    minimum_observed = free_bytes(root)
    if minimum_observed < threshold:
        print("[DiskBudget] FAIL: insufficient free space before build", flush=True)
        return 75
    process = subprocess.Popen(command, start_new_session=True)
    previous_handlers = {}

    def interrupted(signum, _frame):
        raise InterruptedError(f"build interrupted by signal {signum}")

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        while True:
            minimum_observed = min(minimum_observed, free_bytes(root))
            if minimum_observed < threshold:
                print("[DiskBudget] FAIL: disk reserve reached; stopping candidate build", flush=True)
                terminate_build(process)
                return 75
            status = process.poll()
            if status is not None:
                return status if status >= 0 else 128 - status
            time.sleep(interval)
    finally:
        if process.poll() is None:
            terminate_build(process)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        print(f"[DiskBudget] minimum_free_bytes={minimum_observed} reserve_gib={minimum_gib}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--minimum-free-gib", type=int, default=20)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a build command is required")
    raise SystemExit(run_guarded(command, args.root, args.minimum_free_gib))


if __name__ == "__main__":
    main()
