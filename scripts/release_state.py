"""Persistent release state and fail-closed activation inspection."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from artifact_provenance import artifact_provenance
from ireland_artifact_snapshot import validate_ireland_release_snapshot


STATE_FILE_NAME = "release-state.json"
STATE_SCHEMA_VERSION = 1
STAGES = (
    "build",
    "candidate-validation",
    "static-departures",
    "handoff-readiness",
    "commit",
)
STAGE_ORDER = {stage: index for index, stage in enumerate(STAGES)}
NEXT_STAGE = {
    "build": "candidate-validation",
    "candidate-validation": "static-departures",
    "static-departures": "handoff-readiness",
    "handoff-readiness": "commit",
    "commit": "commit",
}
RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

REQUIRED_DIRECTORIES = (
    "stops",
    "routes",
    "departures",
    "trips",
    "transit",
    "radar",
    "swiss-static",
    "provenance",
)
REQUIRED_FILES = (
    "manifest.json",
    "transit-radar-cities.json",
    "swiss-static/manifest.json",
    "provenance/input-artifacts.json",
)
INTEGRITY_SCHEMA_VERSION = 1
INTEGRITY_BASE_PATHS = (
    "stop-data",
    "gtfs-artifacts.json",
    "custom-gtfs-artifacts.json",
    "release-metadata.json",
)
INTEGRITY_OPTIONAL_PATHS = ("austrian-artifacts.json",)
INTEGRITY_STATIC_PATHS = ("departures.sqlite",)


class ResumeError(ValueError):
    """Raised when a release cannot be resumed safely."""


@dataclass(frozen=True)
class ResumeInfo:
    status: str
    completed_stage: str
    next_stage: str


def _validate_release_id(release_id: str) -> None:
    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise ResumeError(f"invalid release ID: {release_id!r}")


def _read_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResumeError(f"cannot read JSON artifact {path}: {type(error).__name__}") from error
    if not isinstance(payload, dict):
        raise ResumeError(f"JSON artifact is not an object: {path}")
    return payload


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _filesystem_entry_type(path: Path) -> str:
    mode = path.lstat().st_mode
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


def _validate_integrity_tree(path: Path) -> None:
    try:
        entry_type = _filesystem_entry_type(path)
    except OSError as error:
        raise ResumeError(f"cannot inspect integrity entry {path}") from error
    if entry_type != "directory":
        raise ResumeError(
            f"unsupported filesystem entry in integrity scope: {path} ({entry_type})"
        )

    pending = [path]
    while pending:
        current = pending.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise ResumeError(f"cannot inspect integrity directory {current}") from error
        for child in children:
            try:
                child_type = _filesystem_entry_type(child)
            except OSError as error:
                raise ResumeError(f"cannot inspect integrity entry {child}") from error
            if child_type == "directory":
                pending.append(child)
            elif child_type != "regular file":
                raise ResumeError(
                    "unsupported filesystem entry in integrity scope: "
                    f"{child} ({child_type})"
                )


def _integrity_paths(release_dir: Path, completed_stage: str) -> tuple[str, ...]:
    paths = list(INTEGRITY_BASE_PATHS)
    paths.extend(
        relative
        for relative in INTEGRITY_OPTIONAL_PATHS
        if (release_dir / relative).exists() or (release_dir / relative).is_symlink()
    )
    if STAGE_ORDER[completed_stage] >= STAGE_ORDER["static-departures"]:
        paths.extend(INTEGRITY_STATIC_PATHS)
    return tuple(paths)


def _candidate_integrity_evidence(
    release_dir: Path,
    completed_stage: str,
) -> dict[str, object]:
    files: dict[str, dict[str, object]] = {}
    for relative in _integrity_paths(release_dir, completed_stage):
        path = release_dir / relative
        if relative == "stop-data":
            _validate_integrity_tree(path)
            for directory in REQUIRED_DIRECTORIES:
                directory_path = path / directory
                try:
                    directory_type = _filesystem_entry_type(directory_path)
                except OSError as error:
                    raise ResumeError(
                        f"required integrity directory is missing: {directory_path}"
                    ) from error
                if directory_type != "directory":
                    raise ResumeError(
                        "unsupported filesystem entry in integrity scope: "
                        f"{directory_path} ({directory_type})"
                    )
            valid = True
        else:
            try:
                entry_type = _filesystem_entry_type(path)
            except OSError as error:
                raise ResumeError(f"cannot inspect integrity entry {path}") from error
            if entry_type != "regular file":
                raise ResumeError(
                    "unsupported filesystem entry in integrity scope: "
                    f"{path} ({entry_type})"
                )
            valid = True
        if not valid:
            raise ResumeError(f"integrity artifact is missing: {path}")
        try:
            digest, size = artifact_provenance(path)
        except (OSError, ValueError) as error:
            raise ResumeError(f"cannot fingerprint integrity artifact {path}") from error
        files[relative] = {"sha256": digest, "size": size}
    return {"schemaVersion": INTEGRITY_SCHEMA_VERSION, "files": files}


def _validated_integrity_payload(
    payload: object,
    completed_stage: str,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ResumeError("release state candidateIntegrity is missing or invalid")
    if payload.get("schemaVersion") != INTEGRITY_SCHEMA_VERSION:
        raise ResumeError("release state candidateIntegrity schema is unsupported")
    files = payload.get("files")
    if not isinstance(files, Mapping):
        raise ResumeError("release state candidateIntegrity files are invalid")

    required = set(INTEGRITY_BASE_PATHS)
    if STAGE_ORDER[completed_stage] >= STAGE_ORDER["static-departures"]:
        required.update(INTEGRITY_STATIC_PATHS)
    allowed = required | set(INTEGRITY_OPTIONAL_PATHS)
    if not required.issubset(files) or not set(files).issubset(allowed):
        raise ResumeError("release state candidateIntegrity file set is invalid")

    normalized_files: dict[str, dict[str, object]] = {}
    for relative, entry in files.items():
        if not isinstance(relative, str) or not isinstance(entry, Mapping):
            raise ResumeError("release state candidateIntegrity entry is invalid")
        digest = entry.get("sha256")
        size = entry.get("size")
        if (
            not isinstance(digest, str)
            or not digest
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
        ):
            raise ResumeError(
                f"release state candidateIntegrity entry is invalid: {relative}"
            )
        normalized_files[relative] = {"sha256": digest, "size": size}
    return {"schemaVersion": INTEGRITY_SCHEMA_VERSION, "files": normalized_files}


def _ensure_integrity_compatible(
    previous: dict[str, object],
    current: dict[str, object],
) -> None:
    previous_files = previous["files"]
    current_files = current["files"]
    if not isinstance(previous_files, dict) or not isinstance(current_files, dict):
        raise ResumeError("candidate integrity file set is invalid")
    allowed_new = {"departures.sqlite"}
    if not set(current_files) - set(previous_files) <= allowed_new:
        raise ResumeError("candidate integrity file set changed during resume")
    for relative, entry in previous_files.items():
        if current_files.get(relative) != entry:
            raise ResumeError(f"candidate integrity evidence mismatch: {relative}")


def _verify_candidate_integrity(
    release_dir: Path,
    completed_stage: str,
    state: Mapping[str, object],
) -> None:
    stored = _validated_integrity_payload(
        state.get("candidateIntegrity"),
        completed_stage,
    )
    current = _candidate_integrity_evidence(release_dir, completed_stage)
    if stored != current:
        raise ResumeError("candidate integrity evidence mismatch")


def write_state(
    release_dir: Path,
    *,
    release_id: str,
    completed_stage: str,
    build_fingerprint: str,
) -> None:
    _validate_release_id(release_id)
    if completed_stage not in STAGE_ORDER:
        raise ResumeError(f"invalid completed stage: {completed_stage!r}")
    if not build_fingerprint or build_fingerprint == "unknown":
        raise ResumeError("build fingerprint is unavailable")
    if not release_dir.is_dir():
        raise ResumeError(f"release directory is missing: {release_dir}")
    state_path = release_dir / STATE_FILE_NAME
    previous: dict[str, object] | None = None
    if state_path.exists() or state_path.is_symlink():
        previous = read_state(release_dir, release_id)
        if previous["buildFingerprint"] != build_fingerprint:
            raise ResumeError("existing release state fingerprint does not match")
        if STAGE_ORDER[str(previous["completedStage"])] > STAGE_ORDER[completed_stage]:
            raise ResumeError("release state cannot move backwards")

    candidate_integrity = None
    if STAGE_ORDER[completed_stage] >= STAGE_ORDER["candidate-validation"]:
        candidate_integrity = _candidate_integrity_evidence(release_dir, completed_stage)
        if previous is not None:
            previous_stage = str(previous["completedStage"])
            if STAGE_ORDER[previous_stage] >= STAGE_ORDER["candidate-validation"]:
                previous_integrity = _validated_integrity_payload(
                    previous.get("candidateIntegrity"),
                    previous_stage,
                )
                _ensure_integrity_compatible(previous_integrity, candidate_integrity)

    payload: dict[str, object] = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "releaseID": release_id,
        "completedStage": completed_stage,
        "buildFingerprint": build_fingerprint,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    }
    if candidate_integrity is not None:
        payload["candidateIntegrity"] = candidate_integrity
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".release-state-",
        suffix=".tmp",
        dir=release_dir,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, release_dir / STATE_FILE_NAME)
        _fsync_directory(release_dir)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def read_state(release_dir: Path, release_id: str) -> dict[str, object]:
    _validate_release_id(release_id)
    path = release_dir / STATE_FILE_NAME
    if path.is_symlink() or not path.is_file():
        raise ResumeError(f"release state is missing: {path}")
    payload = _read_object(path)
    if payload.get("schemaVersion") != STATE_SCHEMA_VERSION:
        raise ResumeError("release state schema is unsupported")
    if payload.get("releaseID") != release_id:
        raise ResumeError("release state release ID does not match requested release")
    completed_stage = payload.get("completedStage")
    if completed_stage not in STAGE_ORDER:
        raise ResumeError("release state completedStage is invalid")
    build_fingerprint = payload.get("buildFingerprint")
    if not isinstance(build_fingerprint, str) or not build_fingerprint or build_fingerprint == "unknown":
        raise ResumeError("release state buildFingerprint is invalid")
    updated_at = payload.get("updatedAt")
    if not isinstance(updated_at, str) or not updated_at:
        raise ResumeError("release state updatedAt is invalid")
    try:
        timestamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ResumeError("release state updatedAt is invalid") from error
    if timestamp.tzinfo is None:
        raise ResumeError("release state updatedAt must include a timezone")
    result: dict[str, object] = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "releaseID": release_id,
        "completedStage": completed_stage,
        "buildFingerprint": build_fingerprint,
        "updatedAt": updated_at,
    }
    if STAGE_ORDER[str(completed_stage)] >= STAGE_ORDER["candidate-validation"]:
        result["candidateIntegrity"] = _validated_integrity_payload(
            payload.get("candidateIntegrity"),
            str(completed_stage),
        )
    return result


def _validate_required_artifacts(release_dir: Path) -> dict[str, object]:
    stop_data = release_dir / "stop-data"
    if not stop_data.is_dir():
        raise ResumeError(f"stop-data directory is missing: {stop_data}")
    for relative in REQUIRED_DIRECTORIES:
        path = stop_data / relative
        if not path.is_dir():
            raise ResumeError(f"required stop-data directory is missing: {path}")
    for relative in REQUIRED_FILES:
        path = stop_data / relative
        if not path.is_file() or path.stat().st_size == 0:
            raise ResumeError(f"required stop-data artifact is missing or empty: {path}")
        _read_object(path)
    manifest = _read_object(stop_data / "manifest.json")
    cities = manifest.get("cities")
    if not isinstance(cities, list) or not cities:
        raise ResumeError("candidate manifest has no cities")
    for city in cities:
        if not isinstance(city, Mapping):
            raise ResumeError("candidate manifest contains an invalid city entry")
        raw_url = city.get("url")
        if not isinstance(raw_url, str) or not raw_url.startswith("stops/"):
            raise ResumeError("candidate manifest contains an invalid city package URL")
        package_path = stop_data / raw_url
        try:
            package_path.resolve().relative_to(stop_data.resolve())
        except ValueError as error:
            raise ResumeError("candidate city package escapes stop-data") from error
        if not package_path.is_file() or package_path.stat().st_size == 0:
            raise ResumeError(f"candidate city package is missing or empty: {package_path}")
        try:
            json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ResumeError(f"candidate city package is invalid: {package_path}") from error
    artifacts_path = release_dir / "gtfs-artifacts.json"
    if not artifacts_path.is_file() or artifacts_path.stat().st_size == 0:
        raise ResumeError(f"GTFS artifact manifest is missing: {artifacts_path}")
    custom_path = release_dir / "custom-gtfs-artifacts.json"
    if not custom_path.is_file() or custom_path.stat().st_size == 0:
        raise ResumeError(f"custom GTFS artifact manifest is missing: {custom_path}")
    return manifest


def _validate_artifact_manifest(
    release_dir: Path,
    repository_root: Path,
    payload: Mapping[str, object],
) -> None:
    sys.path.insert(0, str(repository_root / "scripts"))
    from release_integrity import validate_artifact_entry

    found = 0
    for group in ("sources", "external"):
        entries = payload.get(group)
        if not isinstance(entries, Mapping):
            continue
        for source_id, entry in entries.items():
            if not isinstance(entry, Mapping) or not entry.get("path"):
                continue
            try:
                if str(source_id) == "ireland":
                    validate_ireland_release_snapshot(entry, release_dir)
                validate_artifact_entry(
                    str(source_id),
                    entry,
                    base_dir=release_dir,
                )
            except (OSError, ValueError) as error:
                raise ResumeError(str(error)) from error
            found += 1
    if found == 0:
        raise ResumeError("GTFS artifact manifest contains no usable provenance")

    custom_path = release_dir / "custom-gtfs-artifacts.json"
    if custom_path.is_file():
        custom = _read_object(custom_path)
        custom_sources = custom.get("sources")
        if not isinstance(custom_sources, Mapping) or set(custom_sources) != {"vbb", "rnv"}:
            raise ResumeError("custom GTFS provenance must contain exactly vbb and rnv")
        for source_id, entry in custom_sources.items():
            if not isinstance(entry, Mapping):
                raise ResumeError(f"custom GTFS provenance is invalid for {source_id}")
            try:
                validate_artifact_entry(str(source_id), entry, base_dir=release_dir)
            except (OSError, ValueError) as error:
                raise ResumeError(str(error)) from error

    austrian_path = release_dir / "austrian-artifacts.json"
    if austrian_path.is_file():
        austrian = _read_object(austrian_path)
        entries = austrian.get("sources")
        if not isinstance(entries, list) or not entries:
            raise ResumeError("Austrian provenance is invalid")
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ResumeError("Austrian provenance contains an invalid entry")
            source_id = str(entry.get("source", ""))
            if not source_id:
                raise ResumeError("Austrian provenance entry has no source")
            try:
                validate_artifact_entry(source_id, entry, base_dir=release_dir)
            except (OSError, ValueError) as error:
                raise ResumeError(str(error)) from error


def _validate_candidate(
    release_dir: Path,
    release_id: str,
    repository_root: Path,
    state: Mapping[str, object],
    *,
    current_fingerprint: str,
    validation_stage: str,
) -> None:
    if state.get("buildFingerprint") != current_fingerprint:
        raise ResumeError("build fingerprint does not match current pipeline")

    if STAGE_ORDER[validation_stage] >= STAGE_ORDER["candidate-validation"]:
        _verify_candidate_integrity(release_dir, validation_stage, state)
        artifacts_path = release_dir / "gtfs-artifacts.json"
        artifacts = _read_object(artifacts_path)
        _validate_artifact_manifest(release_dir, repository_root, artifacts)
        return

    _validate_required_artifacts(release_dir)
    artifacts_path = release_dir / "gtfs-artifacts.json"
    artifacts = _read_object(artifacts_path)
    _validate_artifact_manifest(release_dir, repository_root, artifacts)

    return


def _pointer_kind(
    path: Path,
    *,
    releases_root: Path,
    candidate_release_dir: Path,
    suffix: str,
    allow_dangling_release_pointer: bool = False,
) -> tuple[str, str | None]:
    if not path.is_symlink():
        if path.exists():
            return "invalid", None
        return "absent", None

    resolved = Path(os.path.realpath(path))
    if not resolved.exists():
        if allow_dangling_release_pointer:
            try:
                relative = resolved.relative_to(releases_root.resolve())
            except ValueError:
                return "invalid", None
            parts = relative.parts
            if suffix:
                if len(parts) != 2 or parts[1] != suffix:
                    return "invalid", None
            elif len(parts) != 1:
                return "invalid", None
            release_id = parts[0]
            if not RELEASE_ID_PATTERN.fullmatch(release_id):
                return "invalid", None
            if not (releases_root / release_id).is_dir():
                return "absent", None
        return "invalid", None
    candidate_target = candidate_release_dir / suffix if suffix else candidate_release_dir
    if resolved == candidate_target.resolve():
        return "candidate", candidate_release_dir.name

    try:
        relative = resolved.relative_to(releases_root.resolve())
    except ValueError:
        return "invalid", None
    parts = relative.parts
    if suffix:
        if len(parts) != 2 or parts[1] != suffix:
            return "invalid", None
    elif len(parts) != 1:
        return "invalid", None
    release_id = parts[0]
    if not release_id or not (releases_root / release_id).is_dir():
        return "invalid", None
    return "other", release_id


def detect_activation_state(
    *,
    releases_root: Path,
    candidate_release_dir: Path,
    current: Path,
    current_release: Path,
    static_departures_release: Path,
    departures_current: Path,
    previous: Path,
) -> str:
    specs = {
        "current-release": (current_release, ""),
        "current": (current, "stop-data"),
        "static-departures-release": (static_departures_release, ""),
        "departures-current.sqlite": (departures_current, "departures.sqlite"),
        "previous": (previous, "stop-data"),
    }
    states = {
        name: _pointer_kind(
            path,
            releases_root=releases_root,
            candidate_release_dir=candidate_release_dir,
            suffix=suffix,
            allow_dangling_release_pointer=name == "previous",
        )
        for name, (path, suffix) in specs.items()
    }
    invalid = [name for name, (kind, _) in states.items() if kind == "invalid"]
    if invalid:
        raise ResumeError("invalid or unsafe canonical pointer: " + ", ".join(invalid))

    primary_names = ("current-release", "current", "departures-current.sqlite")
    primary = [states[name] for name in primary_names]
    static_kind, static_release = states["static-departures-release"]
    previous_kind, previous_release = states["previous"]

    if all(kind == "candidate" for kind, _ in primary) and static_kind == "candidate":
        if previous_kind == "candidate":
            raise ResumeError("previous pointer also targets candidate during activation")
        return "already-active"

    if any(kind == "candidate" for kind, _ in primary) or static_kind == "candidate":
        raise ResumeError("partial activation detected in canonical pointers")

    primary_releases = {release for kind, release in primary if kind == "other"}
    primary_absent = all(kind == "absent" for kind, _ in primary)
    primary_coherent = len(primary_releases) == 1 and all(
        kind == "other" and release in primary_releases for kind, release in primary
    )
    if not (primary_absent or primary_coherent):
        raise ResumeError("canonical pointers are inconsistent before resume")

    if static_kind == "other":
        if not primary_coherent or static_release not in primary_releases:
            raise ResumeError("static-departures handoff is inconsistent before resume")
    elif static_kind != "absent":
        raise ResumeError("static-departures handoff is unsafe before resume")
    if previous_kind == "candidate":
        raise ResumeError("previous pointer targets candidate before activation")
    if previous_kind == "other" and previous_release == candidate_release_dir.name:
        raise ResumeError("previous pointer targets candidate before activation")

    return "candidate-not-activated"


def inspect_resume(
    *,
    releases_root: Path,
    release_id: str,
    repository_root: Path,
    current_fingerprint: str,
    current: Path,
    current_release: Path,
    static_departures_release: Path,
    departures_current: Path,
    previous: Path,
) -> ResumeInfo:
    _validate_release_id(release_id)
    release_dir = releases_root / release_id
    if release_dir.is_symlink() or not release_dir.is_dir():
        raise ResumeError(f"release directory is missing: {release_dir}")
    state = read_state(release_dir, release_id)
    completed_stage = str(state["completedStage"])
    _validate_candidate(
        release_dir,
        release_id,
        repository_root,
        state,
        current_fingerprint=current_fingerprint,
        validation_stage=completed_stage,
    )
    status = detect_activation_state(
        releases_root=releases_root,
        candidate_release_dir=release_dir,
        current=current,
        current_release=current_release,
        static_departures_release=static_departures_release,
        departures_current=departures_current,
        previous=previous,
    )
    if status == "already-active":
        if STAGE_ORDER[completed_stage] < STAGE_ORDER["handoff-readiness"]:
            raise ResumeError("already-active release state is before handoff-readiness")
        return ResumeInfo(status, completed_stage, "already-active")
    if completed_stage == "commit":
        raise ResumeError("commit state exists but release is not fully active")
    return ResumeInfo(status, completed_stage, NEXT_STAGE[completed_stage])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    write_parser = subparsers.add_parser("write-state")
    write_parser.add_argument("--release-dir", type=Path, required=True)
    write_parser.add_argument("--release-id", required=True)
    write_parser.add_argument("--completed-stage", choices=STAGES, required=True)
    write_parser.add_argument("--build-fingerprint", required=True)

    inspect_parser = subparsers.add_parser("inspect-resume")
    inspect_parser.add_argument("--releases-root", type=Path, required=True)
    inspect_parser.add_argument("--release-id", required=True)
    inspect_parser.add_argument("--repository", type=Path, required=True)
    inspect_parser.add_argument("--current-fingerprint", required=True)
    inspect_parser.add_argument("--current", type=Path, required=True)
    inspect_parser.add_argument("--current-release", type=Path, required=True)
    inspect_parser.add_argument("--static-departures-release", type=Path, required=True)
    inspect_parser.add_argument("--departures-current", type=Path, required=True)
    inspect_parser.add_argument("--previous", type=Path, required=True)
    inspect_parser.add_argument("--format", choices=("json", "tsv"), default="json")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "write-state":
            write_state(
                args.release_dir,
                release_id=args.release_id,
                completed_stage=args.completed_stage,
                build_fingerprint=args.build_fingerprint,
            )
            return 0

        info = inspect_resume(
            releases_root=args.releases_root,
            release_id=args.release_id,
            repository_root=args.repository,
            current_fingerprint=args.current_fingerprint,
            current=args.current,
            current_release=args.current_release,
            static_departures_release=args.static_departures_release,
            departures_current=args.departures_current,
            previous=args.previous,
        )
        if args.format == "tsv":
            print(f"{info.status}\t{info.completed_stage}\t{info.next_stage}")
        else:
            print(
                json.dumps(
                    {
                        "status": info.status,
                        "completedStage": info.completed_stage,
                        "nextStage": info.next_stage,
                    },
                    separators=(",", ":"),
                )
            )
        return 0
    except ResumeError as error:
        print(f"[StopData] ERROR: resume refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
