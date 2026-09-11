"""Deterministic fingerprint for future derived-artifact reuse."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


STOP_DATA_BUILD_FINGERPRINT_VERSION = 2
# Bump when content-affecting orchestration policy changes in the shell pipeline.
STOP_DATA_ORCHESTRATION_VERSION = 1

STOP_DATA_INPUTS = (
    "config/cities.json",
    "config/swiss-cities.json",
    "config/austrian-sources.json",
    "config/external-gtfs-sources.json",
    "config/australia-cities.json",
    "config/ireland-cities.json",
    "scripts/build_stop_packages.py",
    "scripts/kyiv_open_data.py",
    "scripts/external_gtfs.py",
    "scripts/build_swiss_departure_index.py",
    "scripts/preserve_nl_assets.py",
    # Provenance serialization is part of the validated stop-data manifest contract.
    "scripts/artifact_provenance.py",
)
DEFAULT_INPUTS = STOP_DATA_INPUTS


def component_manifest(
    repository: Path,
    inputs: tuple[str, ...] = STOP_DATA_INPUTS,
    *,
    version: int = STOP_DATA_BUILD_FINGERPRINT_VERSION,
) -> dict[str, object]:
    components = []
    for relative in inputs:
        path = repository / relative
        components.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {
        "version": version,
        "orchestrationVersion": STOP_DATA_ORCHESTRATION_VERSION,
        "components": components,
    }


def compute(
    repository: Path,
    inputs: tuple[str, ...] = STOP_DATA_INPUTS,
    *,
    version: int = STOP_DATA_BUILD_FINGERPRINT_VERSION,
) -> str:
    digest = hashlib.sha256()
    manifest = component_manifest(repository, inputs, version=version)
    digest.update(f"version:{manifest['version']}\n".encode())
    digest.update(
        f"orchestration-version:{manifest['orchestrationVersion']}\n".encode()
    )
    for component in manifest["components"]:
        relative = str(component["path"])
        digest.update(f"path:{relative}\n".encode())
        digest.update((repository / relative).read_bytes())
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(compute(args.repository))


if __name__ == "__main__":
    main()
