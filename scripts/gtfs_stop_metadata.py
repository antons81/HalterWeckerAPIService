"""Display-only metadata extracted from GTFS stop descriptions."""

from __future__ import annotations

import re


_LABELS = r"platform|platform_code|רציף|floor|level|קומה"
_FIELD_PATTERN = re.compile(
    rf"(?:^|[|;,]\s*|\s)(?P<label>{_LABELS})\s*[:：-]",
    re.IGNORECASE,
)


def _first_value(labels: set[str], value: str) -> str:
    match = _FIELD_PATTERN.search(value)
    while match is not None:
        if match.group("label").casefold() in labels:
            next_match = _FIELD_PATTERN.search(value, match.end())
            end = next_match.start() if next_match else len(value)
            return value[match.end():end].strip(" |;,")
        match = _FIELD_PATTERN.search(value, match.end())
    return ""


def display_stop_metadata(
    stop_desc: object,
    platform_code: object = "",
) -> tuple[str, str]:
    """Return display platform/floor without mutating the source fields."""
    description = str(stop_desc or "").strip()
    platform = str(platform_code or "").strip()
    if not platform:
        platform = _first_value({"platform", "platform_code", "רציף"}, description)
    floor = _first_value({"floor", "level", "קומה"}, description)
    return platform, floor
