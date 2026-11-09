"""Lessons from experts' published work: prompts, timely reviews, tools, CLI, retrodiction."""

import re

import pytest
import yaml

from mathresearch import catalog, db, ingest, lessons, retro, store
from mathresearch.charter import DEFAULT_CHARTER, Charter, CharterError
from mathresearch.cli import main
from mathresearch.providers.base import FakeProvider
from mathresearch.reflect import reflect
from mathresearch.tools import registry
from mathresearch.worker import run_question

EXPERTS = {"Regev", "Peikert", "Ducas", "Lyubashevsky", "Bai"}
WB = {"summary": "ok", "status": "open", "findings": [], "new_questions": [], "candidate_problems": []}


def _charter(**over):
    data = yaml.safe_load(DEFAULT_CHARTER)
    data.update(over)
    return Charter.from_dict(data)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "r.db")
    catalog.load(c)
    return c


def _candidate(conn, status="well_defined", domains=("multivariate quadratic systems",), name="Cand"):
    return store.insert(conn, "problems", name=name, status=status, domains=list(domains))


def test_catalog_lessons_cover_the_five_experts(conn):
    rows = lessons.all_lessons(conn)
    assert len(rows) >= 12
    assert {e for r in rows for e in r["experts"]} == EXPERTS
    for r in rows:
        assert r["lesson"] and r["check_text"] and r["refs"], r["id"]
        for ref in r["sources"] or []:
            assert re.fullmatch(r"\d{4}/\d{3,5}|arXiv:\d{4}\.\d{4,5}", ref), ref
            assert ingest.ax.classify(ref) is not None


def test_prompt_block_reaches_every_worker(conn, tmp_path):
    block = lessons.prompt_block(conn)
    assert block.startswith("# Lessons from the field") and "never speak for the authors" in block
    assert all(e in block for e in EXPERTS) and "[ls_structure_needs_justification]" in block
    assert len(block) < 5000
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="survey", lane="harvest", title="Q"))
    prov = FakeProvider([FakeProvider.final(WB)])
    run_question(conn, _charter(), prov, q, tmp_path)
    assert "# Lessons from the field" in prov.requests[0].system


def test_reviews_open_rotate_and_do_not_repeat(conn):
    charter = _charter()
    pid = _candidate(conn)
    _candidate(conn, status="idea", name="Raw idea")       # below from_status: not reviewed
    r1 = reflect(conn, charter)
    reviews = store.select(conn, "questions", "origin = 'review'")
    assert len(reviews) == 1 and any("opened review" in a for a in r1.actions)
    first = reviews[0]
    expert = first["origin_id"].split("|")[0]
    assert first["problem_id"] == pid and first["lane"] == "deepen" and expert in EXPERTS
    assert expert in first["title"] and "do not speak for" in first["body"]
    # an open review blocks another; the same status is never reviewed twice
    reflect(conn, charter)
    assert len(store.select(conn, "questions", "origin = 'review'")) == 1
    store.close_question(conn, first["id"])
    reflect(conn, charter)
    assert len(store.select(conn, "questions", "origin = 'review'")) == 1
    # a new ladder status earns a review by a different expert
    store.update(conn, "problems", pid, status="not_trivially_broken")
    reflect(conn, charter)
    second = [q for q in store.select(conn, "questions", "origin = 'review'") if q["id"] != first["id"]]
    assert len(second) == 1 and second[0]["origin_id"].split("|")[0] != expert
    assert second[0]["origin_id"].endswith("|not_trivially_broken")
    assert "Expert reviews: 2" in reflect(conn, charter).digest


def test_review_limits_and_switch(conn):
    for i in range(4):
        _candidate(conn, name=f"C{i}")
    reflect(conn, _charter(reviews={"max_per_reflection": 1}))
    assert len(store.select(conn, "questions", "origin = 'review'")) == 1
    conn2 = db.connect(":memory:")
    catalog.load(conn2)
    _candidate(conn2)
    reflect(conn2, _charter(reviews={"enabled": False}))
    assert not store.select(conn2, "questions", "origin = 'review'")


def test_lattice_candidates_get_lattice_lessons(conn):
    pid = _candidate(conn, domains=["lattices (LWE variants)"])
    p = store.get(conn, "problems", pid)
    ids = {r["id"] for r in lessons.lessons_for(conn, p["domains"])}
    assert {"ls_small_secrets", "ls_instantiate_as_analysed", "ls_concrete_cost"} <= ids
    tensors = {r["id"] for r in lessons.lessons_for(conn, ["group actions on tensors"])}
    assert "ls_classical_math_to_assumption" in tensors and "ls_small_secrets" not in tensors


def test_list_lessons_tool(conn, tmp_path):
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="survey", lane="harvest", title="Q"))
    ctx = registry.ToolContext(conn=conn, workspace=tmp_path, question=q, run_id="r")
    text, err = registry.execute("list_lessons", {"expert": "Peikert", "problem_id": ""}, ctx)
    assert not err and "ls_problem_vs_scheme" in text and "ls_small_secrets" not in text
    assert "ingest: 2015/939" in text


def test_charter_reviews_section():
    assert _charter().reviews.enabled and _charter().reviews.from_status == "well_defined"
    with pytest.raises(CharterError):
        _charter(reviews={"from_status": "broken"})
    with pytest.raises(CharterError):
        _charter(reviews={"every": 3})


def test_cli_lessons_list_and_ingest(tmp_path, capsys, monkeypatch):
    main(["--dir", str(tmp_path), "init"])
    main(["--dir", str(tmp_path), "catalog", "load"])
    capsys.readouterr()
    assert main(["--dir", str(tmp_path), "lessons", "list", "--expert", "Ducas"]) == 0
    out = capsys.readouterr().out
    assert "ls_concrete_cost" in out and "ls_small_secrets" not in out
    conn = db.connect(tmp_path / "research.db")
    fetched = []

    def fake(conn_, ref, ws, **kw):
        fetched.append(ref)
        sid = store.insert(conn_, "sources", kind="paper", title=f"paper {ref}")
        return ingest.IngestResult(source_id=sid, title=f"paper {ref}", format="html", units=0)

    monkeypatch.setattr(ingest, "ingest_reference", fake)
    monkeypatch.setattr(ingest.ax, "polite_sleep", lambda s: None)
    assert main(["--dir", str(tmp_path), "lessons", "ingest", "--expert", "Bai"]) == 0
    assert fetched == ["2013/839", "2018/856"]
    assert len(store.select(conn, "questions", "kind = 'seed'")) == 2


def test_retro_hides_all_lessons(tmp_path):
    conn = db.connect(tmp_path / "research.db")
    res = retro.setup(conn, tmp_path, retro.PATARIN_1995)
    assert res["catalog"]["lessons"] == 0 and not lessons.prompt_block(conn) and res["leaks"] == []
