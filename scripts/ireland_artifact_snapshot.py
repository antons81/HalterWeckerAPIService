"""Capture and validate release-local snapshots of the Ireland GTFS tree."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

IRELAND_SOURCE_ID = "ireland"
IRELAND_SNAPSHOT_RELATIVE_PATH = Path("external-artifacts") / IRELAND_SOURCE_ID
DEFAULT_CAPTURE_ATTEMPTS = 3
TEMP_DIRECTORY_ATTEMPTS = 16
COPY_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class IrelandSnapshot:
    path: Path
    sha256: str
    size: int


class _CaptureRetry(Exception):
    """Internal signal for a source generation change during capture."""


def _entry_type_from_mode(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode):
        return "block device"
    if stat.S_ISCHR(mode):
        return "character device"
    return "special filesystem object"


def _check_fd_platform_support() -> None:
    required = (os.mkdir, os.open, os.rename, os.rmdir, os.stat, os.unlink)
    if not all(function in os.supports_dir_fd for function in required):
        raise ValueError("Ireland snapshot requires dir_fd filesystem operations")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("Ireland snapshot requires O_DIRECTORY and O_NOFOLLOW")
    if os.stat not in os.supports_follow_symlinks:
        raise ValueError("Ireland snapshot requires no-follow stat support")


def _open_directory(
    path: str | Path,
    *,
    dir_fd: int | None = None,
    label: str,
) -> int:
    _check_fd_platform_support()
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=dir_fd,
        )
    except OSError as error:
        raise ValueError(f"{label} cannot be opened safely: {path}") from error
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError(f"{label} must be a regular directory: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _entry_type_at(name: str, directory_fd: int) -> str | None:
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _entry_type_from_mode(metadata.st_mode)


def _validate_directory_fd(directory_fd: int, *, label: str) -> None:
    if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
        raise ValueError(f"{label} must be a regular directory")
    file_count = 0
    directories = [directory_fd]
    try:
        while directories:
            current_fd = directories.pop()
            try:
                entries = sorted(os.scandir(current_fd), key=lambda entry: entry.name)
                for entry in entries:
                    entry_type = _entry_type_at(entry.name, current_fd)
                    if entry_type == "directory":
                        directories.append(
                            _open_directory(entry.name, dir_fd=current_fd, label=label)
                        )
                    elif entry_type == "regular file":
                        file_count += 1
                    else:
                        raise ValueError(
                            f"{label} contains unsupported filesystem entry: "
                            f"{entry.name} ({entry_type})"
                        )
            except OSError as error:
                raise ValueError(f"{label} cannot be read safely") from error
            finally:
                if current_fd != directory_fd:
                    os.close(current_fd)
    finally:
        for descriptor in directories:
            if descriptor != directory_fd:
                os.close(descriptor)
    if file_count == 0:
        raise ValueError(f"{label} is empty")


def _update_digest_from_file(
    file_fd: int,
    *,
    file_size: int,
    digest_builder,
) -> None:
    remaining = file_size
    while remaining:
        chunk = os.read(file_fd, min(COPY_CHUNK_SIZE, remaining))
        if not chunk:
            raise ValueError("Ireland artifact file changed while being read")
        digest_builder.update(chunk)
        remaining -= len(chunk)
    if os.read(file_fd, 1):
        raise ValueError("Ireland artifact file changed while being read")


def _relative_files(directory_fd: int) -> list[tuple[str, ...]]:
    files: list[tuple[str, ...]] = []
    pending = [(directory_fd, ())]
    try:
        while pending:
            current_fd, prefix = pending.pop()
            try:
                entries = sorted(os.scandir(current_fd), key=lambda entry: entry.name)
                for entry in entries:
                    entry_type = _entry_type_at(entry.name, current_fd)
                    relative = (*prefix, entry.name)
                    if entry_type == "directory":
                        pending.append(
                            (
                                _open_directory(
                                    entry.name,
                                    dir_fd=current_fd,
                                    label="Ireland artifact directory",
                                ),
                                relative,
                            )
                        )
                    elif entry_type == "regular file":
                        files.append(relative)
                    else:
                        raise ValueError(
                            "Ireland artifact contains unsupported filesystem entry: "
                            f"{'/'.join(relative)} ({entry_type})"
                        )
            except OSError as error:
                raise ValueError("Ireland artifact cannot be read safely") from error
            finally:
                if current_fd != directory_fd:
                    os.close(current_fd)
    finally:
        for descriptor, _ in pending:
            if descriptor != directory_fd:
                os.close(descriptor)
    return sorted(files)


def _open_relative_file(directory_fd: int, relative: tuple[str, ...]) -> int:
    current_fd = directory_fd
    opened: list[int] = []
    try:
        for component in relative[:-1]:
            child_fd = _open_directory(
                component,
                dir_fd=current_fd,
                label="Ireland artifact directory",
            )
            opened.append(child_fd)
            current_fd = child_fd
        _check_fd_platform_support()
        return os.open(
            relative[-1],
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=current_fd,
        )
    except OSError as error:
        raise ValueError(
            f"Ireland artifact file cannot be opened safely: {'/'.join(relative)}"
        ) from error
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _directory_provenance_fd(directory_fd: int) -> tuple[str, int]:
    digest_builder = hashlib.sha256()
    total_size = 0
    files = _relative_files(directory_fd)
    if not files:
        raise ValueError("Ireland artifact directory is empty")
    for relative in files:
        relative_path = "/".join(relative).encode("utf-8")
        digest_builder.update(len(relative_path).to_bytes(8, "big"))
        digest_builder.update(relative_path)
        file_fd = _open_relative_file(directory_fd, relative)
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    "Ireland artifact file changed to an unsupported object"
                )
            file_size = metadata.st_size
            total_size += file_size
            digest_builder.update(file_size.to_bytes(8, "big"))
            _update_digest_from_file(
                file_fd,
                file_size=file_size,
                digest_builder=digest_builder,
            )
        finally:
            os.close(file_fd)
    return digest_builder.hexdigest(), total_size


def _validate_tree(path: Path, *, label: str) -> None:
    directory_fd = _open_directory(path, label=label)
    try:
        _validate_directory_fd(directory_fd, label=label)
    finally:
        os.close(directory_fd)


def _source_provenance(source: Path) -> tuple[str, int]:
    directory_fd = _open_directory(source, label="Ireland source directory")
    try:
        _validate_directory_fd(directory_fd, label="Ireland source directory")
        return _directory_provenance_fd(directory_fd)
    finally:
        os.close(directory_fd)


def _release_snapshot_path(release_root: Path) -> Path:
    return Path(release_root) / IRELAND_SNAPSHOT_RELATIVE_PATH


@contextmanager
def _open_release_parent(
    release_root: Path,
    *,
    create_parent: bool,
) -> Iterator[int]:
    root_fd = _open_directory(release_root, label="Ireland release directory")
    parent_fd: int | None = None
    try:
        try:
            parent_fd = _open_directory(
                IRELAND_SNAPSHOT_RELATIVE_PATH.parent.name,
                dir_fd=root_fd,
                label="Ireland release snapshot parent",
            )
        except ValueError as error:
            if not create_parent:
                raise ValueError(
                    "Ireland release snapshot parent is unavailable"
                ) from error
            try:
                os.mkdir(
                    IRELAND_SNAPSHOT_RELATIVE_PATH.parent.name,
                    0o755,
                    dir_fd=root_fd,
                )
            except FileExistsError:
                pass
            parent_fd = _open_directory(
                IRELAND_SNAPSHOT_RELATIVE_PATH.parent.name,
                dir_fd=root_fd,
                label="Ireland release snapshot parent",
            )
        yield parent_fd
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def _create_temp_directory(parent_fd: int) -> tuple[str, int]:
    _check_fd_platform_support()
    for _ in range(TEMP_DIRECTORY_ATTEMPTS):
        name = f".ireland-snapshot-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        try:
            descriptor = _open_directory(
                name,
                dir_fd=parent_fd,
                label="Ireland temporary snapshot directory",
            )
            try:
                if _entry_type_at(name, parent_fd) != "directory":
                    raise ValueError(
                        "Ireland temporary snapshot directory changed type"
                    )
                return name, descriptor
            except BaseException:
                os.close(descriptor)
                raise
        except BaseException:
            _remove_tree_at(parent_fd, name)
            raise
    raise ValueError("Ireland temporary snapshot directory name allocation failed")


def _copy_regular_file_at(source_fd: int, name: str, destination_fd: int) -> None:
    _check_fd_platform_support()
    try:
        source_file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
    except OSError as error:
        raise ValueError(
            f"Ireland source file cannot be opened safely: {name}"
        ) from error
    destination_file_fd: int | None = None
    try:
        source_metadata = os.fstat(source_file_fd)
        if not stat.S_ISREG(source_metadata.st_mode):
            raise ValueError(
                f"Ireland source contains unsupported filesystem entry: {name}"
            )
        try:
            destination_file_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                stat.S_IMODE(source_metadata.st_mode),
                dir_fd=destination_fd,
            )
        except OSError as error:
            raise ValueError(
                f"Ireland staged file cannot be created safely: {name}"
            ) from error
        remaining = source_metadata.st_size
        while remaining:
            chunk = os.read(source_file_fd, min(COPY_CHUNK_SIZE, remaining))
            if not chunk:
                raise ValueError(f"Ireland source file changed during copy: {name}")
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_file_fd, chunk[offset:])
                if written <= 0:
                    raise ValueError(f"Ireland staged file cannot be written: {name}")
                offset += written
            remaining -= len(chunk)
        if os.read(source_file_fd, 1):
            raise ValueError(f"Ireland source file changed during copy: {name}")
        os.fchmod(destination_file_fd, stat.S_IMODE(source_metadata.st_mode))
    finally:
        os.close(source_file_fd)
        if destination_file_fd is not None:
            os.close(destination_file_fd)


def _copy_tree_at(source_fd: int, destination_fd: int) -> None:
    try:
        entries = sorted(os.scandir(source_fd), key=lambda entry: entry.name)
    except OSError as error:
        raise ValueError("Ireland source directory cannot be read safely") from error
    for entry in entries:
        name = entry.name
        entry_type = _entry_type_at(name, source_fd)
        if entry_type == "directory":
            try:
                os.mkdir(name, 0o755, dir_fd=destination_fd)
            except OSError as error:
                raise ValueError(
                    f"Ireland staged directory cannot be created: {name}"
                ) from error
            source_child_fd = _open_directory(
                name,
                dir_fd=source_fd,
                label="Ireland source directory",
            )
            try:
                destination_child_fd = _open_directory(
                    name,
                    dir_fd=destination_fd,
                    label="Ireland staged directory",
                )
                try:
                    _copy_tree_at(source_child_fd, destination_child_fd)
                finally:
                    os.close(destination_child_fd)
            finally:
                os.close(source_child_fd)
        elif entry_type == "regular file":
            _copy_regular_file_at(source_fd, name, destination_fd)
        else:
            raise ValueError(
                "Ireland source contains unsupported filesystem entry: "
                f"{name} ({entry_type})"
            )


def _remove_tree_at(parent_fd: int, name: str) -> None:
    entry_type = _entry_type_at(name, parent_fd)
    if entry_type is None:
        return
    if entry_type == "directory":
        directory_fd = _open_directory(
            name,
            dir_fd=parent_fd,
            label="Ireland cleanup directory",
        )
        try:
            entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
            for entry in entries:
                child_type = _entry_type_at(entry.name, directory_fd)
                if child_type == "directory":
                    _remove_tree_at(directory_fd, entry.name)
                elif child_type == "regular file":
                    os.unlink(entry.name, dir_fd=directory_fd)
                else:
                    raise ValueError(
                        "Ireland cleanup encountered unsupported filesystem entry: "
                        f"{entry.name} ({child_type})"
                    )
        finally:
            os.close(directory_fd)
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError as error:
            raise ValueError(
                "Ireland cleanup could not remove temporary directory"
            ) from error
    elif entry_type == "regular file":
        os.unlink(name, dir_fd=parent_fd)
    else:
        raise ValueError(
            f"Ireland cleanup refuses unsafe filesystem entry: {name} ({entry_type})"
        )


def _publish_temp_directory(parent_fd: int, temporary_name: str) -> None:
    _check_fd_platform_support()
    try:
        os.rename(
            temporary_name,
            IRELAND_SOURCE_ID,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except OSError as error:
        raise ValueError(
            "Ireland release snapshot could not be published safely"
        ) from error


def _validate_snapshot_fd(
    parent_fd: int,
    entry: Mapping[str, object],
) -> tuple[str, int]:
    snapshot_fd = _open_directory(
        IRELAND_SOURCE_ID,
        dir_fd=parent_fd,
        label="Ireland release snapshot",
    )
    try:
        _validate_directory_fd(snapshot_fd, label="Ireland release snapshot")
        digest, size = _directory_provenance_fd(snapshot_fd)
    finally:
        os.close(snapshot_fd)
    expected_digest = entry.get("sha256")
    expected_size = entry.get("size")
    if not isinstance(expected_digest, str) or not isinstance(expected_size, int):
        raise ValueError("Ireland release snapshot provenance is missing")  # noqa: TRY004
    if (digest, size) != (expected_digest, expected_size):
        raise ValueError("Ireland release snapshot provenance does not match manifest")
    return digest, size


def capture_ireland_snapshot(
    source: Path,
    release_root: Path,
    *,
    max_attempts: int = DEFAULT_CAPTURE_ATTEMPTS,
) -> IrelandSnapshot:
    """Copy one stable source generation into the release tree.

    The source publisher is independent of this process, so capture uses a
    physical copy and compares source provenance before and after the copy.
    A changed generation is retried without publishing the partial directory.
    """
    if max_attempts < 1:
        raise ValueError("Ireland snapshot capture attempts must be positive")
    source = Path(source)
    release_root = Path(release_root)
    destination = _release_snapshot_path(release_root)

    with _open_release_parent(release_root, create_parent=True) as parent_fd:
        if _entry_type_at(IRELAND_SOURCE_ID, parent_fd) is not None:
            raise ValueError(f"Ireland release snapshot already exists: {destination}")
        _source_provenance(source)
        for attempt in range(1, max_attempts + 1):
            temporary_name: str | None = None
            temporary_fd: int | None = None
            published = False
            completed = False
            try:
                before_digest, before_size = _source_provenance(source)
                temporary_name, temporary_fd = _create_temp_directory(parent_fd)
                source_fd = _open_directory(source, label="Ireland source directory")
                try:
                    _copy_tree_at(source_fd, temporary_fd)
                finally:
                    os.close(source_fd)
                _validate_directory_fd(temporary_fd, label="Ireland staged snapshot")
                copied_digest, copied_size = _directory_provenance_fd(temporary_fd)
                after_digest, after_size = _source_provenance(source)
                if (before_digest, before_size) != (copied_digest, copied_size) or (
                    before_digest,
                    before_size,
                ) != (after_digest, after_size):
                    raise _CaptureRetry

                if _entry_type_at(IRELAND_SOURCE_ID, parent_fd) is not None:
                    raise ValueError(
                        f"Ireland release snapshot already exists: {destination}"
                    )
                _publish_temp_directory(parent_fd, temporary_name)
                temporary_name = None
                published = True
                final_digest, final_size = _directory_provenance_fd(temporary_fd)
                if (final_digest, final_size) != (before_digest, before_size):
                    _remove_tree_at(parent_fd, IRELAND_SOURCE_ID)
                    published = False
                    raise ValueError(
                        "Ireland release snapshot changed during publication"
                    )
                completed = True
                return IrelandSnapshot(destination, final_digest, final_size)
            except _CaptureRetry:
                if attempt == max_attempts:
                    raise ValueError(
                        "Ireland source changed during snapshot capture; refusing mixed generation"
                    )
            except (FileNotFoundError, OSError, ValueError) as error:
                if attempt == max_attempts:
                    if isinstance(error, ValueError):
                        raise
                    raise ValueError(
                        "Ireland source could not be captured safely; refusing mutable fallback"
                    ) from error
            finally:
                if temporary_fd is not None:
                    os.close(temporary_fd)
                if temporary_name is not None:
                    _remove_tree_at(parent_fd, temporary_name)
                if published and not completed:
                    _remove_tree_at(parent_fd, IRELAND_SOURCE_ID)

    raise AssertionError("unreachable Ireland snapshot capture state")


def validate_ireland_release_snapshot(
    entry: Mapping[str, object],
    release_root: Path,
) -> Path:
    """Require an Ireland manifest entry to point at its exact release snapshot."""
    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("Ireland release-local snapshot path is missing")

    release_root = Path(release_root)
    expected = Path(os.path.abspath(_release_snapshot_path(release_root)))
    path = Path(raw_path)
    if not path.is_absolute():
        path = release_root / path
    lexical_path = Path(os.path.abspath(path))
    if lexical_path != expected:
        raise ValueError(
            f"Ireland release-local snapshot escapes its expected release path: {path}"
        )
    with _open_release_parent(release_root, create_parent=False) as parent_fd:
        if _entry_type_at(IRELAND_SOURCE_ID, parent_fd) != "directory":
            raise ValueError(
                "Ireland release-local snapshot escapes its expected release path: "
                f"{path}"
            )
        _validate_snapshot_fd(parent_fd, entry)
    return expected
