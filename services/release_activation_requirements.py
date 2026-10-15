"""Fail-closed requirements for the shared production transit release."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from .static_departures_runtime import load_release_manifest


class ActivationRequirementError(ValueError):
    """A candidate cannot serve a mandatory production consumer."""


def validate_provider_contract(
    build_provider_ids: tuple[str, ...], runtime_provider_ids: tuple[str, ...],
    supported_runtime_provider_ids: tuple[str, ...],
) -> None:
    """Release membership and image capabilities are separate contracts."""
    build = set(build_provider_ids)
    runtime = set(runtime_provider_ids)
    if "germany" not in build:
        raise ActivationRequirementError("mandatory build provider germany is missing")
    if not runtime:
        raise ActivationRequirementError("runtime provider list is empty")
    if not runtime <= build:
        raise ActivationRequirementError("runtime providers are absent from the build release")
    unsupported = runtime - set(supported_runtime_provider_ids)
    if unsupported:
        raise ActivationRequirementError(f"unsupported runtime providers: {', '.join(sorted(unsupported))}")


def validate_shared_release(
    release_root: Path | str, *, validation_date: date | None = None,
    require_current_services: bool = False,
) -> dict[str, object]:
    """Validate Germany independently of a configurable provider allowlist.

    Receipts can accelerate artifact validation, but never bypass these
    consumer requirements. Calendar dates are validated structurally; the
    current date is not required to have service. All connections are read-only
    and deterministically closed. No release files or production pointers are
    changed.
    """
    root = Path(release_root).resolve()
    payload = json.loads((root / "release.json").read_text(encoding="utf-8"))
    germany = payload.get("providers", {}).get("germany")
    if not isinstance(germany, dict) or germany.get("status") != "active":
        raise ActivationRequirementError("mandatory production provider germany is missing or inactive")
    if germany.get("required") is not True:
        raise ActivationRequirementError("production provider germany must be required")
    manifest = load_release_manifest(root, provider_ids=("germany",))
    references = manifest.providers["germany"]
    today = validation_date or date.today()
    start = references.temporal.valid_from
    end = references.temporal.valid_through
    if not start or not end:
        raise ActivationRequirementError("Germany temporal coverage bounds are missing")
    try:
        temporal_start = date.fromisoformat(start)
        temporal_end = date.fromisoformat(end)
    except ValueError as error:
        raise ActivationRequirementError("Germany temporal coverage bounds are invalid") from error
    if temporal_start > temporal_end:
        raise ActivationRequirementError("Germany temporal coverage bounds are reversed")
    if require_current_services and not (temporal_start <= today <= temporal_end):
        raise ActivationRequirementError("Germany temporal coverage does not include the validation date")

    def connection(path: Path):
        return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True))

    with connection(manifest.common_database_path) as common:
        common_cities = {str(row[0]) for row in common.execute(
            "SELECT city_id FROM provider_city_modes WHERE provider_id=?", ("germany",)
        )}
        with connection(references.structural.database_path) as structural:
            structural_cities = {str(row[0]) for row in structural.execute(
                "SELECT city_id FROM provider_city_modes WHERE provider_id=?", ("germany",)
            )}
            declared = {str(city) for city in germany.get("cities", ())}
            if not declared or common_cities != structural_cities or not declared <= common_cities:
                raise ActivationRequirementError("Germany city/provider mappings are incomplete or inconsistent")
            for city in common_cities:
                if common.execute(
                    "SELECT 1 FROM provider_city_stops WHERE provider_id=? AND city_id=? LIMIT 1",
                    ("germany", city),
                ).fetchone() is None:
                    raise ActivationRequirementError(f"Germany city has no stop routing: {city}")
            for table in ("routes", "trips", "stop_times"):
                if structural.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None:
                    raise ActivationRequirementError(f"Germany structural table is empty: {table}")
    with connection(references.temporal.database_path) as temporal:
        try:
            # The temporal database and its schema must exist even when this
            # release has no service on the validation date.
            temporal.execute("SELECT 1 FROM active_services LIMIT 1").fetchone()
        except sqlite3.Error as error:
            raise ActivationRequirementError("Germany temporal database is invalid") from error
        if require_current_services:
            if temporal.execute(
                "SELECT 1 FROM active_services WHERE service_date=? LIMIT 1", (today.strftime("%Y%m%d"),)
            ).fetchone() is None:
                raise ActivationRequirementError("Germany has no active services on the validation date")
    return {"status": "PASS", "releaseID": manifest.release_id,
            "requiredProvider": "germany", "germanyCityMappings": len(common_cities),
            "validFrom": start, "validThrough": end}
