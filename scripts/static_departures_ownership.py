#!/usr/bin/env python3
"""Internal provider ownership metadata for static departures snapshots."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable


OWNERSHIP_SCHEMA_VERSION = "1"


def ensure_ownership_schema(connection: sqlite3.Connection) -> None:
    """Create internal ownership tables without changing public query tables."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS provider_entities (
            entity_type TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            key_1 TEXT NOT NULL,
            key_2 TEXT NOT NULL DEFAULT '',
            key_3 TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (entity_type, provider_id, key_1, key_2, key_3)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS provider_entities_by_provider
            ON provider_entities(provider_id, entity_type, key_1);
        CREATE TABLE IF NOT EXISTS provider_city_stops (
            provider_id TEXT NOT NULL,
            city_id TEXT NOT NULL,
            stop_id TEXT NOT NULL,
            PRIMARY KEY (provider_id, city_id, stop_id)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS provider_city_stops_by_city_stop
            ON provider_city_stops(city_id, stop_id, provider_id);
        CREATE TABLE IF NOT EXISTS provider_city_modes (
            provider_id TEXT NOT NULL,
            city_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            timezone TEXT NOT NULL,
            stop_id_prefix TEXT NOT NULL DEFAULT '',
            identifier_prefix TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (provider_id, city_id)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS ownership_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )
    connection.execute(
        """
        INSERT INTO ownership_metadata(key, value)
        VALUES ('schemaVersion', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (OWNERSHIP_SCHEMA_VERSION,),
    )


def has_ownership_schema(connection: sqlite3.Connection) -> bool:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    return {
        "provider_entities",
        "provider_city_stops",
        "provider_city_modes",
        "ownership_metadata",
    }.issubset(tables)


def register_entities(
    connection: sqlite3.Connection,
    provider_id: str,
    entity_type: str,
    keys: Iterable[tuple[str, ...]],
) -> None:
    if not has_ownership_schema(connection):
        return
    if not provider_id.strip():
        raise ValueError("Provider ownership requires a non-empty provider ID.")
    if not entity_type.strip():
        raise ValueError("Provider ownership requires a non-empty entity type.")
    connection.executemany(
        """
        INSERT OR IGNORE INTO provider_entities(
            entity_type, provider_id, key_1, key_2, key_3
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            (
                entity_type,
                provider_id,
                key[0],
                key[1] if len(key) > 1 else "",
                key[2] if len(key) > 2 else "",
            )
            for key in keys
        ),
    )


def register_city_stops(
    connection: sqlite3.Connection,
    provider_id: str,
    memberships: Iterable[tuple[str, str]],
) -> None:
    if not has_ownership_schema(connection):
        return
    connection.executemany(
        """
        INSERT OR IGNORE INTO provider_city_stops(provider_id, city_id, stop_id)
        VALUES (?, ?, ?)
        """,
        ((provider_id, city_id, stop_id) for city_id, stop_id in memberships),
    )


def register_city_mode(
    connection: sqlite3.Connection,
    provider_id: str,
    city_id: str,
    mode: str,
    timezone: str,
    stop_id_prefix: str = "",
    identifier_prefix: str = "",
) -> None:
    if not has_ownership_schema(connection):
        return
    connection.execute(
        """
        INSERT INTO provider_city_modes(
            provider_id, city_id, mode, timezone, stop_id_prefix, identifier_prefix
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider_id, city_id) DO UPDATE SET
            mode=excluded.mode,
            timezone=excluded.timezone,
            stop_id_prefix=excluded.stop_id_prefix,
            identifier_prefix=excluded.identifier_prefix
        """,
        (
            provider_id,
            city_id,
            mode,
            timezone,
            stop_id_prefix,
            identifier_prefix,
        ),
    )


def rebuild_city_stops(connection: sqlite3.Connection) -> None:
    if not has_ownership_schema(connection):
        return
    connection.execute("DELETE FROM city_stops")
    connection.execute(
        """
        INSERT OR IGNORE INTO city_stops(city_id, stop_id)
        SELECT city_id, stop_id
        FROM provider_city_stops
        ORDER BY city_id, stop_id, provider_id
        """
    )


def rebuild_city_departure_modes(connection: sqlite3.Connection) -> None:
    if not has_ownership_schema(connection):
        return
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "city_departure_modes" not in tables:
        return
    connection.execute("DELETE FROM city_departure_modes")
    connection.execute(
        """
        INSERT OR IGNORE INTO city_departure_modes(
            city_id, mode, timezone, stop_id_prefix, identifier_prefix
        )
        SELECT city_id, mode, timezone, stop_id_prefix, identifier_prefix
        FROM provider_city_modes
        ORDER BY city_id, provider_id
        """
    )


def _provider_placeholders(provider_ids: list[str]) -> str:
    return ",".join("?" for _ in provider_ids)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})")
    }


def delete_provider_data(
    connection: sqlite3.Connection,
    provider_ids: Iterable[str],
) -> None:
    """Remove only rows owned by providers, preserving shared keys if needed."""
    selected = sorted({provider_id.strip() for provider_id in provider_ids if provider_id.strip()})
    if not selected:
        raise ValueError("At least one provider is required for scoped deletion.")
    if not has_ownership_schema(connection):
        raise ValueError(
            "Active static departures database has no provider ownership metadata; "
            "run the canonical full pipeline once before using scoped rebuild."
        )

    placeholders = _provider_placeholders(selected)
    params = tuple(selected)
    owned = (
        "SELECT key_1 FROM provider_entities "
        f"WHERE entity_type = ? AND provider_id IN ({placeholders})"
    )
    def other_owner(entity_type: str, column: str) -> str:
        return (
            "NOT EXISTS (SELECT 1 FROM provider_entities other "
            f"WHERE other.entity_type = ? AND other.key_1 = target.{column} "
            f"AND other.provider_id NOT IN ({placeholders}))"
        )

    connection.execute(
        """
        DELETE FROM stop_times
        WHERE trip_id IN (
            SELECT key_1 FROM provider_entities
            WHERE entity_type = 'trips' AND provider_id IN (%s)
        )
        AND NOT EXISTS (
            SELECT 1 FROM provider_entities other
            WHERE other.entity_type = 'trips'
              AND other.key_1 = stop_times.trip_id
              AND other.provider_id NOT IN (%s)
        )
        """ % (placeholders, placeholders),
        params + params,
    )
    connection.execute(
        """
        DELETE FROM active_services
        WHERE service_id IN (
            SELECT key_1 FROM provider_entities
            WHERE entity_type = 'calendar' AND provider_id IN (%s)
            UNION
            SELECT key_1 FROM provider_entities
            WHERE entity_type = 'calendar_dates' AND provider_id IN (%s)
        )
        AND NOT EXISTS (
            SELECT 1 FROM provider_entities other
            WHERE other.entity_type IN ('calendar', 'calendar_dates')
              AND other.key_1 = active_services.service_id
              AND other.provider_id NOT IN (%s)
        )
        """ % (placeholders, placeholders, placeholders),
        params + params + params,
    )
    connection.execute(
        """
        DELETE FROM calendar_dates
        WHERE service_id IN (
            SELECT key_1 FROM provider_entities
            WHERE entity_type = 'calendar_dates' AND provider_id IN (%s)
        )
        AND NOT EXISTS (
            SELECT 1 FROM provider_entities other
            WHERE other.entity_type = 'calendar_dates'
              AND other.key_1 = calendar_dates.service_id
              AND other.provider_id NOT IN (%s)
        )
        """ % (placeholders, placeholders),
        params + params,
    )
    if _table_exists(connection, "transfers"):
        transfer_columns = _table_columns(connection, "transfers")
        if {
            "from_stop_id",
            "to_stop_id",
            "from_trip_id",
            "to_trip_id",
            "from_route_id",
            "to_route_id",
        }.issubset(transfer_columns):
            transfer_identity = (
                "target.from_stop_id || char(31) || target.to_stop_id || char(31) || "
                "target.from_trip_id || char(31) || target.to_trip_id || char(31) || "
                "target.from_route_id || char(31) || target.to_route_id"
            )
            connection.execute(
                """
                DELETE FROM transfers AS target
                WHERE EXISTS (
                    SELECT 1 FROM provider_entities owned
                    WHERE owned.entity_type = 'transfers'
                      AND owned.provider_id IN (%s)
                      AND owned.key_1 = %s
                )
                AND NOT EXISTS (
                    SELECT 1 FROM provider_entities other
                    WHERE other.entity_type = 'transfers'
                      AND other.provider_id NOT IN (%s)
                      AND other.key_1 = %s
                )
                """ % (placeholders, transfer_identity, placeholders, transfer_identity),
                params + params,
            )
        elif {"from_stop_id", "to_stop_id", "transfer_type"}.issubset(transfer_columns):
            connection.execute(
                """
                DELETE FROM transfers AS target
                WHERE EXISTS (
                    SELECT 1 FROM provider_entities owned
                    WHERE owned.entity_type = 'transfers'
                      AND owned.provider_id IN (%s)
                      AND owned.key_1 = target.from_stop_id
                      AND owned.key_2 = target.to_stop_id
                      AND owned.key_3 = CAST(target.transfer_type AS TEXT)
                )
                AND NOT EXISTS (
                    SELECT 1 FROM provider_entities other
                    WHERE other.entity_type = 'transfers'
                      AND other.provider_id NOT IN (%s)
                      AND other.key_1 = target.from_stop_id
                      AND other.key_2 = target.to_stop_id
                      AND other.key_3 = CAST(target.transfer_type AS TEXT)
                )
                """ % (placeholders, placeholders),
                params + params,
            )
        else:
            raise ValueError("Unsupported transfers table schema.")
    if _table_exists(connection, "pathways"):
        connection.execute(
            """
            DELETE FROM pathways AS target
            WHERE EXISTS (
                SELECT 1 FROM provider_entities owned
                WHERE owned.entity_type = 'pathways'
                  AND owned.provider_id IN (%s)
                  AND owned.key_1 = target.pathway_id
                  AND owned.key_2 = target.from_stop_id
                  AND owned.key_3 = target.to_stop_id
            )
            AND NOT EXISTS (
                SELECT 1 FROM provider_entities other
                WHERE other.entity_type = 'pathways'
                  AND other.provider_id NOT IN (%s)
                  AND other.key_1 = target.pathway_id
                  AND other.key_2 = target.from_stop_id
                  AND other.key_3 = target.to_stop_id
            )
            """ % (placeholders, placeholders),
            params + params,
        )
    for table, entity_type, column in (
        ("trips", "trips", "trip_id"),
        ("routes", "routes", "route_id"),
        ("calendar", "calendar", "service_id"),
        ("raw_stops", "raw_stops", "stop_id"),
    ):
        connection.execute(
            f"""
            DELETE FROM {table} AS target
            WHERE {column} IN ({owned})
              AND {other_owner(entity_type, column)}
            """,
            (entity_type,) + params + (entity_type,) + params,
        )

    connection.execute(
        f"DELETE FROM provider_city_stops WHERE provider_id IN ({placeholders})",
        params,
    )
    connection.execute(
        f"DELETE FROM provider_city_modes WHERE provider_id IN ({placeholders})",
        params,
    )
    connection.execute(
        f"DELETE FROM provider_entities WHERE provider_id IN ({placeholders})",
        params,
    )
    rebuild_city_stops(connection)
    rebuild_city_departure_modes(connection)
