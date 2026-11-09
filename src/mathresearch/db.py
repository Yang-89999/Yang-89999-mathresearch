"""SQLite connection and schema management.

All state lives in one file. ``connect`` opens it with the pragmas the schema
expects and applies the schema if the database is new or behind.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 3


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (and if needed create) the research database at ``path``."""
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; use explicit BEGIN when needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")  # parallel workers share the file
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
    if version >= SCHEMA_VERSION:
        return
    # Some migrations rebuild a table; foreign keys must be off while the old table is dropped,
    # or ON DELETE SET NULL would wipe the references to it.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for target in range(version + 1, SCHEMA_VERSION + 1):
            try:
                conn.executescript("BEGIN;\n" + _MIGRATIONS[target]
                                   + f"\nINSERT INTO schema_version(version) VALUES ({target});\nCOMMIT;")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise RuntimeError(f"foreign key check failed after migration: {[tuple(r) for r in bad[:5]]}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


_MIGRATIONS: dict[int, str] = {
    2: """
-- World digests written by the reflection step (every charter.reflect_every_runs runs).
CREATE TABLE IF NOT EXISTS digests (
    id         TEXT PRIMARY KEY,
    run_count  INTEGER NOT NULL,
    trigger    TEXT,                  -- 'schedule' | 'manual'
    content    TEXT NOT NULL,         -- markdown digest as workers see it
    stats      TEXT,                  -- JSON statistics the digest was rendered from
    actions    TEXT,                  -- JSON list of agenda changes made
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
""",
    3: """
-- Purpose-driven reading (PR 8).
-- Sources: the abstract, a paper card (L0: what a PQC designer needs to know about the paper), who provided it.
ALTER TABLE sources ADD COLUMN abstract TEXT;
ALTER TABLE sources ADD COLUMN card TEXT;             -- JSON paper card, written by a worker
ALTER TABLE sources ADD COLUMN card_run TEXT;
ALTER TABLE sources ADD COLUMN provided_by TEXT;      -- 'operator' | 'agent' | 'catalog'

-- Questions gain the 'seed' kind (a first read of an operator reference) and an origin: what sparked them.
CREATE TABLE questions_v3 (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('learn', 'explore', 'investigate', 'attack', 'novelty_check', 'survey',
                                             'seed')),
    lane        TEXT NOT NULL CHECK (lane IN ('harvest', 'deepen', 'open')),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'blocked', 'running', 'closed', 'abandoned')),
    title       TEXT NOT NULL,
    body        TEXT,
    summary     TEXT,
    parent_id   TEXT REFERENCES questions(id) ON DELETE SET NULL,
    problem_id  TEXT REFERENCES problems(id) ON DELETE SET NULL,
    domains     TEXT,
    priority    REAL NOT NULL DEFAULT 0.5,
    runs        INTEGER NOT NULL DEFAULT 0,
    created_by_run TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    closed_at   TEXT,
    origin      TEXT,                 -- 'charter' | 'operator' | 'reference' | 'experiment' | 'prior' | 'system' | 'reflection'
    origin_id   TEXT                  -- the source or experiment that sparked it
);
INSERT INTO questions_v3 (id, kind, lane, status, title, body, summary, parent_id, problem_id, domains, priority,
                          runs, created_by_run, created_at, updated_at, closed_at, origin)
    SELECT id, kind, lane, status, title, body, summary, parent_id, problem_id, domains, priority, runs,
           created_by_run, created_at, updated_at, closed_at,
           CASE WHEN created_by_run IS NULL THEN 'charter' ELSE 'prior' END FROM questions;
DROP TABLE questions;
ALTER TABLE questions_v3 RENAME TO questions;
CREATE INDEX IF NOT EXISTS idx_questions_pick ON questions(status, lane, priority);
CREATE INDEX IF NOT EXISTS idx_questions_origin ON questions(origin, origin_id);

-- What a worker read, why, and what it changed. One row per source read in a run.
CREATE TABLE IF NOT EXISTS reading_notes (
    id                  TEXT PRIMARY KEY,
    question_id         TEXT REFERENCES questions(id) ON DELETE SET NULL,
    run_id              TEXT,
    source_id           TEXT REFERENCES sources(id) ON DELETE SET NULL,
    prior               TEXT,         -- what the worker expected before reading
    decision            TEXT,         -- the decision the reading was meant to change
    confirmed           TEXT,
    surprises           TEXT,         -- where the paper differed from the prior
    hardness            TEXT,         -- hardness assumption at stake
    structure_exploited TEXT,         -- structure an attack in the paper exploits, and why it works
    limits              TEXT,         -- limits and open problems the authors admit
    ideas               TEXT,         -- JSON list of ideas; each should become a question or candidate
    patterns            TEXT,         -- JSON list of weakness-pattern ids the paper illustrates
    unit_ids            TEXT,         -- JSON list of units read
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_notes_source ON reading_notes(source_id);
CREATE INDEX IF NOT EXISTS idx_notes_q ON reading_notes(question_id);

-- Why schemes broke: structural weaknesses every candidate is audited against.
CREATE TABLE IF NOT EXISTS weakness_patterns (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    applies_to  TEXT,                 -- JSON list of domain tags ('multivariate', 'codes', ..., 'generic')
    description TEXT NOT NULL,        -- the structure and why it helps an attacker
    signature   TEXT,                 -- how it shows up in the public data
    test        TEXT,                 -- a computational test for it
    exploited_by TEXT,                -- JSON list of attack ids
    examples    TEXT,                 -- JSON list of schemes broken this way
    refs        TEXT,                 -- JSON list
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
-- Per-candidate audit: {pattern_id: {"status": avoided|vulnerable|untested|not_applicable, "evidence_id", "note"}}
ALTER TABLE problems ADD COLUMN weakness_audit TEXT;
""",
}


def _read_schema() -> str:
    return resources.files("mathresearch").joinpath("schema.sql").read_text(encoding="utf-8")


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ["sources", "units", "problems", "attacks", "questions", "experiments", "findings", "runs",
              "digests", "reading_notes", "weakness_patterns"]
    return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables}
