#!/usr/bin/env python3
"""Switch the HalteWecker monetization strategy in the static app config."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, TextIO


ALLOWED_MONETIZATION_FLOWS = ("app_trial", "storekit_trial")
CONFIG_PATH_ENVIRONMENT_VARIABLE = "HALTEWECKER_MONETIZATION_CONFIG_PATH"
DEFAULT_CONFIG_PATH = Path("/srv/haltewecker/config/haltewecker.json")
DEFAULT_CONFIG_VERSION = 1


class MonetizationConfigError(ValueError):
    """Raised when the existing runtime configuration cannot be updated safely."""


def resolve_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the production path, with a test-only environment override."""
    values = os.environ if environ is None else environ
    configured_path = values.get(CONFIG_PATH_ENVIRONMENT_VARIABLE)
    return Path(configured_path) if configured_path else DEFAULT_CONFIG_PATH


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw_document = path.read_text(encoding="utf-8")
    except OSError as error:
        raise MonetizationConfigError(f"Unable to read config: {path}: {error}") from error

    try:
        document = json.loads(raw_document)
    except json.JSONDecodeError as error:
        raise MonetizationConfigError(
            f"Config is not valid JSON: {path}: {error.msg}"
        ) from error

    if not isinstance(document, dict):
        raise MonetizationConfigError("Config JSON must contain an object at the top level")
    return document


def _validate_flow(flow: str) -> None:
    if flow not in ALLOWED_MONETIZATION_FLOWS:
        allowed = ", ".join(ALLOWED_MONETIZATION_FLOWS)
        raise MonetizationConfigError(
            f"Unknown monetization flow {flow!r}; allowed values: {allowed}"
        )


def _encode_json(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")


def _validate_written_config(path: Path, expected_flow: str) -> None:
    document = _load_json(path)
    if document.get("monetizationFlow") != expected_flow:
        raise MonetizationConfigError(
            "Written config did not contain the requested monetization flow"
        )


def _fsync_directory(directory: Path) -> None:
    try:
        directory_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return

    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_atomically(path: Path, document: Mapping[str, Any]) -> None:
    encoded_document = _encode_json(document)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    temporary_path: Path | None = None

    try:
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, existing_mode)
        with os.fdopen(temporary_fd, "wb") as temporary_file:
            temporary_file.write(encoded_document)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        _validate_written_config(temporary_path, str(document["monetizationFlow"]))
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def update_monetization_flow(path: Path, flow: str) -> None:
    """Update only monetizationFlow and replace the file atomically."""
    _validate_flow(flow)
    document = _load_json(path) if path.exists() else {"version": DEFAULT_CONFIG_VERSION}
    updated_document = dict(document)
    updated_document["monetizationFlow"] = flow
    _write_atomically(path, updated_document)
    _validate_written_config(path, flow)


def _write_error(stderr: TextIO, message: str) -> int:
    print(message, file=stderr)
    return 1


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the CLI and return a process exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    if len(arguments) != 1:
        return _write_error(
            stderr,
            "Usage: haltewecker-monetization {app_trial|storekit_trial}",
        )

    flow = arguments[0]
    try:
        _validate_flow(flow)
        config_path = resolve_config_path(environ)
        update_monetization_flow(config_path, flow)
    except MonetizationConfigError as error:
        return _write_error(stderr, str(error))
    except OSError as error:
        return _write_error(stderr, f"Unable to update config: {error}")

    print(f"HalteWecker monetization flow: {flow}", file=stdout)
    print(f"Config: {config_path}", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
