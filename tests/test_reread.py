"""Once in a while a source is read again, with what its first reading led to in front of the reader."""

import random

import yaml

from mathresearch import agenda, db, reading, store, writeback
from mathresearch.charter import DEFAULT_CHARTER, Charter
from mathresearch.context import user_prompt


def _charter(**over):
    c = Charter.from_dict(yaml.safe_load(DEFAULT_CHARTER))
    c.candidate_target = 0
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _read_source(conn, title, events):
    """A source whose seed read is closed, with ``events`` findings made later on a question it sparked."""
    sid = store.insert(conn, "sources", kind="paper", title=title, provided_by="operator")
    seed = store.insert(conn, "questions", kind="seed", lane="harvest", title=f"Seed read: {title}",
                        origin="operator", origin_id=sid, status="closed")
    store.insert(conn, "reading_notes", question_id=seed, source_id=sid, prior="expected a Groebner attack",
                 surprises="the attack is graph-theoretic", ideas=["hide the low-rank points"],
                 created_at="2026-01-01T00:00:00.000Z")
    q = store.insert(conn, "questions", kind="investigate", lane="deepen", title=f"Idea from {title}",
                     origin="reference", origin_id=sid)
    child = store.insert(conn, "questions", kind="attack", lane="deepen", title="Attack it", parent_id=q)
    for i in range(events):
        store.insert(conn, "findings", question_id=child, kind="negative" if i == 0 else "experimental",
                     claim=f"{title}: rank filter result {i}", evidence_kind="reasoning")
    return sid


def _wb(**extra):
    data = {"summary": "s", "status": "closed", "findings": [], "new_questions": [], "candidate_problems": []}
    data.update(extra)
    return writeback.validate(data)


def test_the_source_with_the_most_new_experience_is_read_again(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    charter = _charter(reread_every_runs=2, reread_min_events=3)
    quiet = _read_source(conn, "A quiet paper", events=1)
    busy = _read_source(conn, "Graph-theoretic algorithms for ATFE", events=4)
    pid = store.insert(conn, "problems", name="Hidden low-rank points", status="broken", refs=[busy])
    assert agenda.ensure_reread(conn, charter) is None                      # not before reread_every_runs runs
    for _ in range(2):
        store.insert(conn, "runs", status="done")
    qid = agenda.ensure_reread(conn, charter)
    q = store.get(conn, "questions", qid)
    assert q["kind"] == "reread" and q["origin_id"] == busy and q["lane"] == "harvest"
    body = q["body"]
    assert "expected a Groebner attack" in body and "idea then: hide the low-rank points" in body   # the first notes
    assert f"candidate {pid} 'Hidden low-rank points' is now broken" in body                    # what happened since
    assert "(negative): Graph-theoretic algorithms for ATFE: rank filter result 0" in body
    assert quiet not in body
    prompt = user_prompt(conn, q)
    assert "This is a re-read" in prompt and "The reference behind this question" in prompt
    assert agenda.ensure_reread(conn, charter) is None                      # one at a time
    charter.lanes = {"harvest": 1.0, "deepen": 0.0, "open": 0.0}
    assert agenda.pick(conn, charter, random.Random(0))["id"] == qid

    # it closes only with something new sparked by the source, or a stated "nothing new"
    run = store.insert(conn, "runs", question_id=qid, status="done")
    created = writeback.apply(conn, run, q, _wb())
    assert any("re-read kept open" in w for w in created["warnings"])
    assert store.get(conn, "questions", qid)["status"] == "open"
    new_q = {"kind": "attack", "lane": "deepen", "title": "The filter fails when points have rank 6", "body": "b",
             "blocks_parent": False, "priority": 0.6, "sparked_by": "reference", "sparked_by_id": busy}
    note = {"source_id": busy, "prior": "now expect the rank bound to matter", "decision": "d", "confirmed": "",
            "surprises": "Remark 3 limits the filter to rank 4", "hardness": "", "structure_exploited": "",
            "limits": "", "ideas": "", "patterns": "", "unit_ids": ""}
    created = writeback.apply(conn, run, store.get(conn, "questions", qid),
                              _wb(new_questions=[new_q], reading_notes=[note]))
    assert not [w for w in created["warnings"] if "kept open" in w]
    assert store.get(conn, "questions", qid)["status"] == "closed"

    # the new note is the new baseline: the same source is not read again until more has happened
    for _ in range(2):
        store.insert(conn, "runs", status="done", started_at="2999-01-01T00:00:00.000Z")
    assert agenda.ensure_reread(conn, charter) is None
    assert agenda.ensure_reread(conn, _charter(reread_every_runs=0)) is None


def test_a_reread_can_close_with_nothing_new(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    sid = _read_source(conn, "A paper", events=3)
    rows = reading.reread_candidates(conn, 3)
    assert [r["source"]["id"] for r in rows] == [sid] and rows[0]["events"] == 3
    qid = reading.open_reread(conn, rows[0])
    run = store.insert(conn, "runs", question_id=qid, status="done")
    writeback.apply(conn, run, store.get(conn, "questions", qid), _wb(findings=[{
        "kind": "negative", "claim": "The second reading adds nothing: the results concern rank 6, outside its scope.",
        "evidence_kind": "reasoning", "evidence_id": "", "evidence_note": "", "confidence": 0.7}]))
    assert store.get(conn, "questions", qid)["status"] == "closed"
