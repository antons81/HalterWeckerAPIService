"""Read-only agency scoping for multi-agency GTFS archives."""

from __future__ import annotations

import csv
import io
import os
import tempfile
import zipfile
from pathlib import Path


_FILTERED_TABLES = {
    "agency.txt",
    "routes.txt",
    "trips.txt",
    "stops.txt",
    "stop_times.txt",
    "calendar.txt",
    "calendar_dates.txt",
    "frequencies.txt",
    "transfers.txt",
    "pathways.txt",
    "shapes.txt",
    "fare_rules.txt",
}


def _rows(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    if name not in archive.namelist():
        return []
    with archive.open(name) as source:
        return list(csv.DictReader(io.TextIOWrapper(source, encoding="utf-8-sig")))


def _write_rows(archive: zipfile.ZipFile, name: str, rows: list[dict[str, str]]) -> bytes:
    if not rows:
        return b""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


class AgencyScopedArchive:
    """ZipFile-compatible filtered archive that owns its temporary file."""

    def __init__(self, original: zipfile.ZipFile, agency_id: str) -> None:
        self._original = original
        self._temporary_path: Path | None = None
        self._archive = self._build(agency_id)

    def _build(self, agency_id: str) -> zipfile.ZipFile:
        agency_rows = _rows(self._original, "agency.txt")
        route_rows = _rows(self._original, "routes.txt")
        if not agency_rows or not route_rows:
            raise ValueError("Agency filtering requires agency.txt and routes.txt")
        selected_routes = {
            str(row.get("route_id", "")).strip()
            for row in route_rows
            if str(row.get("agency_id", "")).strip() == agency_id
            and str(row.get("route_id", "")).strip()
        }
        if not selected_routes:
            raise ValueError(f"GTFS agency_id {agency_id!r} has no routes")

        trip_rows = [
            row for row in _rows(self._original, "trips.txt")
            if str(row.get("route_id", "")).strip() in selected_routes
        ]
        selected_trips = {
            str(row.get("trip_id", "")).strip()
            for row in trip_rows
            if str(row.get("trip_id", "")).strip()
        }
        stop_time_rows = [
            row for row in _rows(self._original, "stop_times.txt")
            if str(row.get("trip_id", "")).strip() in selected_trips
        ]
        selected_stops = {
            str(row.get("stop_id", "")).strip()
            for row in stop_time_rows
            if str(row.get("stop_id", "")).strip()
        }
        all_stop_rows = _rows(self._original, "stops.txt")
        stop_by_id = {
            str(row.get("stop_id", "")).strip(): row
            for row in all_stop_rows
            if str(row.get("stop_id", "")).strip()
        }
        pending = list(selected_stops)
        while pending:
            stop_id = pending.pop()
            parent_id = str(stop_by_id.get(stop_id, {}).get("parent_station", "")).strip()
            if parent_id and parent_id not in selected_stops:
                selected_stops.add(parent_id)
                pending.append(parent_id)
        selected_services = {
            str(row.get("service_id", "")).strip()
            for row in trip_rows
            if str(row.get("service_id", "")).strip()
        }
        selected_shapes = {
            str(row.get("shape_id", "")).strip()
            for row in trip_rows
            if str(row.get("shape_id", "")).strip()
        }
        selected_agency = [
            row for row in agency_rows
            if str(row.get("agency_id", "")).strip() == agency_id
        ]

        def filter_rows(name: str, rows: list[dict[str, str]]) -> list[dict[str, str]]:
            if name == "agency.txt":
                return selected_agency
            if name == "routes.txt":
                return [row for row in rows if row.get("route_id", "").strip() in selected_routes]
            if name == "trips.txt":
                return [row for row in rows if row.get("trip_id", "").strip() in selected_trips]
            if name == "stops.txt":
                return [row for row in rows if row.get("stop_id", "").strip() in selected_stops]
            if name == "stop_times.txt":
                return [row for row in rows if row.get("trip_id", "").strip() in selected_trips]
            if name in {"calendar.txt", "calendar_dates.txt"}:
                return [row for row in rows if row.get("service_id", "").strip() in selected_services]
            if name == "frequencies.txt":
                return [row for row in rows if row.get("trip_id", "").strip() in selected_trips]
            if name == "transfers.txt":
                return [row for row in rows if row.get("from_stop_id", "").strip() in selected_stops and row.get("to_stop_id", "").strip() in selected_stops]
            if name == "pathways.txt":
                return [row for row in rows if row.get("from_stop_id", "").strip() in selected_stops and row.get("to_stop_id", "").strip() in selected_stops]
            if name == "shapes.txt":
                return [row for row in rows if row.get("shape_id", "").strip() in selected_shapes]
            if name == "fare_rules.txt":
                return [row for row in rows if row.get("route_id", "").strip() in selected_routes]
            return rows

        fd, path = tempfile.mkstemp(prefix="haltewecker-agency-", suffix=".zip")
        os.close(fd)
        self._temporary_path = Path(path)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for name in self._original.namelist():
                basename = Path(name).name
                if basename in _FILTERED_TABLES:
                    rows = filter_rows(basename, _rows(self._original, name))
                    if rows:
                        output.writestr(name, _write_rows(output, basename, rows))
                else:
                    output.writestr(name, self._original.read(name))
        return zipfile.ZipFile(path)

    def namelist(self) -> list[str]:
        return self._archive.namelist()

    def open(self, name: str):
        return self._archive.open(name)

    def close(self) -> None:
        self._archive.close()
        self._original.close()
        if self._temporary_path is not None:
            self._temporary_path.unlink(missing_ok=True)
            self._temporary_path = None


def agency_scoped_archive(archive: zipfile.ZipFile, agency_id: object | None):
    value = str(agency_id or "").strip()
    return AgencyScopedArchive(archive, value) if value else archive
