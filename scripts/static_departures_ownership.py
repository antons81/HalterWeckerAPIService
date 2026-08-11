#!/usr/bin/env python3
"""Internal provider ownership metadata for static departures snapshots."""

from __future__ import annotations

import sqlite3
import time
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


def _normalized_city_ids(city_ids: Iterable[str] | None) -> tuple[str, ...] | None:
    if city_ids is None:
        return None
    return tuple(sorted({city_id.strip() for city_id in city_ids if city_id.strip()}))


def provider_city_ids(
    connection: sqlite3.Connection,
    provider_ids: Iterable[str],
) -> set[str]:
    selected = tuple(sorted({provider_id.strip() for provider_id in provider_ids if provider_id.strip()}))
    if not selected or not has_ownership_schema(connection):
        return set()
    placeholders = ",".join("?" for _ in selected)
    rows = connection.execute(
        f"""
        SELECT city_id FROM provider_city_stops WHERE provider_id IN ({placeholders})
        UNION
        SELECT city_id FROM provider_city_modes WHERE provider_id IN ({placeholders})
        """,
        selected + selected,
    )
    return {str(row[0]) for row in rows}


def rebuild_city_stops(
    connection: sqlite3.Connection,
    city_ids: Iterable[str] | None = None,
) -> None:
    if not has_ownership_schema(connection):
        return
    selected = _normalized_city_ids(city_ids)
    if selected is None:
        connection.execute("DELETE FROM city_stops")
        connection.execute(
            """
            INSERT OR IGNORE INTO city_stops(city_id, stop_id)
            SELECT city_id, stop_id
            FROM provider_city_stops
            ORDER BY city_id, stop_id, provider_id
            """
        )
    elif selected:
        placeholders = ",".join("?" for _ in selected)
        connection.execute(
            f"DELETE FROM city_stops WHERE city_id IN ({placeholders})",
            selected,
        )
        connection.execute(
            f"""
            INSERT OR IGNORE INTO city_stops(city_id, stop_id)
            SELECT city_id, stop_id
            FROM provider_city_stops
            WHERE city_id IN ({placeholders})
            ORDER BY city_id, stop_id, provider_id
            """,
            selected,
        )


def rebuild_city_departure_modes(
    connection: sqlite3.Connection,
    city_ids: Iterable[str] | None = None,
) -> None:
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
    selected = _normalized_city_ids(city_ids)
    if selected is None:
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
    elif selected:
        placeholders = ",".join("?" for _ in selected)
        connection.execute(
            f"DELETE FROM city_departure_modes WHERE city_id IN ({placeholders})",
            selected,
        )
        connection.execute(
            f"""
            INSERT OR IGNORE INTO city_departure_modes(
                city_id, mode, timezone, stop_id_prefix, identifier_prefix
            )
            SELECT city_id, mode, timezone, stop_id_prefix, identifier_prefix
            FROM provider_city_modes
            WHERE city_id IN ({placeholders})
            ORDER BY city_id, provider_id
            """,
            selected,
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


def _timed_delete_statement(
    connection: sqlite3.Connection,
    statement_name: str,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> sqlite3.Cursor:
    started = time.monotonic()
    before_changes = connection.total_changes
    try:
        cursor = connection.execute(sql, parameters)
    except Exception:
        print(
            f"[ScopedDepartures] stage=delete-provider-data statement={statement_name} "
            f"duration={time.monotonic() - started:.2f}s status=error",
            flush=True,
        )
        raise
    print(
        f"[ScopedDepartures] stage=delete-provider-data statement={statement_name} "
        f"duration={time.monotonic() - started:.2f}s changes={connection.total_changes - before_changes}",
        flush=True,
    )
    return cursor


def _prepare_scoped_delete_sets(
    connection: sqlite3.Connection,
    selected: list[str],
) -> None:
    placeholders = _provider_placeholders(selected)
    params = tuple(selected)
    for table_name, column_definition in (
        (
            "scoped_foreign_entities",
            "entity_type TEXT NOT NULL, key_1 TEXT NOT NULL, key_2 TEXT NOT NULL, "
            "key_3 TEXT NOT NULL, PRIMARY KEY(entity_type, key_1, key_2, key_3)",
        ),
        ("scoped_stop_ids", "stop_id TEXT PRIMARY KEY"),
        ("scoped_trip_ids", "trip_id TEXT PRIMARY KEY"),
        ("scoped_route_ids", "route_id TEXT PRIMARY KEY"),
        ("scoped_service_ids", "service_id TEXT PRIMARY KEY"),
        ("scoped_calendar_ids", "service_id TEXT PRIMARY KEY"),
        ("scoped_calendar_date_ids", "service_id TEXT PRIMARY KEY"),
        ("scoped_transfer_keys", "transfer_key TEXT PRIMARY KEY"),
        (
            "scoped_transfer_simple_keys",
            "from_stop_id TEXT NOT NULL, to_stop_id TEXT NOT NULL, "
            "transfer_type TEXT NOT NULL, PRIMARY KEY(from_stop_id, to_stop_id, transfer_type)",
        ),
        (
            "scoped_pathway_keys",
            "pathway_id TEXT NOT NULL, from_stop_id TEXT NOT NULL, "
            "to_stop_id TEXT NOT NULL, PRIMARY KEY(pathway_id, from_stop_id, to_stop_id)",
        ),
        ):
        _timed_delete_statement(
            connection,
            f"create-{table_name}",
            f"CREATE TEMP TABLE {table_name} ({column_definition})",
        )

    _timed_delete_statement(
        connection,
        "populate-foreign-entities",
        f"""
        INSERT INTO scoped_foreign_entities(entity_type, key_1, key_2, key_3)
        SELECT DISTINCT entity_type, key_1, key_2, key_3
        FROM provider_entities
        WHERE provider_id NOT IN ({placeholders})
          AND entity_type IN (
              'raw_stops', 'trips', 'routes', 'calendar', 'calendar_dates',
              'transfers', 'pathways'
          )
        """,
        params,
    )

    _timed_delete_statement(
        connection,
        "populate-stop-ids",
        f"""
        INSERT INTO scoped_stop_ids(stop_id)
        SELECT owned.key_1
        FROM provider_entities owned
        WHERE owned.entity_type = 'raw_stops'
          AND owned.provider_id IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM scoped_foreign_entities other
              WHERE other.entity_type = 'raw_stops'
                AND other.key_1 = owned.key_1
          )
        """,
        params,
    )
    _timed_delete_statement(
        connection,
        "populate-trip-ids",
        f"""
        INSERT INTO scoped_trip_ids(trip_id)
        SELECT owned.key_1
        FROM provider_entities owned
        WHERE owned.entity_type = 'trips'
          AND owned.provider_id IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM scoped_foreign_entities other
              WHERE other.entity_type = 'trips'
                AND other.key_1 = owned.key_1
          )
        """,
        params,
    )
    _timed_delete_statement(
        connection,
        "populate-route-ids",
        f"""
        INSERT INTO scoped_route_ids(route_id)
        SELECT owned.key_1
        FROM provider_entities owned
        WHERE owned.entity_type = 'routes'
          AND owned.provider_id IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM scoped_foreign_entities other
              WHERE other.entity_type = 'routes'
                AND other.key_1 = owned.key_1
          )
        """,
        params,
    )
    _timed_delete_statement(
        connection,
        "populate-service-ids",
        f"""
        INSERT INTO scoped_service_ids(service_id)
        SELECT DISTINCT owned.key_1
        FROM provider_entities owned
        WHERE owned.entity_type IN ('calendar', 'calendar_dates')
          AND owned.provider_id IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM scoped_foreign_entities other
              WHERE other.entity_type IN ('calendar', 'calendar_dates')
                AND other.key_1 = owned.key_1
          )
        """,
        params,
    )
    _timed_delete_statement(
        connection,
        "populate-calendar-ids",
        f"""
        INSERT INTO scoped_calendar_ids(service_id)
        SELECT owned.key_1
        FROM provider_entities owned
        WHERE owned.entity_type = 'calendar'
          AND owned.provider_id IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM scoped_foreign_entities other
              WHERE other.entity_type = 'calendar'
                AND other.key_1 = owned.key_1
          )
        """,
        params,
    )
    _timed_delete_statement(
        connection,
        "populate-calendar-date-ids",
        f"""
        INSERT INTO scoped_calendar_date_ids(service_id)
        SELECT DISTINCT owned.key_1
        FROM provider_entities owned
        WHERE owned.entity_type = 'calendar_dates'
          AND owned.provider_id IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM scoped_foreign_entities other
              WHERE other.entity_type = 'calendar_dates'
                AND other.key_1 = owned.key_1
          )
        """,
        params,
    )
    _timed_delete_statement(
        connection,
        "populate-transfer-keys",
        f"""
        INSERT INTO scoped_transfer_keys(transfer_key)
        SELECT owned.key_1
        FROM provider_entities owned
        WHERE owned.entity_type = 'transfers'
          AND owned.provider_id IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM scoped_foreign_entities other
              WHERE other.entity_type = 'transfers'
                AND other.key_1 = owned.key_1
          )
        """,
        params,
    )
    _timed_delete_statement(
        connection,
        "populate-transfer-simple-keys",
        f"""
        INSERT INTO scoped_transfer_simple_keys(from_stop_id, to_stop_id, transfer_type)
        SELECT owned.key_1, owned.key_2, owned.key_3
        FROM provider_entities owned
        WHERE owned.entity_type = 'transfers'
          AND owned.provider_id IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM scoped_foreign_entities other
              WHERE other.entity_type = 'transfers'
                AND other.key_1 = owned.key_1
                AND other.key_2 = owned.key_2
                AND other.key_3 = owned.key_3
          )
        """,
        params,
    )
    _timed_delete_statement(
        connection,
        "populate-pathway-keys",
        f"""
        INSERT INTO scoped_pathway_keys(pathway_id, from_stop_id, to_stop_id)
        SELECT owned.key_1, owned.key_2, owned.key_3
        FROM provider_entities owned
        WHERE owned.entity_type = 'pathways'
          AND owned.provider_id IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM scoped_foreign_entities other
              WHERE other.entity_type = 'pathways'
                AND other.key_1 = owned.key_1
                AND other.key_2 = owned.key_2
                AND other.key_3 = owned.key_3
          )
        """,
        params,
    )


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

    affected_city_ids = provider_city_ids(connection, selected)
    placeholders = _provider_placeholders(selected)
    params = tuple(selected)
    _prepare_scoped_delete_sets(connection, selected)

    _timed_delete_statement(
        connection,
        "delete-stop-times",
        """
        DELETE FROM stop_times
        WHERE trip_id IN (SELECT trip_id FROM scoped_trip_ids)
        """,
    )
    _timed_delete_statement(
        connection,
        "delete-active-services",
        """
        DELETE FROM active_services
        WHERE service_id IN (SELECT service_id FROM scoped_service_ids)
        """,
    )
    _timed_delete_statement(
        connection,
        "delete-calendar-dates",
        """
        DELETE FROM calendar_dates
        WHERE service_id IN (SELECT service_id FROM scoped_calendar_date_ids)
        """,
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
            _timed_delete_statement(
                connection,
                "delete-transfers",
                """
                DELETE FROM transfers AS target
                WHERE (target.from_stop_id || char(31) || target.to_stop_id || char(31) ||
                       target.from_trip_id || char(31) || target.to_trip_id || char(31) ||
                       target.from_route_id || char(31) || target.to_route_id)
                      IN (SELECT transfer_key FROM scoped_transfer_keys)
                """,
            )
        elif {"from_stop_id", "to_stop_id", "transfer_type"}.issubset(transfer_columns):
            _timed_delete_statement(
                connection,
                "delete-transfers",
                """
                DELETE FROM transfers AS target
                WHERE (target.from_stop_id, target.to_stop_id, CAST(target.transfer_type AS TEXT))
                      IN (SELECT from_stop_id, to_stop_id, transfer_type FROM scoped_transfer_simple_keys)
                """,
            )
        else:
            raise ValueError("Unsupported transfers table schema.")
    if _table_exists(connection, "pathways"):
        _timed_delete_statement(
            connection,
            "delete-pathways",
            """
            DELETE FROM pathways AS target
            WHERE (target.pathway_id, target.from_stop_id, target.to_stop_id)
                  IN (SELECT pathway_id, from_stop_id, to_stop_id FROM scoped_pathway_keys)
            """,
        )
    for table, statement_name, column, temp_table in (
        ("trips", "delete-trips", "trip_id", "scoped_trip_ids"),
        ("routes", "delete-routes", "route_id", "scoped_route_ids"),
        ("calendar", "delete-calendar", "service_id", "scoped_calendar_ids"),
        ("raw_stops", "delete-raw-stops", "stop_id", "scoped_stop_ids"),
    ):
        _timed_delete_statement(
            connection,
            statement_name,
            f"DELETE FROM {table} WHERE {column} IN (SELECT {column} FROM {temp_table})",
        )

    _timed_delete_statement(
        connection,
        "delete-provider-city-stops",
        f"DELETE FROM provider_city_stops WHERE provider_id IN ({placeholders})",
        params,
    )
    _timed_delete_statement(
        connection,
        "delete-provider-city-modes",
        f"DELETE FROM provider_city_modes WHERE provider_id IN ({placeholders})",
        params,
    )
    _timed_delete_statement(
        connection,
        "delete-provider-entities",
        f"DELETE FROM provider_entities WHERE provider_id IN ({placeholders})",
        params,
    )
    rebuild_city_stops(connection, affected_city_ids)
    rebuild_city_departure_modes(connection, affected_city_ids)
