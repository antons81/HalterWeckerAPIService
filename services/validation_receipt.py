"""Immutable receipts for already completed release validation."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


VALIDATION_RECEIPT_FILE_NAME = "validation-receipt.json"
VALIDATION_RECEIPT_SCHEMA_VERSION = 1
VALIDATOR_COMPATIBILITY = "provider-runtime-validation-v1"
VALIDATOR_FINGERPRINT = "static-departures-runtime-validation-v1"


class ValidationReceiptError(ValueError):
    """Raised when a validation receipt cannot be trusted."""


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationReceiptError(f"{label} is invalid: {path}") from error
    if not isinstance(payload, dict):
        raise ValidationReceiptError(f"{label} must be an object: {path}")
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ValidationReceiptError(f"file is unreadable: {path}") from error
    return digest.hexdigest()


def _stat_payload(path: Path) -> dict[str, int]:
    try:
        stat = path.stat()
    except OSError as error:
        raise ValidationReceiptError(f"file is unavailable: {path}") from error
    if not path.is_file():
        raise ValidationReceiptError(f"path is not a file: {path}")
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtimeNs": int(stat.st_mtime_ns),
    }


def _resolve_path(root: Path, value: object, label: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValidationReceiptError(f"{label} path is missing")
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValidationReceiptError(f"{label} path is unavailable: {path}") from error
    return resolved


def _portable_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _manifest_path(root: Path, entry: Mapping[str, object], database_path: Path, label: str) -> Path:
    value = entry.get("manifestPath")
    return _resolve_path(
        root,
        value if value else database_path.parent / "manifest.json",
        f"{label} manifest",
    )


def _release_manifest_path(root: Path) -> Path:
    for candidate in (root / "release.json", root / "manifest.json"):
        if candidate.is_file():
            return candidate.resolve()
    raise ValidationReceiptError(f"release manifest is missing below {root}")


def _provider_ids(payload: Mapping[str, object], provider_ids: Sequence[str]) -> tuple[str, ...]:
    providers = payload.get("providers")
    if not isinstance(providers, Mapping):
        raise ValidationReceiptError("release provider registry is missing")
    selected = tuple(str(provider_id) for provider_id in provider_ids)
    if not selected or any(provider_id not in providers for provider_id in selected):
        raise ValidationReceiptError("release does not contain all selected providers")
    return selected


def build_validation_receipt(
    release_root: Path | str,
    *,
    provider_ids: Sequence[str],
    payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    root = Path(release_root).resolve()
    manifest_path = _release_manifest_path(root)
    release_payload = dict(payload) if payload is not None else _read_object(manifest_path, "release manifest")
    release_id = str(release_payload.get("releaseID") or "").strip()
    if not release_id:
        raise ValidationReceiptError("releaseID is missing")
    selected = _provider_ids(release_payload, provider_ids)

    common = release_payload.get("common")
    if not isinstance(common, Mapping):
        raise ValidationReceiptError("common DB reference is missing")
    common_path = _resolve_path(root, common.get("path"), "common DB")

    stop_data = release_payload.get("stopData")
    stop_data_entry = stop_data if isinstance(stop_data, Mapping) else {}
    stop_data_root = _resolve_path(root, stop_data_entry.get("path") or "stop-data", "stop-data")
    stop_manifest_path = _resolve_path(
        root,
        stop_data_entry.get("manifestPath") or stop_data_root / "manifest.json",
        "stop-data manifest",
    )

    artifacts: dict[str, dict[str, object]] = {}
    providers = release_payload["providers"]
    assert isinstance(providers, Mapping)
    for provider_id in selected:
        entry = providers.get(provider_id)
        if not isinstance(entry, Mapping):
            raise ValidationReceiptError(f"provider={provider_id} entry is invalid")
        for artifact_type in ("structural", "temporal"):
            reference = entry.get(artifact_type)
            if not isinstance(reference, Mapping):
                raise ValidationReceiptError(
                    f"provider={provider_id} {artifact_type} reference is missing"
                )
            database_path = _resolve_path(
                root,
                reference.get("path") or reference.get("databasePath"),
                f"provider={provider_id} {artifact_type}",
            )
            artifact_manifest_path = _manifest_path(
                root,
                reference,
                database_path,
                f"provider={provider_id} {artifact_type}",
            )
            manifest = _read_object(
                artifact_manifest_path,
                f"provider={provider_id} {artifact_type} manifest",
            )
            key = f"{provider_id}/{artifact_type}"
            artifacts[key] = {
                "path": _portable_path(root, database_path),
                "manifestPath": _portable_path(root, artifact_manifest_path),
                "manifestSha256": _sha256_file(artifact_manifest_path),
                "databaseStat": _stat_payload(database_path),
                "artifactKey": str(reference.get("artifactKey") or manifest.get("artifactKey") or ""),
                "sha256": str(reference.get("sha256") or ""),
                "size": int(reference.get("size") or 0),
            }

    return {
        "schemaVersion": VALIDATION_RECEIPT_SCHEMA_VERSION,
        "validatorCompatibility": VALIDATOR_COMPATIBILITY,
        "validatorFingerprint": VALIDATOR_FINGERPRINT,
        "result": "PASS",
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "releaseID": release_id,
        "providerIDs": list(selected),
        "releaseManifest": {
            "path": _portable_path(root, manifest_path),
            "sha256": _sha256_file(manifest_path),
        },
        "common": {
            "path": _portable_path(root, common_path),
            "sha256": str(common.get("sha256") or ""),
            "size": int(common.get("size") or 0),
            "databaseStat": _stat_payload(common_path),
        },
        "stopData": {
            "path": _portable_path(root, stop_data_root),
            "manifestPath": _portable_path(root, stop_manifest_path),
            "manifestSha256": _sha256_file(stop_manifest_path),
            "fingerprint": str(
                stop_data_entry.get("fingerprint")
                or stop_data_entry.get("stopDataFingerprint")
                or ""
            ),
        },
        "artifacts": artifacts,
    }


def write_validation_receipt(
    release_root: Path | str,
    *,
    provider_ids: Sequence[str],
    payload: Mapping[str, object] | None = None,
) -> Path:
    root = Path(release_root).resolve()
    receipt = build_validation_receipt(root, provider_ids=provider_ids, payload=payload)
    target = root / VALIDATION_RECEIPT_FILE_NAME
    temporary = root / f".{VALIDATION_RECEIPT_FILE_NAME}.tmp-{os.getpid()}"
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def _same_stat(path: Path, expected: object) -> bool:
    return isinstance(expected, Mapping) and dict(_stat_payload(path)) == dict(expected)


def validate_validation_receipt(
    release_root: Path | str,
    *,
    provider_ids: Sequence[str],
    payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    root = Path(release_root).resolve()
    receipt_path = root / VALIDATION_RECEIPT_FILE_NAME
    if not receipt_path.is_file():
        raise ValidationReceiptError(f"validation receipt is missing: {receipt_path}")
    receipt = _read_object(receipt_path, "validation receipt")
    if receipt.get("schemaVersion") != VALIDATION_RECEIPT_SCHEMA_VERSION:
        raise ValidationReceiptError("validation receipt schema is unsupported")
    if receipt.get("validatorCompatibility") != VALIDATOR_COMPATIBILITY:
        raise ValidationReceiptError("validation receipt validator is incompatible")
    if receipt.get("validatorFingerprint") != VALIDATOR_FINGERPRINT:
        raise ValidationReceiptError("validation receipt validator fingerprint is incompatible")
    if receipt.get("result") != "PASS":
        raise ValidationReceiptError("validation receipt does not contain PASS")

    manifest_path = _release_manifest_path(root)
    release_payload = dict(payload) if payload is not None else _read_object(manifest_path, "release manifest")
    release_id = str(release_payload.get("releaseID") or "").strip()
    if receipt.get("releaseID") != release_id:
        raise ValidationReceiptError("validation receipt releaseID mismatch")
    selected = _provider_ids(release_payload, provider_ids)
    if set(receipt.get("providerIDs") or ()) != set(selected):
        raise ValidationReceiptError("validation receipt provider set mismatch")

    release_reference = receipt.get("releaseManifest")
    if not isinstance(release_reference, Mapping):
        raise ValidationReceiptError("validation receipt release manifest record is missing")
    if _resolve_path(root, release_reference.get("path"), "receipt release manifest") != manifest_path:
        raise ValidationReceiptError("validation receipt release manifest path mismatch")
    if _sha256_file(manifest_path) != str(release_reference.get("sha256") or ""):
        raise ValidationReceiptError("release.json changed after validation")

    common_reference = receipt.get("common")
    common = release_payload.get("common")
    if not isinstance(common_reference, Mapping) or not isinstance(common, Mapping):
        raise ValidationReceiptError("validation receipt common record is missing")
    common_path = _resolve_path(root, common.get("path"), "common DB")
    if _resolve_path(root, common_reference.get("path"), "receipt common DB") != common_path:
        raise ValidationReceiptError("validation receipt common DB path mismatch")
    if str(common_reference.get("sha256") or "") != str(common.get("sha256") or ""):
        raise ValidationReceiptError("validation receipt common DB digest mismatch")
    if int(common_reference.get("size") or 0) != int(common.get("size") or 0):
        raise ValidationReceiptError("validation receipt common DB size mismatch")
    if not _same_stat(common_path, common_reference.get("databaseStat")):
        raise ValidationReceiptError("common DB changed after validation")

    stop_reference = receipt.get("stopData")
    stop_data = release_payload.get("stopData")
    if not isinstance(stop_reference, Mapping):
        raise ValidationReceiptError("validation receipt stop-data record is missing")
    stop_data_entry = stop_data if isinstance(stop_data, Mapping) else {}
    stop_data_root = _resolve_path(root, stop_data_entry.get("path") or "stop-data", "stop-data")
    stop_manifest_path = _resolve_path(
        root,
        stop_data_entry.get("manifestPath") or stop_data_root / "manifest.json",
        "stop-data manifest",
    )
    if _resolve_path(root, stop_reference.get("path"), "receipt stop-data") != stop_data_root:
        raise ValidationReceiptError("validation receipt stop-data path mismatch")
    if _resolve_path(root, stop_reference.get("manifestPath"), "receipt stop-data manifest") != stop_manifest_path:
        raise ValidationReceiptError("validation receipt stop-data manifest path mismatch")
    if _sha256_file(stop_manifest_path) != str(stop_reference.get("manifestSha256") or ""):
        raise ValidationReceiptError("stop-data manifest changed after validation")
    expected_stop_fingerprint = str(
        stop_data_entry.get("fingerprint")
        or stop_data_entry.get("stopDataFingerprint")
        or ""
    )
    if str(stop_reference.get("fingerprint") or "") != expected_stop_fingerprint:
        raise ValidationReceiptError("stop-data fingerprint changed after validation")

    receipt_artifacts = receipt.get("artifacts")
    providers = release_payload.get("providers")
    if not isinstance(receipt_artifacts, Mapping) or not isinstance(providers, Mapping):
        raise ValidationReceiptError("validation receipt artifact records are missing")
    expected_keys = {f"{provider_id}/{artifact_type}" for provider_id in selected for artifact_type in ("structural", "temporal")}
    if set(receipt_artifacts) != expected_keys:
        raise ValidationReceiptError("validation receipt artifact set mismatch")
    for key in sorted(expected_keys):
        provider_id, artifact_type = key.split("/", 1)
        entry = providers.get(provider_id)
        reference = entry.get(artifact_type) if isinstance(entry, Mapping) else None
        record = receipt_artifacts.get(key)
        if not isinstance(reference, Mapping) or not isinstance(record, Mapping):
            raise ValidationReceiptError(f"validation receipt record is incomplete: {key}")
        database_path = _resolve_path(root, reference.get("path"), f"provider={key}")
        manifest_path = _resolve_path(root, reference.get("manifestPath"), f"provider={key} manifest")
        if _resolve_path(root, record.get("path"), f"receipt provider={key}") != database_path:
            raise ValidationReceiptError(f"validation receipt database path mismatch: {key}")
        if _resolve_path(root, record.get("manifestPath"), f"receipt provider={key} manifest") != manifest_path:
            raise ValidationReceiptError(f"validation receipt manifest path mismatch: {key}")
        if _sha256_file(manifest_path) != str(record.get("manifestSha256") or ""):
            raise ValidationReceiptError(f"provider artifact manifest changed after validation: {key}")
        if str(record.get("artifactKey") or "") != str(reference.get("artifactKey") or ""):
            raise ValidationReceiptError(f"provider artifact key changed after validation: {key}")
        if str(record.get("sha256") or "") != str(reference.get("sha256") or ""):
            raise ValidationReceiptError(f"provider artifact digest changed in release: {key}")
        if int(record.get("size") or 0) != int(reference.get("size") or 0):
            raise ValidationReceiptError(f"provider artifact size changed in release: {key}")
        if not _same_stat(database_path, record.get("databaseStat")):
            raise ValidationReceiptError(f"provider artifact changed after validation: {key}")

    return receipt
