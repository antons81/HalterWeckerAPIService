import sqlite3
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import static_departures_scoped as scoped
from build_german_departure_index import connect


def transfer_key(*components: str) -> str:
    return scoped.TRANSFER_KEY_SEPARATOR.join(components)


def make_validation_connection() -> sqlite3.Connection:
    connection = connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE city_aliases (
            alias_city_id TEXT PRIMARY KEY,
            canonical_city_id TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE city_departure_modes (
            city_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            timezone TEXT NOT NULL,
            stop_id_prefix TEXT NOT NULL DEFAULT '',
            identifier_prefix TEXT NOT NULL DEFAULT ''
        ) WITHOUT ROWID;
        CREATE TABLE transfers (
            from_stop_id TEXT NOT NULL,
            to_stop_id TEXT NOT NULL,
            from_trip_id TEXT NOT NULL DEFAULT '',
            to_trip_id TEXT NOT NULL DEFAULT '',
            from_route_id TEXT NOT NULL DEFAULT '',
            to_route_id TEXT NOT NULL DEFAULT '',
            transfer_type INTEGER NOT NULL,
            min_transfer_time INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (
                from_stop_id, to_stop_id,
                from_trip_id, to_trip_id,
                from_route_id, to_route_id
            )
        ) WITHOUT ROWID;
        CREATE INDEX transfers_by_stop ON transfers(from_stop_id, to_stop_id);
        """
    )
    connection.execute(
        "INSERT INTO city_stops(city_id, stop_id) VALUES ('fixture-city', 'stop')"
    )
    connection.execute(
        "INSERT INTO active_services(service_id, service_date) VALUES ('service', '20260823')"
    )
    connection.commit()
    return connection


def insert_owned_transfer(
    connection: sqlite3.Connection,
    provider_id: str,
    ownership_key: str,
) -> None:
    connection.execute(
        """
        INSERT INTO provider_entities(entity_type, provider_id, key_1)
        VALUES ('transfers', ?, ?)
        """,
        (provider_id, ownership_key),
    )


def insert_transfer(
    connection: sqlite3.Connection,
    components: tuple[str, ...],
) -> None:
    connection.execute(
        """
        INSERT INTO transfers(
            from_stop_id, to_stop_id, from_trip_id, to_trip_id,
            from_route_id, to_route_id, transfer_type, min_transfer_time
        ) VALUES (?, ?, ?, ?, ?, ?, 2, 60)
        """,
        components,
    )


class ScopedTransferValidationTests(unittest.TestCase):
    def tearDown(self) -> None:
        if getattr(self, "connection", None) is not None:
            self.connection.close()

    def setUp(self) -> None:
        self.connection = make_validation_connection()

    def test_valid_owned_transfer_exists(self) -> None:
        components = ("from-stop", "to-stop", "trip-a", "trip-b", "route-a", "route-b")
        insert_transfer(self.connection, components)
        insert_owned_transfer(self.connection, "provider-a", transfer_key(*components))

        scoped.validate_scoped_database(self.connection, ["provider-a"])

    def test_orphan_transfer_is_detected(self) -> None:
        insert_owned_transfer(
            self.connection,
            "provider-a",
            transfer_key("from-stop", "to-stop", "trip-a", "trip-b", "route-a", "route-b"),
        )

        with self.assertRaisesRegex(ValueError, "orphaned transfers rows"):
            scoped.validate_scoped_database(self.connection, ["provider-a"])

    def test_malformed_transfer_ownership_key_fails_closed(self) -> None:
        insert_owned_transfer(self.connection, "provider-a", transfer_key("from-stop", "to-stop"))

        with self.assertRaisesRegex(ValueError, "malformed transfer key"):
            scoped.validate_scoped_database(self.connection, ["provider-a"])

    def test_empty_transfer_components_round_trip(self) -> None:
        components = ("from-stop", "to-stop", "", "", "route-a", "")
        insert_transfer(self.connection, components)
        insert_owned_transfer(self.connection, "provider-a", transfer_key(*components))

        scoped.validate_scoped_database(self.connection, ["provider-a"])
        self.assertEqual(
            self.connection.execute(
                "SELECT from_stop_id, to_stop_id, from_trip_id, to_trip_id, "
                "from_route_id, to_route_id FROM transfers"
            ).fetchone(),
            components,
        )

    def test_provider_filter_does_not_validate_unselected_provider(self) -> None:
        selected_components = ("from-a", "to-a", "", "", "route-a", "")
        other_components = ("from-b", "to-b", "", "", "route-b", "")
        insert_transfer(self.connection, selected_components)
        insert_transfer(self.connection, other_components)
        insert_owned_transfer(self.connection, "provider-a", transfer_key(*selected_components))
        insert_owned_transfer(self.connection, "provider-b", transfer_key(*other_components))
        insert_owned_transfer(
            self.connection,
            "provider-b",
            transfer_key("missing", "to-b", "", "", "route-b", ""),
        )

        scoped.validate_scoped_database(self.connection, ["provider-a"])
        with self.assertRaisesRegex(ValueError, "orphaned transfers rows"):
            scoped.validate_scoped_database(self.connection, ["provider-a", "provider-b"])

    def test_large_transfer_table_uses_bounded_scoped_lookups(self) -> None:
        transfer_rows = [
            (f"from-{index}", f"to-{index}", "", "", f"route-{index}", "", 0, 0)
            for index in range(25_000)
        ]
        self.connection.executemany(
            """
            INSERT INTO transfers(
                from_stop_id, to_stop_id, from_trip_id, to_trip_id,
                from_route_id, to_route_id, transfer_type, min_transfer_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            transfer_rows,
        )
        for index in (0, 24_999):
            insert_owned_transfer(
                self.connection,
                "provider-a",
                transfer_key(f"from-{index}", f"to-{index}", "", "", f"route-{index}", ""),
            )
        self.connection.commit()

        started = time.monotonic()
        scoped.validate_scoped_database(self.connection, ["provider-a"])
        self.assertLess(time.monotonic() - started, 5.0)

        primary_key_index = scoped._transfer_primary_key_index(self.connection)
        self.assertIsNotNone(primary_key_index)
        new_plan = self.connection.execute(
            "EXPLAIN QUERY PLAN " + scoped.transfer_existence_query(primary_key_index),
            ("", "", "", "", "", ""),
        ).fetchall()
        new_details = " ".join(str(row[3]) for row in new_plan)
        self.assertIn("PRIMARY KEY", new_details)
        self.assertNotIn("SCAN transfers", new_details)
        self.assertNotIn("CORRELATED", new_details)

        old_plan = self.connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT COUNT(*)
            FROM provider_entities owned
            WHERE owned.entity_type = 'transfers'
              AND owned.provider_id IN (?)
              AND NOT EXISTS (
                  SELECT 1
                  FROM transfers actual
                  WHERE owned.key_1 =
                      actual.from_stop_id || char(31) ||
                      actual.to_stop_id || char(31) ||
                      actual.from_trip_id || char(31) ||
                      actual.to_trip_id || char(31) ||
                      actual.from_route_id || char(31) ||
                      actual.to_route_id
              )
            """,
            ("provider-a",),
        ).fetchall()
        old_details = " ".join(str(row[3]) for row in old_plan)
        self.assertIn("CORRELATED SCALAR SUBQUERY", old_details)
        self.assertIn("SCAN actual", old_details)

    def test_existing_orphan_checks_remain_strict(self) -> None:
        insert_owned_transfer(self.connection, "provider-a", transfer_key("a", "b", "", "", "r", ""))
        self.connection.execute(
            """
            INSERT INTO provider_entities(entity_type, provider_id, key_1)
            VALUES ('raw_stops', 'provider-a', 'missing-stop')
            """
        )

        with self.assertRaisesRegex(ValueError, "orphaned raw_stops rows"):
            scoped.validate_scoped_database(self.connection, ["provider-a"])


if __name__ == "__main__":
    unittest.main()
