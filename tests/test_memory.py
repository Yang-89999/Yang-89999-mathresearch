"""Searching the system's own memory, and keeping run transcripts for the operator."""

import yaml

from mathresearch import db, store
from mathresearch.charter import DEFAULT_CHARTER, Charter
from mathresearch.cli import main
from mathresearch.providers.base import FakeProvider
from mathresearch.tools import registry
from mathresearch.worker import load_transcript, run_question, transcript_path

WB = {"summary": "ok", "status": "open", "findings": [], "new_questions": [], "candidate_problems": []}


def _seed(conn):
    q = store.insert(conn, "questions", kind="attack", lane="deepen", status="open",
                     title="Does MinRank break the planted quiver problem?", summary="Rank defects appear at n=8.")
    store.insert(conn, "findings", question_id=q, kind="negative", evidence_kind="reasoning",
                 claim="The public quadratic forms of the planted subrepresentation scheme have a rank defect.")
    store.insert(conn, "reading_notes", question_id=q, surprises="Grassmannian fibres are smooth",
                 structure_exploited="low-rank linear combinations")
    store.insert(conn, "problems", name="Planted subrepresentation", status="idea", hidden="the subrepresentation")
    return q


def test_search_memory_spans_findings_questions_notes_and_problems(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    _seed(conn)
    kinds = {k for k, _, _ in store.search_memory(conn, "rank")}
    assert kinds == {"findings", "questions", "notes"}
    assert [k for k, _, _ in store.search_memory(conn, "subrepresentation", ["problems"])] == ["problems"]
    # all words are required first; with no such match, any word is enough; bad FTS syntax never raises
    assert store.search_memory(conn, "rank defect zebra")
    assert store.search_memory(conn, 'rank AND (("') is not None
    # updates are indexed: a question's new summary is searchable, the old one is not
    q = store.select(conn, "questions")[0]
    store.update(conn, "questions", q["id"], summary="Closed: no low-weight codewords found.")
    assert store.search_memory(conn, "codewords", ["questions"]) and not store.search_memory(conn, "n=8 defects",
                                                                                              ["questions"])


def test_migration_indexes_existing_memory(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "SCHEMA_VERSION", 4)
    old = db.connect(path)
    store.insert(old, "findings", kind="fact", evidence_kind="reasoning", claim="Tensor isomorphism resists Groebner")
    old.close()
    monkeypatch.undo()
    conn = db.connect(path)
    assert db.current_version(conn) == db.SCHEMA_VERSION == 5
    assert store.search_memory(conn, "tensor isomorphism")


def test_search_findings_tool_and_cli(tmp_path, capsys):
    main(["--dir", str(tmp_path), "init"])
    conn = db.connect(tmp_path / "research.db")
    _seed(conn)
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="survey", lane="harvest", title="Q"))
    ctx = registry.ToolContext(conn=conn, workspace=tmp_path, question=q, run_id="r")
    text, err = registry.execute("search_findings", {"query": "rank defect", "kind": "all", "limit": 5}, ctx)
    assert not err and "rank defect" in text and "finding (negative" in text
    text, _ = registry.execute("search_findings", {"query": "unrelated", "kind": "all", "limit": 5}, ctx)
    assert text.startswith("Nothing in the research memory")
    capsys.readouterr()
    assert main(["--dir", str(tmp_path), "memory", "search", "Grassmannian"]) == 0
    assert "reading note" in capsys.readouterr().out


def test_every_run_keeps_a_transcript(tmp_path, capsys):
    conn = db.connect(tmp_path / "research.db")
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="survey", lane="harvest", title="Q"))
    prov = FakeProvider([FakeProvider.call("list_catalog", {"kind": "problems"}), FakeProvider.final(WB)])
    out = run_question(conn, Charter.from_dict(yaml.safe_load(DEFAULT_CHARTER)), prov, q, tmp_path)
    assert transcript_path(tmp_path, out.run_id).exists()
    doc = load_transcript(tmp_path, out.run_id)
    assert doc["status"] == "done" and "# Research charter" in doc["system"]
    types = [b["type"] for m in doc["messages"] for b in m["content"]]
    assert "tool_use" in types and "tool_result" in types
    assert main(["--dir", str(tmp_path), "transcript", out.run_id]) == 0
    printed = capsys.readouterr().out
    assert "-> list_catalog(" in printed and "<- " in printed
    assert main(["--dir", str(tmp_path), "transcript", "run_missing"]) == 1
