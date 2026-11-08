-- MathResearch persistent state. One SQLite file holds everything the
-- research loop reads from and writes back to. Workers keep no state of
-- their own: each run is assembled from these tables and ends by writing
-- back into them.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Immutable literature: papers, books, lecture notes. Content lives on disk
-- (path); the row holds metadata and provenance.
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('paper', 'book', 'notes', 'web', 'other')),
    title       TEXT NOT NULL,
    authors     TEXT,                 -- JSON list
    year        INTEGER,
    venue       TEXT,
    identifiers TEXT,                 -- JSON: {"arxiv": ..., "eprint": ..., "doi": ...}
    url         TEXT,
    path        TEXT,                 -- local file (PDF, LaTeX source, text)
    format      TEXT,                 -- 'latex', 'pdf', 'text', 'html'
    domains     TEXT,                 -- JSON list of domain tags
    ingested_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Knowledge units (K2): one row per definition, theorem, lemma, algorithm,
-- example or remark, with the exact statement and an anchor into its source.
CREATE TABLE IF NOT EXISTS units (
    id          TEXT PRIMARY KEY,
    source_id   TEXT REFERENCES sources(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('definition', 'theorem', 'lemma', 'proposition',
                                             'corollary', 'algorithm', 'example', 'remark', 'conjecture')),
    label       TEXT,                 -- author's label, e.g. "Thm 3.4" or LaTeX \label
    title       TEXT,
    statement   TEXT NOT NULL,        -- exact statement, LaTeX allowed
    hypotheses  TEXT,                 -- JSON list of hypotheses / assumptions
    notation    TEXT,                 -- JSON: symbol -> meaning
    proof_sketch TEXT,
    depends_on  TEXT,                 -- JSON list of unit ids or labels
    anchor      TEXT,                 -- JSON: {"page": 123} or {"file": ..., "line": ...}
    domains     TEXT,                 -- JSON list
    computational_notes TEXT,         -- what can be computed, what is lost, inverse problems
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Computational problems: known hard problems (the catalog) and candidate
-- problems the system proposes live in one table, distinguished by status.
CREATE TABLE IF NOT EXISTS problems (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('known', 'idea', 'well_defined', 'not_trivially_broken',
                                                    'not_known', 'useful_structure', 'human_review',
                                                    'broken', 'duplicate', 'abandoned')),
    domains         TEXT,             -- JSON list
    objects         TEXT,             -- mathematical objects involved
    forward_map     TEXT,             -- description of the easy direction
    forward_cost    TEXT,             -- asymptotic or measured cost of the forward map
    distribution    TEXT,             -- instance distribution and how to sample it
    hidden          TEXT,             -- what the solver must recover
    verification    TEXT,             -- how a claimed solution is checked
    structure       TEXT,             -- JSON: {"trapdoor": ..., "group_action": ..., "homomorphic": ...}
    quantum_notes   TEXT,             -- hidden abelian structure? known quantum speedups?
    reductions      TEXT,             -- JSON list: {"to"|"from": problem id, "note": ...}
    best_attacks    TEXT,             -- JSON list: {"attack_id": ..., "complexity": ..., "ref": ...}
    generator_path  TEXT,             -- path to the instance generator module, if implemented
    refs            TEXT,             -- JSON list of source ids
    parent_id       TEXT REFERENCES problems(id) ON DELETE SET NULL,  -- derived from
    created_by_run  TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Attack techniques: what structure each exploits and whether we can run it.
CREATE TABLE IF NOT EXISTS attacks (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    family          TEXT,             -- 'algebraic', 'lattice', 'combinatorial', 'quantum', 'generic', ...
    exploits        TEXT,             -- the structure the attack needs
    applies_to      TEXT,             -- JSON list of problem ids or problem-shape tags
    complexity      TEXT,             -- known asymptotic complexity, as text
    implementation  TEXT,             -- module path implementing run(problem, size) -> cost, or NULL
    requires        TEXT,             -- JSON list of tools: ['sage', 'msolve', 'magma']
    refs            TEXT,             -- JSON list of source ids
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- The agenda: every open or closed research question. Learning is a kind of
-- question ('learn'), so hitting a knowledge gap opens a child question and
-- blocks the parent on it.
CREATE TABLE IF NOT EXISTS questions (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('learn', 'explore', 'investigate', 'attack', 'novelty_check', 'survey')),
    lane        TEXT NOT NULL CHECK (lane IN ('harvest', 'deepen', 'open')),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'blocked', 'running', 'closed', 'abandoned')),
    title       TEXT NOT NULL,
    body        TEXT,                 -- the question in full
    summary     TEXT,                 -- rewritten after every run: current state of knowledge on it
    parent_id   TEXT REFERENCES questions(id) ON DELETE SET NULL,
    problem_id  TEXT REFERENCES problems(id) ON DELETE SET NULL,
    domains     TEXT,                 -- JSON list
    priority    REAL NOT NULL DEFAULT 0.5,
    runs        INTEGER NOT NULL DEFAULT 0,
    created_by_run TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    closed_at   TEXT
);

-- Experiments: every piece of code the system executed, with enough to
-- reproduce it.
CREATE TABLE IF NOT EXISTS experiments (
    id           TEXT PRIMARY KEY,
    question_id  TEXT REFERENCES questions(id) ON DELETE SET NULL,
    problem_id   TEXT REFERENCES problems(id) ON DELETE SET NULL,
    attack_id    TEXT REFERENCES attacks(id) ON DELETE SET NULL,
    run_id       TEXT,
    kind         TEXT NOT NULL DEFAULT 'script' CHECK (kind IN ('script', 'attack_sweep', 'check')),
    engine       TEXT NOT NULL,       -- 'sage', 'python', 'msolve', 'magma'
    code         TEXT NOT NULL,
    code_sha256  TEXT NOT NULL,
    params       TEXT,                -- JSON
    seed         INTEGER,
    stdout       TEXT,
    stderr       TEXT,
    exit_code    INTEGER,
    wall_seconds REAL,
    timed_out    INTEGER NOT NULL DEFAULT 0,
    result       TEXT,                -- JSON parsed from stdout when the script prints a JSON line
    tool_versions TEXT,               -- JSON: {"sage": "10.4", "python": "3.12.1"}
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Findings: append-only claims, each tied to its evidence.
CREATE TABLE IF NOT EXISTS findings (
    id            TEXT PRIMARY KEY,
    question_id   TEXT REFERENCES questions(id) ON DELETE SET NULL,
    problem_id    TEXT REFERENCES problems(id) ON DELETE SET NULL,
    run_id        TEXT,
    kind          TEXT NOT NULL CHECK (kind IN ('fact', 'derivation', 'experimental', 'negative', 'gap', 'anomaly', 'novelty')),
    claim         TEXT NOT NULL,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('unit', 'source', 'experiment', 'reasoning')),
    evidence_id   TEXT,               -- unit id, source id (+anchor in evidence_note) or experiment id
    evidence_note TEXT,
    confidence    REAL,               -- 0..1
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Runs: every worker invocation, for cost accounting and replay.
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    question_id   TEXT REFERENCES questions(id) ON DELETE SET NULL,
    lane          TEXT,
    provider      TEXT,
    model         TEXT,
    status        TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed', 'cancelled')),
    context_ids   TEXT,               -- JSON: unit/problem/experiment ids placed in context
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL,
    result        TEXT,               -- JSON write-back as returned by the model
    error         TEXT,
    started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_units_source   ON units(source_id);
CREATE INDEX IF NOT EXISTS idx_units_kind     ON units(kind);
CREATE INDEX IF NOT EXISTS idx_problems_status ON problems(status);
CREATE INDEX IF NOT EXISTS idx_questions_pick ON questions(status, lane, priority);
CREATE INDEX IF NOT EXISTS idx_findings_q     ON findings(question_id);
CREATE INDEX IF NOT EXISTS idx_experiments_q  ON experiments(question_id);
CREATE INDEX IF NOT EXISTS idx_runs_q         ON runs(question_id);

-- Full-text search over units and sources (BM25 via FTS5).
CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
    id UNINDEXED, title, statement, hypotheses, computational_notes,
    content='units', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS units_ai AFTER INSERT ON units BEGIN
    INSERT INTO units_fts(rowid, id, title, statement, hypotheses, computational_notes)
    VALUES (new.rowid, new.id, new.title, new.statement, new.hypotheses, new.computational_notes);
END;
CREATE TRIGGER IF NOT EXISTS units_ad AFTER DELETE ON units BEGIN
    INSERT INTO units_fts(units_fts, rowid, id, title, statement, hypotheses, computational_notes)
    VALUES ('delete', old.rowid, old.id, old.title, old.statement, old.hypotheses, old.computational_notes);
END;
CREATE TRIGGER IF NOT EXISTS units_au AFTER UPDATE ON units BEGIN
    INSERT INTO units_fts(units_fts, rowid, id, title, statement, hypotheses, computational_notes)
    VALUES ('delete', old.rowid, old.id, old.title, old.statement, old.hypotheses, old.computational_notes);
    INSERT INTO units_fts(rowid, id, title, statement, hypotheses, computational_notes)
    VALUES (new.rowid, new.id, new.title, new.statement, new.hypotheses, new.computational_notes);
END;
