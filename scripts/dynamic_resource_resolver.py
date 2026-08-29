"""Safe resolution of versioned public GTFS resources and manifests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Mapping
from urllib.parse import urljoin
from urllib.request import Request, urlopen


MAX_METADATA_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30
_DOWNLOAD_LINK = re.compile(
    r"href=[\"'](?P<href>/hdb/download/[0-9]+/)[\"']",
    re.IGNORECASE,
)
_GTFS_ROW = re.compile(r"<tr\b.*?</tr>", re.IGNORECASE | re.DOTALL)
_DATE = re.compile(r"\b(20[0-9]{2}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})\b")


class DynamicResourceError(ValueError):
    """A dynamic metadata endpoint did not resolve to a safe resource."""


@dataclass(frozen=True)
class ResolvedResource:
    url: str
    version: str
    metadata: dict[str, object]


def _validate_http_url(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        raise DynamicResourceError(f"{field} is not an HTTP(S) URL")
    return value.strip()


def _read_metadata(url: str, *, headers: Mapping[str, str] | None = None) -> bytes:
    request = Request(
        _validate_http_url(url, field="metadata URL"),
        headers={
            "Accept": "application/json, text/html;q=0.9",
            "User-Agent": "HalteWeckerDynamicResourceResolver/1.0",
            **{str(key): str(value) for key, value in (headers or {}).items()},
        },
    )
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_METADATA_BYTES:
                raise DynamicResourceError("dynamic metadata exceeds size limit")
            body = response.read(MAX_METADATA_BYTES + 1)
    except DynamicResourceError:
        raise
    except Exception as error:
        raise DynamicResourceError("dynamic metadata unavailable") from error
    if len(body) > MAX_METADATA_BYTES:
        raise DynamicResourceError("dynamic metadata exceeds size limit")
    return body


def resolve_ckan_gtfs_zip(
    metadata_url: str,
    *,
    package_id: str,
) -> ResolvedResource:
    """Choose the newest active ZIP resource from a CKAN package."""
    try:
        payload = json.loads(_read_metadata(metadata_url).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DynamicResourceError("CKAN metadata is not valid JSON") from error
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise DynamicResourceError("CKAN package_show failed")
    result = payload.get("result")
    if not isinstance(result, dict) or str(result.get("id", "")) != package_id:
        raise DynamicResourceError("CKAN package ID mismatch")
    candidates: list[tuple[str, dict[str, object]]] = []
    for resource in result.get("resources", []):
        if not isinstance(resource, dict) or str(resource.get("state", "active")) != "active":
            continue
        resource_url = resource.get("url")
        name = str(resource.get("name", ""))
        fmt = str(resource.get("format", ""))
        if not isinstance(resource_url, str) or not resource_url.lower().split("?", 1)[0].endswith(".zip"):
            continue
        if fmt.upper() != "ZIP" and not name.lower().endswith(".zip"):
            continue
        version = str(resource.get("last_modified") or resource.get("created") or resource.get("id") or "")
        candidates.append((version, resource))
    if not candidates:
        raise DynamicResourceError("CKAN package has no active GTFS ZIP resource")
    version, resource = max(
        candidates,
        key=lambda item: (item[0], str(item[1].get("id", ""))),
    )
    return ResolvedResource(
        url=_validate_http_url(resource.get("url"), field="CKAN resource URL"),
        version=f"{version}:{resource.get('id', '')}",
        metadata={
            "resolver": "ckanGTFS",
            "packageID": package_id,
            "resourceID": str(resource.get("id", "")),
            "resourceName": str(resource.get("name", "")),
            "license": result.get("license_title"),
        },
    )


def resolve_wroclaw_gtfs_zip(metadata_url: str) -> ResolvedResource:
    """Choose the newest GTFS ZIP link from the official Wrocław HTML catalog."""
    try:
        html = _read_metadata(metadata_url).decode("utf-8")
    except UnicodeDecodeError as error:
        raise DynamicResourceError("Wrocław catalog is not UTF-8") from error
    candidates: list[tuple[str, str, str]] = []
    for row in _GTFS_ROW.findall(html):
        lowered = unescape(row).lower()
        if "gtfs" not in lowered or "zip" not in lowered:
            continue
        date_match = _DATE.search(unescape(row))
        if date_match is None:
            continue
        link_match = _DOWNLOAD_LINK.search(row)
        if link_match is None:
            continue
        candidates.append((date_match.group(1), link_match.group("href"), date_match.group(1)))
    if not candidates:
        raise DynamicResourceError("Wrocław catalog has no valid GTFS ZIP resource")
    version, href, published_at = max(candidates, key=lambda item: item[0])
    return ResolvedResource(
        url=urljoin(metadata_url, href),
        version=version,
        metadata={"resolver": "wroclawGTFS", "publishedAt": published_at},
    )


def resolve_gtfs_resource(source: dict[str, object]) -> ResolvedResource | None:
    """Resolve a source's optional dynamicResource declaration."""
    configuration = source.get("dynamicResource")
    if configuration is None:
        return None
    if not isinstance(configuration, dict):
        raise DynamicResourceError("dynamicResource must be an object")
    kind = str(configuration.get("kind", "")).strip()
    metadata_url = _validate_http_url(
        configuration.get("metadataURL"),
        field="dynamicResource.metadataURL",
    )
    if kind == "ckanGTFS":
        package_id = configuration.get("packageID")
        if not isinstance(package_id, str) or not package_id.strip():
            raise DynamicResourceError("ckanGTFS requires packageID")
        return resolve_ckan_gtfs_zip(metadata_url, package_id=package_id)
    if kind == "wroclawGTFS":
        return resolve_wroclaw_gtfs_zip(metadata_url)
    raise DynamicResourceError(f"unsupported dynamicResource kind: {kind}")


def resolve_realtime_manifest(metadata_url: str) -> dict[str, str]:
    """Normalize a provider manifest into stable realtime capability keys."""
    try:
        payload = json.loads(_read_metadata(metadata_url).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DynamicResourceError("realtime manifest is not valid JSON") from error
    if not isinstance(payload, dict):
        raise DynamicResourceError("realtime manifest must be an object")
    source = payload.get("GTFS-RT", payload)
    if not isinstance(source, dict):
        raise DynamicResourceError("realtime manifest has no GTFS-RT object")
    result: dict[str, str] = {}
    for raw_key, raw_url in source.items():
        if not isinstance(raw_url, str) or not raw_url.startswith(("https://", "http://")):
            continue
        key = re.sub(r"[^a-z0-9]", "", str(raw_key).lower())
        if key in {"all", "combined"}:
            result["combined"] = raw_url
        elif key in {"tripupdates", "tripupdate"}:
            result["tripUpdates"] = raw_url
        elif key in {"vehiclepositions", "vehicleposition"}:
            result["vehiclePositions"] = raw_url
        elif key in {"servicealerts", "alerts", "alert"}:
            result["alerts"] = raw_url
    if not result:
        raise DynamicResourceError("realtime manifest has no usable endpoints")
    return result
