"""The funnel stays fed: the loop asks for candidates when too few are in play, and candidates go first."""

import random
import sqlite3

import yaml

from mathresearch import agenda, catalog, db, novelty, store, writeback
from mathresearch.charter import DEFAULT_CHARTER, Charter

QUIVER = {
    "name": "Arrow-mixed Kronecker representation transporter",
    "objects": "A two-vertex quiver with r parallel arrows over F_q, an ordered tuple of m-by-n matrices; "
               "equivalence additionally permits mixing the arrows.",
    "forward_map": "Given P in GL_m, Q in GL_n and R in GL_r, compute B_i = sum_j R_ij P A_j Q^{-1}.",
    "distribution": "uniform entries, uniform invertible P, Q, R", "hidden": "any triple (P, Q, R)",
    "verification": "check invertibility and the equations", "domains": "", "known_relatives": "",
    "notes": "", "sparked_by": "prior", "sparked_by_id": "",
}


def _charter(**over):
    c = Charter.from_dict(yaml.safe_load(DEFAULT_CHARTER))
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _wb(**extra):
    data = {"summary": "s", "status": "closed", "findings": [], "new_questions": [], "candidate_problems": []}
    data.update(extra)
    return writeback.validate(data)


def test_the_loop_asks_for_candidates_when_too_few_are_in_play(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    charter = _charter(candidate_target=2, propose_every_runs=3)
    store.insert(conn, "questions", kind="seed", lane="harvest", title="Seed read: a textbook", priority=0.9)
    spark = store.insert(conn, "questions", kind="explore", lane="open", origin="reference",
                         title="Does the hull of a quotient code hide the shear?")
    store.insert(conn, "problems", name="Old broken idea", status="broken")
    qid = agenda.ensure_proposal(conn, charter)
    q = store.get(conn, "questions", qid)
    assert q["kind"] == "propose" and q["lane"] == "harvest" and spark in q["body"] and "Old broken idea" in q["body"]
    assert agenda.ensure_proposal(conn, charter) is None                       # one at a time
    charter.lanes = {"harvest": 1.0, "deepen": 0.0, "open": 0.0}
    assert agenda.pick(conn, charter, random.Random(0))["id"] == qid            # ahead of the seed read

    # it closes only with a candidate (or a negative finding saying why there is none)
    run = store.insert(conn, "runs", question_id=qid, status="done")
    created = writeback.apply(conn, run, q, _wb())
    assert any("propose question kept open" in w for w in created["warnings"])
    assert store.get(conn, "questions", qid)["status"] == "open"
    created = writeback.apply(conn, run, store.get(conn, "questions", qid), _wb(candidate_problems=[QUIVER]))
    assert store.get(conn, "questions", qid)["status"] == "closed" and len(created["problems"]) == 1

    # not again until propose_every_runs runs have started, and never once the target is met
    assert agenda.ensure_proposal(conn, charter) is None
    for _ in range(3):
        store.insert(conn, "runs", question_id=qid, status="done", started_at="2999-01-01T00:00:00.000Z")
    second = agenda.ensure_proposal(conn, charter)
    assert second is not None and "Arrow-mixed" in store.get(conn, "questions", second)["body"]
    store.close_question(conn, second)
    store.insert(conn, "problems", name="Another", status="well_defined")
    for _ in range(3):
        store.insert(conn, "runs", question_id=qid, status="done", started_at="2999-01-02T00:00:00.000Z")
    assert agenda.ensure_proposal(conn, charter) is None                       # two in play: target met
    assert agenda.ensure_proposal(conn, _charter(candidate_target=0)) is None  # switched off


def test_deepen_works_on_candidates_before_known_problems(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    charter = _charter(candidate_target=0)
    charter.lanes = {"harvest": 0.0, "deepen": 1.0, "open": 0.0}
    pid = store.insert(conn, "problems", name="A candidate", status="idea")
    for i in range(8):
        store.insert(conn, "questions", kind="attack", lane="deepen", title=f"Attack SIDH variant {i}", priority=0.9)
    define = store.insert(conn, "questions", kind="investigate", lane="deepen", title="Make it well defined",
                          problem_id=pid, priority=0.6)
    check = novelty.open_check(conn, pid)
    picks = [agenda.pick(conn, charter, random.Random(seed))["id"] for seed in range(60)]
    assert set(picks) == {check, define}                       # never a known-problem question while these wait
    assert 30 <= picks.count(define) <= 50                     # the definition is not starved by the check
    store.close_question(conn, check)
    assert {agenda.pick(conn, charter, random.Random(seed))["id"] for seed in range(20)} == {define}
    store.close_question(conn, define)
    assert agenda.pick(conn, charter, random.Random(0))["title"].startswith("Attack SIDH")


def test_the_lab_gets_most_of_the_runs_while_candidates_wait(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    charter = _charter(candidate_target=0)                     # default lanes: deepen 0.25
    store.insert(conn, "questions", kind="learn", lane="harvest", title="Read about codes")
    store.insert(conn, "questions", kind="explore", lane="open", title="Wander")
    known = store.insert(conn, "questions", kind="attack", lane="deepen", title="Attack SIDH")

    def deepen_share():
        lanes = [agenda.pick(conn, charter, random.Random(seed))["lane"] for seed in range(400)]
        return lanes.count("deepen") / 400

    assert 0.18 < deepen_share() < 0.32
    store.close_question(conn, known)
    for i in range(3):
        pid = store.insert(conn, "problems", name=f"Candidate {i}", status="idea")
        store.insert(conn, "questions", kind="investigate", lane="deepen", title=f"Define {i}", problem_id=pid)
    assert 0.63 < deepen_share() < 0.77


def test_copies_of_the_known_result_check_are_cleared_at_the_start_of_a_run(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    pid = store.insert(conn, "problems", name="A candidate", status="idea")
    own = novelty.open_check(conn, pid)
    copy = store.insert(conn, "questions", kind="novelty_check", lane="harvest",
                        title="Classify the candidate against known subspace problems")
    assert novelty.drop_copies(conn) == [copy] and novelty.drop_copies(conn) == []
    assert store.get(conn, "questions", copy)["status"] == "abandoned"
    assert store.get(conn, "questions", own)["status"] == "open"


def test_closest_known_sees_through_another_areas_vocabulary(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    catalog.load(conn)
    pid = store.insert(conn, "problems", status="idea",
                       **{k: QUIVER[k] for k in ("name", "objects", "forward_map", "distribution", "hidden",
                                                 "verification")})
    # as stored before the catalog listed each problem's disguises
    store.update(conn, "problems", pid, novelty={"closest": [{"id": "prob_known_code_equivalence", "name": "x",
                                                              "score": 1.0}], "checks": [], "verdict": "unchecked"})
    q = store.insert(conn, "questions", kind="novelty_check", lane="deepen", title="check", body="old",
                     problem_id=pid)
    assert novelty.refresh_unchecked(conn) == [pid]
    p = store.get(conn, "problems", pid)
    assert p["novelty"]["closest"][0]["id"] == "prob_known_tensor_isomorphism"
    body = store.get(conn, "questions", q)["body"]
    assert "prob_known_tensor_isomorphism" in body and "whole catalog" in body
    assert novelty.refresh_unchecked(conn) == []
    # a relative the proposer named is always listed
    named = store.insert(conn, "problems", name="Odd walk", status="idea", objects="walks on a curve",
                         reductions=[{"note": "maybe related to MinRank"}])
    assert "prob_known_minrank" in [c["id"] for c in novelty.closest_known(conn, store.get(conn, "problems", named))]


def test_a_database_from_before_gains_the_propose_kind(tmp_path):
    path = tmp_path / "r.db"
    conn = db.connect(path)
    old = store.insert(conn, "questions", kind="seed", lane="harvest", title="Seed read: kept across the rebuild")
    conn.execute("DELETE FROM schema_version WHERE version >= 10")
    conn.executescript("""
        PRAGMA foreign_keys = OFF;
        DROP TRIGGER questions_fts_ai; DROP TRIGGER questions_fts_ad; DROP TRIGGER questions_fts_au;
        CREATE TABLE q_old AS SELECT * FROM questions;
        DROP TABLE questions;
        CREATE TABLE questions (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('learn', 'seed')),
            lane TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', title TEXT NOT NULL, body TEXT, summary TEXT,
            parent_id TEXT, problem_id TEXT, domains TEXT, priority REAL NOT NULL DEFAULT 0.5,
            runs INTEGER NOT NULL DEFAULT 0, created_by_run TEXT, created_at TEXT, updated_at TEXT, closed_at TEXT,
            origin TEXT, origin_id TEXT);
        INSERT INTO questions SELECT * FROM q_old;
        DROP TABLE q_old;
    """)
    conn.close()
    conn = db.connect(path)
    assert store.get(conn, "questions", old)["title"].endswith("kept across the rebuild")
    new = store.insert(conn, "questions", kind="propose", lane="harvest", title="Propose new candidate problems")
    hits = conn.execute("SELECT id FROM questions_fts WHERE questions_fts MATCH 'rebuild OR propose'").fetchall()
    assert {r["id"] for r in hits} == {old, new}
    try:
        store.insert(conn, "questions", kind="nonsense", lane="harvest", title="x")
        raise AssertionError("kind check lost")
    except sqlite3.IntegrityError:
        pass


def test_the_operator_drops_candidates_by_hand(tmp_path, capsys):
    from mathresearch.cli import main
    main(["--dir", str(tmp_path), "init"])
    conn = db.connect(tmp_path / "research.db")
    pid = store.insert(conn, "problems", name="Planted sparse filling", status="idea")
    q = store.insert(conn, "questions", kind="investigate", lane="deepen", title="Define it", problem_id=pid)
    assert main(["--dir", str(tmp_path), "candidates", "drop", pid, "--why", "plain syndrome decoding"]) == 0
    assert "dropped 'Planted sparse filling' (1 questions closed)" in capsys.readouterr().out
    assert store.get(conn, "problems", pid)["status"] == "abandoned"
    assert store.get(conn, "questions", q)["status"] == "abandoned"
    assert "plain syndrome decoding" in store.get(conn, "questions", q)["summary"]
    assert main(["--dir", str(tmp_path), "candidates", "drop", "prob_nope"]) == 1
