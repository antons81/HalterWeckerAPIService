"""Generic CSV readers for GTFS text tables."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from typing import Any


ASCII_WHITESPACE = " \t\r\n\v\f"


class GTFSHeaderError(ValueError):
    """Raised when a GTFS header cannot be normalized safely."""


def _normalized_fieldnames(fieldnames: list[str] | None) -> list[str]:
    if fieldnames is None:
        return []

    normalized = [fieldname.strip(ASCII_WHITESPACE) for fieldname in fieldnames]
    duplicates = sorted(
        {fieldname for fieldname in normalized if normalized.count(fieldname) > 1}
    )
    if duplicates:
        duplicate_names = ", ".join(repr(name) for name in duplicates)
        raise GTFSHeaderError(
            f"Duplicate GTFS header names after whitespace normalization: {duplicate_names}"
        )
    return normalized


class NormalizedGTFSReader(Iterator[dict[str, Any]]):
    """Read CSV rows while normalizing only GTFS header field names."""

    def __init__(self, source: Iterable[str], **kwargs: Any) -> None:
        self._reader = csv.DictReader(source, **kwargs)
        self._raw_fieldnames = list(self._reader.fieldnames or [])
        self.fieldnames = _normalized_fieldnames(self._raw_fieldnames)

    def __iter__(self) -> "NormalizedGTFSReader":
        return self

    def __next__(self) -> dict[str, Any]:
        raw_row = next(self._reader)
        row = {
            normalized_name: raw_row.get(raw_name)
            for raw_name, normalized_name in zip(
                self._raw_fieldnames, self.fieldnames
            )
        }
        if None in raw_row:
            row[None] = raw_row[None]
        return row


def normalized_dict_reader(source: Iterable[str], **kwargs: Any) -> NormalizedGTFSReader:
    """Return a GTFS reader with ASCII whitespace stripped from field names."""

    return NormalizedGTFSReader(source, **kwargs)
