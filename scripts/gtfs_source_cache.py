"""Persistent cache for validated raw GTFS source artifacts."""

from __future__ import annotations

import fcntl
import errno
import hashlib
import json
import os
import re
import socket
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit


DEFAULT_CACHE_ROOT = Path("/srv/haltewecker/cache/gtfs")
REQUIRED_GTFS_FILES = {"stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}
DEFAULT_REQUEST_HEADERS = {"User-Agent": "HalteWeckerStopPipeline/1.0"}
SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
TRANSIENT_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
TRANSIENT_ERRNOS = {
    error_number
    for error_number in (
        errno.EAGAIN,
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.ETIMEDOUT,
    )
    if errno is not None
}


@dataclass(frozen=True)
class ArtifactResult:
    source_id: str
    path: Path
    status: str
    reason: str = ""
    state: dict[str, object] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprint_matches(state: Mapping[str, object], headers: Mapping[str, str]) -> bool:
    etag = headers.get("etag", "")
    last_modified = headers.get("last-modified", "")
    content_length = headers.get("content-length", "")
    if etag and state.get("etag") == etag:
        return True
    if last_modified and state.get("lastModified") == last_modified:
        previous_length = state.get("contentLength")
        return not content_length or str(previous_length) == content_length
    return False


def _headers(response: object) -> dict[str, str]:
    raw = getattr(response, "headers", {})
    return {
        key.lower(): str(value).strip()
        for key, value in raw.items()
        if value is not None
    }


def _state_url(url: str, explicit_url: str | None) -> str:
    """Keep cache metadata free of query-string credentials and signed tokens."""
    if explicit_url is not None:
        return explicit_url
    parsed = urlsplit(url)
    if parsed.scheme and parsed.netloc:
        safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
        return parsed._replace(netloc=safe_netloc, query="", fragment="").geturl()
    return url


def _is_transient_error(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in TRANSIENT_HTTP_STATUS_CODES
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, BaseException):
            return _is_transient_error(reason)
        reason_text = str(reason).casefold()
        return any(
            marker in reason_text
            for marker in ("timeout", "temporar", "connection reset", "refused")
        )
    if isinstance(error, (TimeoutError, socket.timeout, ConnectionError)):
        return True
    return isinstance(error, OSError) and getattr(error, "errno", None) in TRANSIENT_ERRNOS


def _error_summary(error: BaseException) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return _error_summary(error.reason) if isinstance(error.reason, BaseException) else "network error"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "connection failure"
    return type(error).__name__


def validate_gtfs_archive(
    path: Path,
    validator: Callable[[Path], None] | None = None,
) -> tuple[str, int]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"GTFS artifact is missing or empty: {path}")
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            raise ValueError(f"GTFS artifact has invalid ZIP member: {bad_member}")
        names = set(archive.namelist())
        missing = REQUIRED_GTFS_FILES - names
        if missing:
            raise ValueError(
                f"GTFS artifact is missing required files: {', '.join(sorted(missing))}"
            )
        for name in REQUIRED_GTFS_FILES:
            if archive.getinfo(name).file_size == 0:
                raise ValueError(f"GTFS artifact contains empty file: {name}")
    if validator is not None:
        validator(path)
    digest_builder = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest_builder.update(chunk)
    digest = digest_builder.hexdigest()
    return digest, path.stat().st_size


class GTFSArtifactCache:
    def __init__(self, root: Path | str = DEFAULT_CACHE_ROOT) -> None:
        self.root = Path(root)

    def _paths(self, source_id: str) -> tuple[Path, Path, Path]:
        if not SAFE_SOURCE_ID.fullmatch(source_id):
            raise ValueError(f"Invalid GTFS source id: {source_id!r}")
        directory = self.root / source_id
        return directory / "current.zip", directory / "state.json", directory / ".lock"

    def _read_state(self, path: Path) -> dict[str, object] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _state_artifact_is_valid(
        self,
        artifact: Path,
        state: Mapping[str, object] | None,
        validator: Callable[[Path], None] | None = None,
    ) -> bool:
        if not state or state.get("validated") is not True:
            return False
        try:
            digest, size = validate_gtfs_archive(artifact, validator=validator)
        except (OSError, ValueError, zipfile.BadZipFile):
            return False
        return digest == state.get("sha256") and size == int(state.get("size", -1))

    def _write_state(self, path: Path, state: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(state, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _activate_candidate(
        self,
        candidate: Path,
        artifact: Path,
        state_path: Path,
        state: dict[str, object],
    ) -> None:
        fd = os.open(candidate, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(candidate, artifact)
        self._fsync_directory(artifact.parent)
        self._write_state(state_path, state)

    def resolve(
        self,
        source_id: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        source_version: Mapping[str, object] | None = None,
        allow_stale: bool = False,
        seed_path: Path | None = None,
        state_url: str | None = None,
        minimum_size: int | None = None,
        metadata_probe: bool = True,
        retry_attempts: int = 1,
        validator: Callable[[Path], None] | None = None,
    ) -> ArtifactResult:
        if not isinstance(retry_attempts, int) or not 1 <= retry_attempts <= 5:
            raise ValueError("retry_attempts must be an integer between 1 and 5")
        artifact, state_path, lock_path = self._paths(source_id)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        request_headers = dict(DEFAULT_REQUEST_HEADERS)
        request_headers.update(
            {str(key): str(value) for key, value in (headers or {}).items()}
        )
        parsed = urlsplit(url)
        started = time.monotonic()

        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = self._read_state(state_path)
            valid_cache = self._state_artifact_is_valid(artifact, state, validator)

            if not valid_cache and seed_path is not None and seed_path.is_file():
                fd, temporary_name = tempfile.mkstemp(prefix=".seed-", suffix=".zip", dir=artifact.parent)
                os.close(fd)
                candidate = Path(temporary_name)
                try:
                    shutil.copyfile(seed_path, candidate)
                    digest, size = validate_gtfs_archive(candidate, validator=validator)
                    if minimum_size is not None and size < max(1024, minimum_size // 2):
                        raise ValueError(f"GTFS artifact is smaller than expected for {source_id}")
                    new_state = self._state(source_id, _state_url(url, state_url), source_version, {}, digest, size)
                    self._activate_candidate(candidate, artifact, state_path, new_state)
                    valid_cache = True
                    state = new_state
                except Exception:
                    candidate.unlink(missing_ok=True)

            if valid_cache and source_version is not None:
                previous_version = state.get("sourceVersion") if state else None
                if previous_version == dict(source_version) and state.get("url") == _state_url(url, state_url):
                    return ArtifactResult(source_id, artifact, "unchanged", "source-version", state)

            if parsed.scheme in ("", "file"):
                local_path = Path(parsed.path if parsed.scheme == "file" else url)
                if not local_path.is_file():
                    if valid_cache and allow_stale:
                        return ArtifactResult(source_id, artifact, "preserved-stale", "local source unavailable", state)
                    raise FileNotFoundError(local_path)
                if valid_cache and local_path.resolve() == artifact.resolve():
                    return ArtifactResult(source_id, artifact, "unchanged", "local artifact", state)
                fd, temporary_name = tempfile.mkstemp(prefix=".download-", suffix=".zip", dir=artifact.parent)
                os.close(fd)
                candidate = Path(temporary_name)
                try:
                    shutil.copyfile(local_path, candidate)
                    digest, size = validate_gtfs_archive(candidate, validator=validator)
                    new_state = self._state(source_id, _state_url(url, None), source_version, {}, digest, size)
                    self._activate_candidate(candidate, artifact, state_path, new_state)
                    return ArtifactResult(source_id, artifact, "updated", f"local source ({time.monotonic() - started:.2f}s)", new_state)
                except Exception:
                    candidate.unlink(missing_ok=True)
                    if valid_cache and allow_stale:
                        return ArtifactResult(source_id, artifact, "preserved-stale", "local artifact validation failed", state)
                    raise

            if metadata_probe:
                try:
                    metadata_request = urllib.request.Request(url, headers=request_headers, method="HEAD")
                    with urllib.request.urlopen(metadata_request, timeout=30) as response:
                        metadata = _headers(response)
                    if valid_cache and (state or {}).get("url") == _state_url(url, state_url) and _fingerprint_matches(state or {}, metadata):
                        return ArtifactResult(source_id, artifact, "unchanged", "remote metadata", state)
                except urllib.error.HTTPError as error:
                    if error.code == 304 and valid_cache:
                        return ArtifactResult(source_id, artifact, "unchanged", "HTTP 304", state)
                    metadata = {}
                except (OSError, ValueError):
                    metadata = {}
            else:
                metadata = {}

            request_headers.update(
                {
                    key: value
                    for key, value in {
                        "If-None-Match": str((state or {}).get("etag", "")),
                        "If-Modified-Since": str((state or {}).get("lastModified", "")),
                    }.items()
                    if value
                }
            )
            fd, temporary_name = tempfile.mkstemp(prefix=".download-", suffix=".zip", dir=artifact.parent)
            os.close(fd)
            candidate = Path(temporary_name)
            last_error: BaseException | None = None
            for attempt in range(1, retry_attempts + 1):
                try:
                    with candidate.open("wb") as output:
                        request = urllib.request.Request(url, headers=request_headers)
                        with urllib.request.urlopen(request, timeout=180) as response:
                            status = int(getattr(response, "status", 200))
                            if status in TRANSIENT_HTTP_STATUS_CODES:
                                raise urllib.error.HTTPError(
                                    url,
                                    status,
                                    f"HTTP {status}",
                                    getattr(response, "headers", {}),
                                    None,
                                )
                            if status >= 400:
                                raise urllib.error.HTTPError(
                                    url,
                                    status,
                                    f"HTTP {status}",
                                    getattr(response, "headers", {}),
                                    None,
                                )
                            if status == 304 and valid_cache:
                                return ArtifactResult(source_id, artifact, "unchanged", "HTTP 304", state)
                            shutil.copyfileobj(response, output)
                            response_headers = _headers(response)
                        output.flush()
                        os.fsync(output.fileno())
                    digest, size = validate_gtfs_archive(candidate, validator=validator)
                    if minimum_size is not None and size < max(1024, minimum_size // 2):
                        raise ValueError(f"GTFS artifact is smaller than expected for {source_id}")
                    if valid_cache and digest == state.get("sha256") and size == int(state.get("size", -1)):
                        return ArtifactResult(source_id, artifact, "unchanged", "checksum", state)
                    new_state = self._state(source_id, _state_url(url, state_url), source_version, response_headers, digest, size)
                    if candidate.parent != artifact.parent:
                        raise RuntimeError("GTFS candidate and cache artifact are on different filesystems")
                    self._activate_candidate(candidate, artifact, state_path, new_state)
                    return ArtifactResult(source_id, artifact, "updated", "downloaded", new_state)
                except Exception as error:
                    last_error = error
                    candidate.unlink(missing_ok=True)
                    if not _is_transient_error(error) or attempt == retry_attempts:
                        break
                    print(
                        f"[GTFSCache] source={source_id} attempt={attempt} "
                        f"failed: {_error_summary(error)}"
                    )
                    time.sleep(min(0.5 * attempt, 2.0))
            candidate.unlink(missing_ok=True)
            if valid_cache and allow_stale:
                reason = _error_summary(last_error) if last_error else "download failure"
                return ArtifactResult(source_id, artifact, "preserved-stale", reason, state)
            if last_error is not None:
                raise last_error
            raise RuntimeError(f"GTFS download failed for {source_id}")
    @staticmethod
    def _state(
        source_id: str,
        url: str,
        source_version: Mapping[str, object] | None,
        headers: Mapping[str, str],
        digest: str,
        size: int,
    ) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "sourceID": source_id,
            "url": url,
            "artifact": "current.zip",
            "etag": headers.get("etag") or None,
            "lastModified": headers.get("last-modified") or None,
            "contentLength": int(headers["content-length"]) if headers.get("content-length", "").isdigit() else None,
            "sourceVersion": dict(source_version) if source_version is not None else None,
            "sha256": digest,
            "size": size,
            "validated": True,
            "validatedAt": _now(),
        }
