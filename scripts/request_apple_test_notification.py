"""Request an App Store Server API test notification for a local app configuration."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, TextIO

import requests
from appstoreserverlibrary.api_client import APIException, AppStoreServerAPIClient
from appstoreserverlibrary.models.Environment import Environment


REQUIRED_ENV_VARS = (
    "APPLE_ISSUER_ID",
    "APPLE_KEY_ID",
    "APPLE_IAP_PRIVATE_KEY_PATH",
    "APPLE_BUNDLE_ID",
    "APPLE_APPLE_ID",
    "APPLE_ENVIRONMENT",
)


class ConfigurationError(ValueError):
    """Raised when the local Apple API configuration is incomplete or invalid."""


@dataclass(frozen=True)
class AppleAPIConfig:
    issuer_id: str
    key_id: str
    private_key_path: Path
    bundle_id: str
    apple_id: str
    environment: Environment


def parse_environment(value: str) -> Environment:
    """Parse the supported App Store Server API environments."""
    normalized = value.strip().lower()
    environments = {
        "production": Environment.PRODUCTION,
        "sandbox": Environment.SANDBOX,
    }
    try:
        return environments[normalized]
    except KeyError as error:
        raise ConfigurationError(
            f"Invalid APPLE_ENVIRONMENT {value!r}; expected production or sandbox"
        ) from error


def load_config(environ: Mapping[str, str] | None = None) -> AppleAPIConfig:
    """Load and validate all required configuration without reading the key."""
    values = os.environ if environ is None else environ
    missing = [name for name in REQUIRED_ENV_VARS if not values.get(name)]
    if missing:
        raise ConfigurationError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )

    private_key_path = Path(values["APPLE_IAP_PRIVATE_KEY_PATH"]).expanduser()
    if not private_key_path.is_file():
        raise ConfigurationError(f"Private key file not found: {private_key_path}")

    apple_id = values["APPLE_APPLE_ID"]
    if not apple_id.isdigit():
        raise ConfigurationError("APPLE_APPLE_ID must contain a numeric App Apple ID")

    return AppleAPIConfig(
        issuer_id=values["APPLE_ISSUER_ID"],
        key_id=values["APPLE_KEY_ID"],
        private_key_path=private_key_path,
        bundle_id=values["APPLE_BUNDLE_ID"],
        apple_id=apple_id,
        environment=parse_environment(values["APPLE_ENVIRONMENT"]),
    )


def _api_error_details(error: APIException) -> str:
    details = [f"http_status={error.http_status_code}"]
    if error.raw_api_error is not None:
        details.append(f"apple_error_code={error.raw_api_error}")
    if error.error_message:
        details.append(f"message={error.error_message}")
    return " ".join(details)


def request_test_notification(config: AppleAPIConfig) -> str:
    """Call Apple's official client and return only the test notification token."""
    try:
        signing_key = config.private_key_path.read_bytes()
    except OSError as error:
        raise ConfigurationError(
            f"Unable to read private key file: {config.private_key_path}"
        ) from error

    client = AppStoreServerAPIClient(
        signing_key=signing_key,
        key_id=config.key_id,
        issuer_id=config.issuer_id,
        bundle_id=config.bundle_id,
        environment=config.environment,
    )
    response = client.request_test_notification()
    token = response.testNotificationToken
    if not isinstance(token, str) or not token:
        raise RuntimeError("Apple API response did not contain testNotificationToken")
    return token


def _write_error(stderr: TextIO, message: str) -> int:
    print(message, file=stderr)
    return 1


def main(
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the local utility and return a process exit code."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    try:
        config = load_config(environ)
        token = request_test_notification(config)
    except ConfigurationError as error:
        return _write_error(stderr, f"configuration error: {error}")
    except APIException as error:
        details = _api_error_details(error)
        if error.http_status_code in {401, 403}:
            return _write_error(stderr, f"Apple API authentication failure: {details}")
        return _write_error(stderr, f"Apple API HTTP error: {details}")
    except requests.RequestException as error:
        return _write_error(stderr, f"Apple API request failed: {error}")
    except ValueError:
        return _write_error(stderr, "configuration error: invalid private key or client configuration")
    except RuntimeError as error:
        return _write_error(stderr, str(error))

    print(f"environment={config.environment.value}", file=stdout)
    print(f"bundleId={config.bundle_id}", file=stdout)
    print(f"testNotificationToken={token}", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
