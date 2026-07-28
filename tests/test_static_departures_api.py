import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from static_departures_api import Database


def write_database(path: Path, version: str) -> None:
    db = sqlite3.connect(path)
    db.execute("create table metadata (key text primary key, value text not null)")
    db.execute("insert into metadata values ('databaseVersion', ?)", (version,))
    db.commit(); db.close()


class StaticDeparturesDatabaseTests(unittest.TestCase):
    def test_reopens_when_atomic_symlink_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second, current = root / "first.sqlite", root / "second.sqlite", root / "current.sqlite"
            write_database(first, "first"); write_database(second, "second")
            current.symlink_to(first.name)
            database = Database(str(current), ttl=0)
            self.assertEqual(database.meta()["databaseVersion"], "first")
            next_link = root / "next.sqlite"; next_link.symlink_to(second.name); os.replace(next_link, current)
            self.assertEqual(database.meta()["databaseVersion"], "second")

    def test_invalid_database_has_no_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "invalid.sqlite"; sqlite3.connect(path).close()
            with self.assertRaises(sqlite3.OperationalError):
                Database(str(path), ttl=0).meta()


if __name__ == "__main__":
    unittest.main()
