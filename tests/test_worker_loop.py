"""End-to-end offline test of the research loop with a scripted provider."""

import yaml

from mathresearch import agenda, db, store
from mathresearch.charter import DEFAULT_CHARTER, Charter
from mathresearch.loop import run_cycles
from mathresearch.providers.base import ChatResponse, FakeProvider, TextBlock
from mathresearch.worker import run_question
from mathresearch.writeback import WRITEBACK_SCHEMA, WritebackError, validate


def _charter():
    return Charter.from_dict(yaml.safe_load(DEFAULT_CHARTER))


def _writeback(**over):
    base = {"summary": "Elimination theory offers the projection of a variety as an efficient forward map.",
            "status": "open", "findings": [], "new_questions": [], "candidate_problems": []}
    base.update(over)
    return base


def test_seed_creates_one_survey_per_domain_plus_open(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    ids = agenda.seed(conn, _charter())
    assert len(ids) == 8
    lanes = [q["lane"] for q in store.select(conn, "questions")]
    assert lanes.count("harvest") == 7 and lanes.count("open") == 1
    assert agenda.seed(conn, _charter()) == []  # idempotent


def test_pick_respects_lane_shares_and_blocking(tmp_path):
    import random
    conn = db.connect(tmp_path / "r.db")
    charter = _charter()
    agenda.seed(conn, charter)
    picks = [agenda.pick(conn, charter, random.Random(i))["lane"] for i in range(200)]
    assert picks.count("harvest") > picks.count("open") > 0
    # block everything in harvest and open: nothing left to pick
    for q in store.select(conn, "questions"):
        store.update(conn, "questions", q["id"], status="blocked")
    assert agenda.pick(conn, charter, random.Random(0)) is None


def test_single_run_with_tools_gap_and_candidate(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    charter = _charter()
    sid = store.insert(conn, "sources", kind="book", title="Ideals, Varieties, and Algorithms", year=2015)
    uid = store.insert(conn, "units", source_id=sid, kind="theorem", label="Elimination Theorem",
                       statement="Let I be an ideal in k[x_1..x_n] and G a Groebner basis w.r.t. lex. Then G_l = G ∩ k[x_{l+1}..x_n] is a Groebner basis of the l-th elimination ideal.")
    qid = store.insert(conn, "questions", kind="survey", lane="harvest", title="Survey elimination theory for hard inverses",
                       body="Projections of varieties over finite fields.", domains=["algebraic geometry"])

    provider = FakeProvider([
        FakeProvider.call("search_units", {"query": "Groebner elimination", "limit": 5}, "c1"),
        FakeProvider.call("run_script", {"code": "print('RESULT: {\"fiber_sizes\": [1, 2, 4]}')", "engine": "python",
                                         "purpose": "fiber sizes of a projection over F_5"}, "c2"),
        FakeProvider.final(_writeback(
            findings=[
                {"kind": "fact", "claim": "Lex Groebner bases compute elimination ideals.", "evidence_kind": "unit",
                 "evidence_id": uid, "evidence_note": "Elimination Theorem", "confidence": 0.95},
                {"kind": "experimental", "claim": "Fibers of the projection have sizes 1, 2 or 4 over F_5.",
                 "evidence_kind": "experiment", "evidence_id": "EXPERIMENT", "evidence_note": "RESULT line",
                 "confidence": 0.7},
            ],
            new_questions=[
                {"kind": "learn", "lane": "harvest", "title": "Learn: counting points on fibers over finite fields",
                 "body": "Need Weil bounds and Chevalley-Warning.", "blocks_parent": True, "priority": 0.8},
            ],
            candidate_problems=[
                {"name": "Projection fiber recovery", "objects": "Varieties over F_q", "forward_map": "projection",
                 "distribution": "random ideal", "hidden": "a point in the fiber", "verification": "evaluate polynomials",
                 "domains": "algebraic geometry, finite fields", "known_relatives": "polynomial system solving (MQ)",
                 "notes": ""},
            ],
        )),
    ])

    question = store.get(conn, "questions", qid)
    outcome = run_question(conn, charter, provider, question, tmp_path, max_steps=6)
    assert outcome.status == "done", outcome.error
    assert outcome.steps == 3
    assert outcome.usage.input_tokens == 300

    # the prompts: stable system prefix, question in the user turn, tool results fed back
    first = provider.requests[0]
    assert first.system.startswith("# Research charter") and "Lane: harvest" in first.system
    assert "Survey elimination theory" in first.messages[0].text()
    assert "Elimination Theorem" in first.messages[0].text()  # related unit surfaced
    assert first.output_schema is WRITEBACK_SCHEMA and first.effort == "low"
    second = provider.requests[1]
    assert second.messages[-1].content[0].tool_use_id == "c1"
    assert "Elimination Theorem" in second.messages[-1].content[0].content
    third = provider.requests[2]
    assert "fiber_sizes" in third.messages[-1].content[0].content

    # persisted state
    assert db.table_counts(conn)["experiments"] == 1
    exp = store.select(conn, "experiments")[0]
    assert exp["question_id"] == qid and exp["result"] == {"fiber_sizes": [1, 2, 4]}
    findings = store.select(conn, "findings")
    assert len(findings) == 2 and findings[0]["evidence_id"] == uid
    problems = store.select(conn, "problems")
    assert len(problems) == 1 and problems[0]["status"] == "idea" and "finite fields" in problems[0]["domains"]
    qs = {q["title"]: q for q in store.select(conn, "questions")}
    parent = qs["Survey elimination theory for hard inverses"]
    assert parent["status"] == "blocked" and parent["runs"] == 1 and "Elimination theory" in parent["summary"]
    learn = qs["Learn: counting points on fibers over finite fields"]
    assert learn["parent_id"] == qid and learn["kind"] == "learn"
    deepen = [q for q in qs.values() if q["lane"] == "deepen"]
    assert len(deepen) == 1 and deepen[0]["problem_id"] == problems[0]["id"]
    run = store.select(conn, "runs")[0]
    assert run["status"] == "done" and run["context_ids"]["experiments"] == [exp["id"]]

    # closing the learn question unblocks the parent
    store.close_question(conn, learn["id"])
    assert store.get(conn, "questions", qid)["status"] == "open"


def test_invalid_json_then_recovery(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    qid = store.insert(conn, "questions", kind="explore", lane="open", title="x")
    provider = FakeProvider([
        ChatResponse(blocks=[TextBlock("I think the answer is...")], stop_reason="end_turn"),
        FakeProvider.final(_writeback(status="closed")),
    ])
    outcome = run_question(conn, _charter(), provider, store.get(conn, "questions", qid), tmp_path)
    assert outcome.status == "done" and outcome.steps == 2
    assert store.get(conn, "questions", qid)["status"] == "closed"


def test_failed_run_leaves_question_open(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    qid = store.insert(conn, "questions", kind="explore", lane="open", title="x")
    provider = FakeProvider([])  # raises: out of responses
    outcome = run_question(conn, _charter(), provider, store.get(conn, "questions", qid), tmp_path)
    assert outcome.status == "failed" and "FakeProvider" in outcome.error
    assert store.get(conn, "questions", qid)["status"] == "open"
    assert store.select(conn, "runs")[0]["status"] == "failed"


def test_run_cycles_seeds_and_runs(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    provider = FakeProvider([FakeProvider.final(_writeback(status="closed")) for _ in range(3)])
    outcomes = run_cycles(conn, _charter(), provider, tmp_path, cycles=3, seed=1)
    assert len(outcomes) == 3 and all(o.status == "done" for o in outcomes)
    assert conn.execute("SELECT COUNT(*) AS n FROM questions WHERE status='closed'").fetchone()["n"] == 3
    assert db.table_counts(conn)["runs"] == 3


def test_validate_rejects_bad_kinds():
    import pytest
    with pytest.raises(WritebackError):
        validate(_writeback(findings=[{"kind": "guess", "claim": "", "evidence_kind": "unit"}]))
    with pytest.raises(WritebackError):
        validate(_writeback(status="done"))
