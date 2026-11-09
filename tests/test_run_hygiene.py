"""Lessons from the first 20-cycle run: one question must not take every run, provider refusals are named, and
an abstract-only ePrint record defers to the full text held under another source."""

import yaml

from mathresearch import db, ingest, library, store
from mathresearch.charter import DEFAULT_CHARTER, Charter
from mathresearch.context import _question_block
from mathresearch.providers.base import FakeProvider
from mathresearch.reading import read_source
from mathresearch.worker import REFUSED, is_policy_refusal, run_question


def _charter():
    return Charter.from_dict(yaml.safe_load(DEFAULT_CHARTER))


def _wb(status):
    return {"summary": "Progress on the trilinear form question, more to do.", "status": status,
            "findings": [], "new_questions": [], "candidate_problems": []}


def test_a_question_that_stays_open_loses_priority(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    qid = store.insert(conn, "questions", kind="learn", lane="harvest", title="Reconcile ALTEQ", priority=0.9)
    other = store.insert(conn, "questions", kind="learn", lane="harvest", title="Seed read", priority=0.7)
    for _ in range(2):
        run_question(conn, _charter(), FakeProvider([FakeProvider.final(_wb("open"))]),
                     store.get(conn, "questions", qid), tmp_path)
    q = store.get(conn, "questions", qid)
    assert q["runs"] == 2 and abs(q["priority"] - 0.9 * 0.7 * 0.7) < 1e-9
    assert store.open_questions(conn, lane="harvest")[0]["id"] == other     # the next pick goes elsewhere
    assert "Do not repeat earlier work" not in _question_block(q)
    assert "has had 3 runs" in _question_block({**q, "runs": 3})


def test_provider_refusal_is_named_and_demotes_the_question(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    qid = store.insert(conn, "questions", kind="attack", lane="deepen", title="x", priority=0.8)

    class Refusing(FakeProvider):
        def complete(self, request):
            raise RuntimeError("BadRequestError: Error code: 400 - This content was flagged for possible "
                               "cybersecurity risk.")

    out = run_question(conn, _charter(), Refusing(), store.get(conn, "questions", qid), tmp_path)
    assert out.status == "failed" and out.error.startswith(REFUSED)
    q = store.get(conn, "questions", qid)
    assert q["status"] == "open" and q["priority"] == 0.4
    assert not is_policy_refusal("ConnectionError: timed out")


def test_abstract_only_record_defers_to_the_full_text_twin(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    title = "General Linear Group Action on Tensors: A Candidate for Post-Quantum Cryptography"
    full = store.insert(conn, "sources", kind="paper", title=title, identifiers={"arxiv": "1906.04330"},
                        format="latex")
    store.insert(conn, "units", source_id=full, kind="theorem", statement="TI is hard.")
    abstract = store.insert(conn, "sources", kind="paper", title=title.upper(), identifiers={"eprint": "2019/687"},
                            format="html")
    lone = store.insert(conn, "sources", kind="paper", title="Hard Homogeneous Spaces Revisited",
                        identifiers={"eprint": "2006/291"}, format="html")
    assert [s["id"] for s in ingest.abstract_only(conn)] == [lone]
    assert [t for t, _, _ in library.missing_full_text(conn, tmp_path)] == ["Hard Homogeneous Spaces Revisited"]
    assert full in read_source(conn, abstract, "card") and "Read that one" in read_source(conn, abstract, "outline")
