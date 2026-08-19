"""Verification for Apple App Store Server Notifications V2."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier


class AppleStoreNotificationVerificationError(Exception):
    """Raised when a signed notification cannot be verified by any candidate."""


@dataclass(frozen=True)
class AppleVerifierCandidate:
    name: str
    bundle_id: str
    app_apple_id: int
    environment: Environment
    verifier: SignedDataVerifier


@dataclass(frozen=True)
class VerifiedAppleNotification:
    notification_uuid: str | None
    notification_type: str | None
    subtype: str | None
    bundle_id: str
    environment: str
    signed_date: int | None


def _configured_path(environment_name: str, relative_path: str) -> Path:
    configured = os.environ.get(environment_name)
    if configured:
        return Path(configured)

    container_path = Path("/app") / relative_path
    if container_path.exists():
        return container_path
    return Path(__file__).resolve().parents[1] / relative_path


def _environment_from_name(name: str) -> Environment:
    try:
        return Environment[name.upper()]
    except KeyError as error:
        raise ValueError(f"Unsupported Apple environment: {name}") from error


@lru_cache(maxsize=1)
def load_root_certificates(root_directory: str) -> tuple[bytes, ...]:
    """Load Apple roots once and keep them in memory for the service lifetime."""
    directory = Path(root_directory)
    certificates = tuple(path.read_bytes() for path in sorted(directory.glob("*.cer")))
    if not certificates:
        raise FileNotFoundError(f"No Apple Root CA certificates found in {directory}")
    return certificates


def _load_app_config(config_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    apps = payload.get("apps") if isinstance(payload, dict) else None
    if not isinstance(apps, list) or not apps:
        raise ValueError("Apple notification app configuration must contain apps")
    return apps


def build_verifier_candidates(
    config_path: Path | None = None,
    root_directory: Path | None = None,
    enable_online_checks: bool = True,
) -> tuple[AppleVerifierCandidate, ...]:
    config_path = config_path or _configured_path(
        "APPLE_STORE_NOTIFICATION_CONFIG",
        "config/apple/store-notification-apps.json",
    )
    root_directory = root_directory or _configured_path(
        "APPLE_ROOT_CERTIFICATES_DIR",
        "config/apple/root-certificates",
    )
    root_certificates = list(load_root_certificates(str(root_directory)))
    candidates: list[AppleVerifierCandidate] = []

    for app in _load_app_config(config_path):
        name = app.get("name")
        bundle_id = app.get("bundleId")
        app_apple_id = app.get("appAppleId")
        environments = app.get("environments")
        if (
            not isinstance(name, str)
            or not isinstance(bundle_id, str)
            or not isinstance(app_apple_id, int)
            or not isinstance(environments, list)
        ):
            raise ValueError("Invalid Apple notification app configuration")

        for environment_name in environments:
            environment = _environment_from_name(environment_name)
            candidates.append(
                AppleVerifierCandidate(
                    name=f"{name}-{environment_name.lower()}",
                    bundle_id=bundle_id,
                    app_apple_id=app_apple_id,
                    environment=environment,
                    verifier=SignedDataVerifier(
                        root_certificates,
                        enable_online_checks,
                        environment,
                        bundle_id,
                        app_apple_id,
                    ),
                )
            )
    return tuple(candidates)


def _raw_value(payload: Any, raw_name: str, value_name: str) -> str | None:
    raw_value = getattr(payload, raw_name, None)
    if raw_value is not None:
        return str(raw_value)
    value = getattr(payload, value_name, None)
    return str(value) if value is not None else None


def _notification_data(payload: Any) -> Any:
    for field_name in ("data", "summary", "externalPurchaseToken", "appData"):
        value = getattr(payload, field_name, None)
        if value is not None:
            return value
    return None


class AppleStoreNotificationVerifier:
    def __init__(self, candidates: Sequence[AppleVerifierCandidate]) -> None:
        if not candidates:
            raise ValueError("At least one Apple verifier candidate is required")
        self._candidates = tuple(candidates)

    @classmethod
    def from_default_configuration(cls) -> "AppleStoreNotificationVerifier":
        return cls(build_verifier_candidates())

    def verify(self, signed_payload: str) -> VerifiedAppleNotification:
        last_error: Exception | None = None
        for candidate in self._candidates:
            try:
                notification = candidate.verifier.verify_and_decode_notification(signed_payload)
                data = _notification_data(notification)
                if data is not None:
                    signed_transaction_info = getattr(data, "signedTransactionInfo", None)
                    if signed_transaction_info:
                        candidate.verifier.verify_and_decode_signed_transaction(signed_transaction_info)
                    signed_renewal_info = getattr(data, "signedRenewalInfo", None)
                    if signed_renewal_info:
                        candidate.verifier.verify_and_decode_renewal_info(signed_renewal_info)

                environment = getattr(candidate.environment, "value", candidate.environment)
                return VerifiedAppleNotification(
                    notification_uuid=getattr(notification, "notificationUUID", None),
                    notification_type=_raw_value(notification, "rawNotificationType", "notificationType"),
                    subtype=_raw_value(notification, "rawSubtype", "subtype"),
                    bundle_id=candidate.bundle_id,
                    environment=str(environment),
                    signed_date=getattr(notification, "signedDate", None),
                )
            except Exception as error:
                last_error = error

        raise AppleStoreNotificationVerificationError from last_error


@lru_cache(maxsize=1)
def default_verifier() -> AppleStoreNotificationVerifier:
    return AppleStoreNotificationVerifier.from_default_configuration()
