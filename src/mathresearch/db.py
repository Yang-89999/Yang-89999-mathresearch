"""SQLite connection and schema management.

All state lives in one file. ``connect`` opens it with the pragmas the schema
expects and applies the schema if the database is new or behind.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (and if needed create) the research database at ``path``."""
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; use explicit BEGIN when needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    migrate(conn)
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


def migrate(conn: sqlite3.Connection) -> None:
    """Bring the database to ``SCHEMA_VERSION``.

    Version 1 is the full schema in ``schema.sql``. Later versions append
    ``ALTER``/``CREATE`` statements to ``_MIGRATIONS`` keyed by target version.
    """
    version = current_version(conn)
    if version == 0:
        conn.executescript(_read_schema())
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (1,))
        version = 1
    for target in range(version + 1, SCHEMA_VERSION + 1):
        conn.executescript(_MIGRATIONS[target])
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (target,))


_MIGRATIONS: dict[int, str] = {}


def _read_schema() -> str:
    return resources.files("mathresearch").joinpath("schema.sql").read_text(encoding="utf-8")


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ["sources", "units", "problems", "attacks", "questions", "experiments", "findings", "runs"]
    return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables}
