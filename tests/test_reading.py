"""Purpose-driven reading: seed reads, levelled reading, reading notes, origins, weakness audits."""

import pytest
import yaml

from mathresearch import agenda, catalog, db, ingest, reading, retro, store, writeback
from mathresearch.charter import DEFAULT_CHARTER, Charter, CharterError
from mathresearch.cli import main
from mathresearch.context import user_prompt
from mathresearch.providers.base import FakeProvider
from mathresearch.reflect import reflect
from mathresearch.tools import registry
from mathresearch.worker import run_question

PAPER = r"""
\documentclass{article}
\newtheorem{thm}{Theorem}
\newtheorem{defn}{Definition}
\title{Hidden Field Equations}
\begin{document}
\section{The scheme}
\begin{defn}\label{def:hfe}
An HFE central map is a univariate polynomial of degree D over $\mathbb{F}_{q^n}$ with quadratic monomials.
\end{defn}
\section{Attacks}
\begin{thm}\label{thm:rank}
The quadratic form of the central map over the extension field has rank at most $\lceil \log_q D \rceil$.
\end{thm}
\end{document}
"""


def _charter(**over):
    data = yaml.safe_load(DEFAULT_CHARTER)
    data.update(over)
    return Charter.from_dict(data)


def _wb(**over):
    data = {"summary": "ok", "status": "open", "findings": [], "new_questions": [], "candidate_problems": [],
            "reading_notes": [], "source_cards": [], "weakness_audits": []}
    data.update(over)
    return data


def _question(conn, **kw):
    kw.setdefault("kind", "investigate")
    kw.setdefault("lane", "harvest")
    kw.setdefault("title", "Q")
    return store.get(conn, "questions", store.insert(conn, "questions", **kw))


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "r.db")
    catalog.load(c)
    return c


@pytest.fixture
def paper(conn, tmp_path):
    path = tmp_path / "hfe.tex"
    path.write_text(PAPER, encoding="utf-8")
    return ingest.ingest_file(conn, path, year=1996).source_id


def _card(sid, **over):
    card = {"source_id": sid, **{k: f"{k} text" for k in reading.CARD_FIELDS}}
    card.update(over)
    return card


def _q(title, sparked_by="prior", sparked_by_id="", **over):
    q = {"kind": "explore", "lane": "harvest", "title": title, "body": "b", "blocks_parent": False,
         "priority": 0.5, "sparked_by": sparked_by, "sparked_by_id": sparked_by_id}
    q.update(over)
    return q


def _cand(name, sparked_by="prior", sparked_by_id=""):
    return {"name": name, "objects": "o", "forward_map": "f", "distribution": "d", "hidden": "h",
            "verification": "v", "domains": "multivariate quadratic systems", "known_relatives": "MQ",
            "notes": "", "sparked_by": sparked_by, "sparked_by_id": sparked_by_id}


# -- levelled reading -----------------------------------------------------------------

def test_read_source_levels_and_brief(conn, paper):
    card = reading.read_source(conn, paper, "card")
    assert "Hidden Field Equations" in card and "No paper card yet" in card and "The scheme (1)" in card
    out = reading.read_source(conn, paper, "outline")
    assert "## Attacks" in out and "theorem" in out
    refused = reading.read_source(conn, paper, "section", section="Attacks", purpose="")
    assert "reading brief" in refused
    sec = reading.read_source(conn, paper, "section", section="attacks",
                              purpose="if rank stays log_q D, the candidate keeps a MinRank weakness")
    assert "rank at most" in sec and "HFE central map" not in sec
    assert "No section matches" in reading.read_source(conn, paper, "section", section="proofs",
                                                       purpose="long enough purpose here")


def test_read_source_tool_logs_reads(conn, paper, tmp_path):
    q = _question(conn)
    ctx = registry.ToolContext(conn=conn, workspace=tmp_path, question=q, run_id="run_x")
    text, err = registry.execute("read_source", {"source_id": paper, "level": "card", "section": "",
                                                 "purpose": ""}, ctx)
    assert not err and ctx.reads == [{"source_id": paper, "level": "card", "section": "", "purpose": ""}]
    text, _ = registry.execute("list_weakness_patterns", {"problem_id": ""}, ctx)
    assert "wp_low_rank_central_map" in text and "test:" in text


# -- seed reads -----------------------------------------------------------------------

def test_cli_ingest_queues_seed_read(tmp_path, capsys):
    main(["--dir", str(tmp_path), "init"])
    f = tmp_path / "hfe.tex"
    f.write_text(PAPER, encoding="utf-8")
    assert main(["--dir", str(tmp_path), "ingest", "file", str(f)]) == 0
    assert "seed read queued" in capsys.readouterr().out
    conn = db.connect(tmp_path / "research.db")
    src = store.select(conn, "sources")[0]
    seeds = store.select(conn, "questions", "kind = 'seed'")
    assert src["provided_by"] == "operator" and len(seeds) == 1
    assert seeds[0]["origin"] == "operator" and seeds[0]["origin_id"] == src["id"] and seeds[0]["priority"] == 0.8
    # seeding again is idempotent; --no-seed indexes only
    assert main(["--dir", str(tmp_path), "ingest", "seed", src["id"]]) == 0
    assert len(store.select(conn, "questions", "kind = 'seed'")) == 1
    g = tmp_path / "other.tex"
    g.write_text(PAPER.replace("Hidden Field Equations", "Other"), encoding="utf-8")
    assert main(["--dir", str(tmp_path), "ingest", "file", str(g), "--no-seed"]) == 0
    assert len(store.select(conn, "questions", "kind = 'seed'")) == 1


def test_agent_ingested_sources_are_not_seeded(conn, tmp_path, monkeypatch):
    q = _question(conn)
    ctx = registry.ToolContext(conn=conn, workspace=tmp_path, question=q, run_id="run_x")
    sid = store.insert(conn, "sources", kind="paper", title="T")
    from mathresearch.ingest import IngestResult
    monkeypatch.setattr(ingest, "ingest_reference",
                        lambda *a, **k: IngestResult(source_id=sid, title="T", format="latex", units=0))
    text, _ = registry.execute("ingest_source", {"reference": "2301.00001", "domains": ""}, ctx)
    assert "read_source" in text and store.get(conn, "sources", sid)["provided_by"] == "agent"
    assert not store.select(conn, "questions", "kind = 'seed'")


def test_seed_read_cannot_close_without_a_spark(conn, paper):
    seed = store.get(conn, "questions", reading.seed_reference(conn, paper))
    created = writeback.apply(conn, "run_1", seed, writeback.validate(_wb(status="closed")))
    assert store.get(conn, "questions", seed["id"])["status"] == "open"
    assert "paper card" in created["warnings"][-1]

    seed = store.get(conn, "questions", seed["id"])
    writeback.apply(conn, "run_2", seed, writeback.validate(_wb(status="closed", source_cards=[_card(paper)])))
    q = store.get(conn, "questions", seed["id"])
    assert q["status"] == "open" and "sparked by this reference" in q["summary"]

    created = writeback.apply(conn, "run_3", q, writeback.validate(_wb(
        status="closed",
        new_questions=[_q("Remove the low-rank structure from HFE", "reference", paper)],
        reading_notes=[{"source_id": paper, "prior": "HFE breaks by MinRank", "decision": "which variant to try",
                        "confirmed": "rank bound", "surprises": "none", "hardness": "MQ",
                        "structure_exploited": "low rank", "limits": "D small", "ideas": ["raise the rank"],
                        "weakness_patterns": ["wp_low_rank_central_map"], "unit_ids": []}])))
    assert store.get(conn, "questions", seed["id"])["status"] == "closed"
    child = store.get(conn, "questions", created["questions"][0])
    assert child["origin"] == "reference" and child["origin_id"] == paper
    note = store.get(conn, "reading_notes", created["notes"][0])
    assert note["ideas"] == ["raise the rank"] and note["patterns"] == ["wp_low_rank_central_map"]
    assert store.get(conn, "sources", paper)["card"]["problem"] == "problem text"


def test_seed_read_may_close_with_a_negative_finding(conn, paper):
    seed = store.get(conn, "questions", reading.seed_reference(conn, paper))
    writeback.apply(conn, "r", seed, writeback.validate(_wb(
        status="closed", source_cards=[_card(paper)],
        findings=[{"kind": "negative", "claim": "Only a survey of known parameters; nothing to transfer.",
                   "evidence_kind": "source", "evidence_id": paper, "evidence_note": "", "confidence": 0.7}])))
    assert store.get(conn, "questions", seed["id"])["status"] == "closed"


# -- origins ---------------------------------------------------------------------------

def test_origins_are_checked(conn, paper):
    q = _question(conn)
    exp = store.insert(conn, "experiments", engine="python", code="x", code_sha256="")
    created = writeback.apply(conn, "r", q, writeback.validate(_wb(new_questions=[
        _q("a", "reference", paper), _q("b", "reference", "src_missing"), _q("c", "experiment", exp),
        _q("d")], candidate_problems=[_cand("Twisted HFE", "reference", paper)])))
    rows = {store.get(conn, "questions", i)["title"][:1]: store.get(conn, "questions", i) for i in created["questions"]}
    assert (rows["a"]["origin"], rows["a"]["origin_id"]) == ("reference", paper)
    assert (rows["b"]["origin"], rows["b"]["origin_id"]) == ("prior", None)   # unknown source: not believed
    assert (rows["c"]["origin"], rows["c"]["origin_id"]) == ("experiment", exp)
    assert rows["d"]["origin"] == "prior"
    investigate = rows["M"]  # "Make 'Twisted HFE' well defined ..."
    assert investigate["origin"] == "reference" and investigate["origin_id"] == paper
    assert store.get(conn, "problems", investigate["problem_id"])["refs"] == [paper]


def test_old_writebacks_without_reading_fields_still_apply(conn):
    q = _question(conn)
    old = {"summary": "s", "status": "closed", "findings": [], "new_questions": [], "candidate_problems": []}
    writeback.apply(conn, "r", q, writeback.validate(old))
    assert store.get(conn, "questions", q["id"])["status"] == "closed"


# -- weakness audits -----------------------------------------------------------------

def test_weakness_audit_needs_experiments(conn):
    q = _question(conn)
    exp = store.insert(conn, "experiments", engine="python", code="x", code_sha256="")
    created = writeback.apply(conn, "r", q, writeback.validate(_wb(
        candidate_problems=[_cand("Rank-raised HFE")],
        weakness_audits=[
            {"problem_id": "new:Rank-raised HFE", "pattern_id": "wp_low_rank_central_map", "status": "avoided",
             "evidence_id": exp, "note": "min rank of 200 random combos is n"},
            {"problem_id": "new:rank-raised hfe", "pattern_id": "wp_invariant_subspace", "status": "avoided",
             "evidence_id": "", "note": "I think so"},
            {"problem_id": "new:Rank-raised HFE", "pattern_id": "wp_bilinear_io_relations", "status": "vulnerable",
             "evidence_id": exp, "note": "kernel dimension 3"},
            {"problem_id": "new:Rank-raised HFE", "pattern_id": "wp_nonexistent", "status": "untested",
             "evidence_id": "", "note": ""}])))
    pid = created["problems"][0]
    audit = store.get(conn, "problems", pid)["weakness_audit"]
    assert audit["wp_low_rank_central_map"]["status"] == "avoided"
    assert audit["wp_invariant_subspace"]["status"] == "untested"
    assert audit["wp_bilinear_io_relations"]["status"] == "vulnerable"
    assert any("without an experiment" in w for w in created["warnings"])
    assert any("wp_nonexistent" in w for w in created["warnings"])
    neg = store.select(conn, "findings", "problem_id = ? AND kind = 'negative'", (pid,))
    assert len(neg) == 1 and "wp_bilinear_io_relations" in neg[0]["claim"]
    summary = reading.audit_summary(conn, store.get(conn, "problems", pid))
    assert "wp_abelian_hidden_structure" in summary["untested"]          # generic patterns always apply
    assert "wp_schur_square_distinguisher" not in sum(summary.values(), [])  # codes pattern does not
    with pytest.raises(writeback.WritebackError):
        writeback.validate(_wb(weakness_audits=[{"problem_id": pid, "pattern_id": "x", "status": "maybe",
                                                 "evidence_id": "", "note": ""}]))


# -- context and reflection --------------------------------------------------------

def test_context_shows_reference_card_notes_and_audit(conn, paper):
    seed = store.get(conn, "questions", reading.seed_reference(conn, paper))
    text = user_prompt(conn, seed)
    assert "This is a seed read" in text and "reference behind this question" in text and "Hidden Field" in text
    pid = store.insert(conn, "problems", name="Cand", status="idea", domains=["multivariate"])
    q = _question(conn, problem_id=pid, title="Investigate HFE central map rank")
    store.insert(conn, "reading_notes", question_id=q["id"], source_id=paper, decision="go or no-go",
                 surprises="rank is lower", ideas=["try D large"])
    text = user_prompt(conn, q)
    assert "Weakness audit" in text and "untested: " in text and "wp_low_rank_central_map" in text
    assert "Your reading notes" in text and "try D large" in text
    assert "Papers that may be relevant" in text


def test_reflection_balances_origins_and_reports_references(conn, paper):
    charter = _charter(reading={"max_prior_share": 0.5})
    reading.seed_reference(conn, paper)
    grounded = _question(conn, origin="reference", origin_id=paper, priority=0.6, title="grounded")
    prior = [_question(conn, origin="prior", priority=0.6, title=f"p{i}") for i in range(4)]
    r = reflect(conn, charter)
    assert any("prior-only questions" in a for a in r.actions)
    assert all(store.get(conn, "questions", p["id"])["priority"] < 0.6 for p in prior)
    assert store.get(conn, "questions", grounded["id"])["priority"] == 0.6
    assert "Open questions sparked by:" in r.digest and "## References" in r.digest
    assert "Seed reads waiting" in r.digest
    assert r.stats["references"]["useful"][0]["id"] == paper


def test_charter_reading_section():
    assert _charter().reading.seed_operator_references is True
    assert _charter(reading={"max_prior_share": 0.3}).reading.max_prior_share == 0.3
    with pytest.raises(CharterError):
        _charter(reading={"max_prior_share": 2})
    with pytest.raises(CharterError):
        _charter(reading={"seed_everything": True})


def test_agenda_seed_marks_charter_origin(conn):
    agenda.seed(conn, _charter())
    assert {q["origin"] for q in store.select(conn, "questions")} == {"charter"}


# -- retrodiction ------------------------------------------------------------------------

def test_retro_scenario_hides_later_weakness_patterns(tmp_path):
    conn = db.connect(tmp_path / "research.db")
    res = retro.setup(conn, tmp_path, retro.PATARIN_1995)
    ids = {r["id"] for r in store.select(conn, "weakness_patterns")}
    assert "wp_bilinear_io_relations" not in ids and "wp_low_rank_central_map" not in ids
    assert "wp_overdetermined" in ids and res["leaks"] == []


def test_worker_run_records_reads(conn, paper, tmp_path):
    q = _question(conn, kind="learn")
    prov = FakeProvider([
        FakeProvider.call("read_source", {"source_id": paper, "level": "outline", "section": "", "purpose": ""}),
        FakeProvider.final(_wb(reading_notes=[{
            "source_id": paper, "prior": "p", "decision": "d", "confirmed": "c", "surprises": "s", "hardness": "h",
            "structure_exploited": "x", "limits": "l", "ideas": [], "weakness_patterns": [], "unit_ids": []}])),
    ])
    out = run_question(conn, _charter(), prov, q, tmp_path)
    assert out.status == "done" and len(out.created["notes"]) == 1
    run = store.get(conn, "runs", out.run_id)
    assert run["context_ids"]["reads"][0]["level"] == "outline"


def test_migration_from_v2_keeps_questions_and_references(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "SCHEMA_VERSION", 2)
    old = db.connect(path)
    parent = store.insert(old, "questions", kind="survey", lane="harvest", title="P")
    child = store.insert(old, "questions", kind="learn", lane="harvest", title="C", parent_id=parent,
                         created_by_run="run_1")
    fid = store.insert(old, "findings", question_id=child, kind="fact", claim="c", evidence_kind="reasoning")
    old.close()
    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    conn = db.connect(path)
    assert db.current_version(conn) == 3
    assert store.get(conn, "findings", fid)["question_id"] == child          # not nulled by the table rebuild
    assert store.get(conn, "questions", child)["parent_id"] == parent
    assert store.get(conn, "questions", parent)["origin"] == "charter"
    assert store.get(conn, "questions", child)["origin"] == "prior"
    store.insert(conn, "questions", kind="seed", lane="harvest", title="S")  # the new kind is accepted
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
