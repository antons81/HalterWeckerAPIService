"""Generic CSV readers for GTFS text tables."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from typing import Any

ASCII_WHITESPACE = " \t\r\n\v\f"


class GTFSHeaderError(ValueError):
    """Raised when a GTFS header cannot be normalized safely."""


def _skip_leading_blank_lines(source: Iterable[str]) -> Iterator[str]:
    """Skip only whitespace-only lines before the CSV header."""
    leading = True
    for line in source:
        if leading and not line.strip(ASCII_WHITESPACE):
            continue
        leading = False
        yield line


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
    """Read CSV rows after skipping leading blanks and normalizing header names."""

    def __init__(self, source: Iterable[str], **kwargs: Any) -> None:
        self._reader = csv.DictReader(_skip_leading_blank_lines(source), **kwargs)
        self._raw_fieldnames = list(self._reader.fieldnames or [])
        self.fieldnames = _normalized_fieldnames(self._raw_fieldnames)

    def __iter__(self) -> NormalizedGTFSReader:
        return self

    def __next__(self) -> dict[str, Any]:
        raw_row = next(self._reader)
        row = {
            normalized_name: raw_row.get(raw_name)
            for raw_name, normalized_name in zip(self._raw_fieldnames, self.fieldnames)
        }
        if None in raw_row:
            row[None] = raw_row[None]
        return row


def normalized_dict_reader(
    source: Iterable[str], **kwargs: Any
) -> NormalizedGTFSReader:
    """Return a GTFS reader with leading blanks skipped and header names normalized."""

    return NormalizedGTFSReader(source, **kwargs)
