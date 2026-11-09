"""SQLite connection and schema management.

All state lives in one file. ``connect`` opens it with the pragmas the schema
expects and applies the schema if the database is new or behind.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 5


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
    4: """
-- Lessons from the published work of named experts (catalog/expert_lessons.yaml): shown in every worker prompt
-- and used for timely reviews of candidates. Paraphrased principles with references, not quotations.
CREATE TABLE IF NOT EXISTS lessons (
    id          TEXT PRIMARY KEY,
    experts     TEXT NOT NULL,        -- JSON list of names
    title       TEXT NOT NULL,
    lesson      TEXT NOT NULL,
    check_text  TEXT,                 -- what a worker should do with the lesson
    applies_to  TEXT,                 -- JSON list of domain tags
    refs        TEXT,                 -- JSON list
    sources     TEXT,                 -- JSON list of ePrint / arXiv ids
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
""",
    5: """
-- Full-text search over the system's own memory: findings, questions, reading notes and candidates,
-- so a worker can find what any branch of the agenda already established (search_findings).
CREATE VIRTUAL TABLE IF NOT EXISTS findings_fts USING fts5(
    id UNINDEXED, claim, evidence_note,
    content='findings', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS findings_fts_ai AFTER INSERT ON findings BEGIN
    INSERT INTO findings_fts(rowid, id, claim, evidence_note) VALUES (new.rowid, new.id, new.claim, new.evidence_note);
END;
CREATE TRIGGER IF NOT EXISTS findings_fts_ad AFTER DELETE ON findings BEGIN
    INSERT INTO findings_fts(findings_fts, rowid, id, claim, evidence_note) VALUES ('delete', old.rowid, old.id, old.claim, old.evidence_note);
END;
CREATE TRIGGER IF NOT EXISTS findings_fts_au AFTER UPDATE ON findings BEGIN
    INSERT INTO findings_fts(findings_fts, rowid, id, claim, evidence_note) VALUES ('delete', old.rowid, old.id, old.claim, old.evidence_note);
    INSERT INTO findings_fts(rowid, id, claim, evidence_note) VALUES (new.rowid, new.id, new.claim, new.evidence_note);
END;
INSERT INTO findings_fts(findings_fts) VALUES ('rebuild');
CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts USING fts5(
    id UNINDEXED, title, body, summary,
    content='questions', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS questions_fts_ai AFTER INSERT ON questions BEGIN
    INSERT INTO questions_fts(rowid, id, title, body, summary) VALUES (new.rowid, new.id, new.title, new.body, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS questions_fts_ad AFTER DELETE ON questions BEGIN
    INSERT INTO questions_fts(questions_fts, rowid, id, title, body, summary) VALUES ('delete', old.rowid, old.id, old.title, old.body, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS questions_fts_au AFTER UPDATE ON questions BEGIN
    INSERT INTO questions_fts(questions_fts, rowid, id, title, body, summary) VALUES ('delete', old.rowid, old.id, old.title, old.body, old.summary);
    INSERT INTO questions_fts(rowid, id, title, body, summary) VALUES (new.rowid, new.id, new.title, new.body, new.summary);
END;
INSERT INTO questions_fts(questions_fts) VALUES ('rebuild');
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    id UNINDEXED, prior, decision, confirmed, surprises, hardness, structure_exploited, limits, ideas,
    content='reading_notes', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS notes_fts_ai AFTER INSERT ON reading_notes BEGIN
    INSERT INTO notes_fts(rowid, id, prior, decision, confirmed, surprises, hardness, structure_exploited, limits, ideas) VALUES (new.rowid, new.id, new.prior, new.decision, new.confirmed, new.surprises, new.hardness, new.structure_exploited, new.limits, new.ideas);
END;
CREATE TRIGGER IF NOT EXISTS notes_fts_ad AFTER DELETE ON reading_notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, id, prior, decision, confirmed, surprises, hardness, structure_exploited, limits, ideas) VALUES ('delete', old.rowid, old.id, old.prior, old.decision, old.confirmed, old.surprises, old.hardness, old.structure_exploited, old.limits, old.ideas);
END;
CREATE TRIGGER IF NOT EXISTS notes_fts_au AFTER UPDATE ON reading_notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, id, prior, decision, confirmed, surprises, hardness, structure_exploited, limits, ideas) VALUES ('delete', old.rowid, old.id, old.prior, old.decision, old.confirmed, old.surprises, old.hardness, old.structure_exploited, old.limits, old.ideas);
    INSERT INTO notes_fts(rowid, id, prior, decision, confirmed, surprises, hardness, structure_exploited, limits, ideas) VALUES (new.rowid, new.id, new.prior, new.decision, new.confirmed, new.surprises, new.hardness, new.structure_exploited, new.limits, new.ideas);
END;
INSERT INTO notes_fts(notes_fts) VALUES ('rebuild');
CREATE VIRTUAL TABLE IF NOT EXISTS problems_fts USING fts5(
    id UNINDEXED, name, objects, forward_map, distribution, hidden, structure, quantum_notes,
    content='problems', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS problems_fts_ai AFTER INSERT ON problems BEGIN
    INSERT INTO problems_fts(rowid, id, name, objects, forward_map, distribution, hidden, structure, quantum_notes) VALUES (new.rowid, new.id, new.name, new.objects, new.forward_map, new.distribution, new.hidden, new.structure, new.quantum_notes);
END;
CREATE TRIGGER IF NOT EXISTS problems_fts_ad AFTER DELETE ON problems BEGIN
    INSERT INTO problems_fts(problems_fts, rowid, id, name, objects, forward_map, distribution, hidden, structure, quantum_notes) VALUES ('delete', old.rowid, old.id, old.name, old.objects, old.forward_map, old.distribution, old.hidden, old.structure, old.quantum_notes);
END;
CREATE TRIGGER IF NOT EXISTS problems_fts_au AFTER UPDATE ON problems BEGIN
    INSERT INTO problems_fts(problems_fts, rowid, id, name, objects, forward_map, distribution, hidden, structure, quantum_notes) VALUES ('delete', old.rowid, old.id, old.name, old.objects, old.forward_map, old.distribution, old.hidden, old.structure, old.quantum_notes);
    INSERT INTO problems_fts(rowid, id, name, objects, forward_map, distribution, hidden, structure, quantum_notes) VALUES (new.rowid, new.id, new.name, new.objects, new.forward_map, new.distribution, new.hidden, new.structure, new.quantum_notes);
END;
INSERT INTO problems_fts(problems_fts) VALUES ('rebuild');
""",
}


def _read_schema() -> str:
    return resources.files("mathresearch").joinpath("schema.sql").read_text(encoding="utf-8")


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ["sources", "units", "problems", "attacks", "questions", "experiments", "findings", "runs",
              "digests", "reading_notes", "weakness_patterns", "lessons"]
    return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables}
