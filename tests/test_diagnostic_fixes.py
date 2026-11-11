"""Fixes from the first diagnostic run: checks that only check, candidates held to the target and to what the
run actually read, and LaTeX sources that keep their prose."""

from mathresearch import catalog, db, ingest, reading, store, worker, writeback
import yaml

from mathresearch.charter import DEFAULT_CHARTER, Charter
from mathresearch.providers.base import FakeProvider

CAND = {
    "name": "Planted widget recovery", "objects": "widgets over GF(3)", "forward_map": "mix a planted widget",
    "distribution": "uniform", "hidden": "the widget", "verification": "recompute", "domains": "algebra",
    "known_relatives": "", "notes": "", "sparked_by": "prior", "sparked_by_id": "",
}


def _wb(**extra):
    data = {"summary": "s", "status": "open", "findings": [], "new_questions": [], "candidate_problems": []}
    data.update(extra)
    return writeback.validate(data)


def _world(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    catalog.load(conn)
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="survey", lane="harvest", title="Q"))
    run = store.insert(conn, "runs", question_id=q["id"], status="done")
    return conn, q, run


def test_candidate_from_a_source_read_only_by_its_card_is_held(tmp_path):
    conn, q, run = _world(tmp_path)
    sid = store.insert(conn, "sources", kind="paper", title="A paper", format="latex")
    store.insert(conn, "units", source_id=sid, kind="theorem", statement="Every widget is planted.")
    cand = {**CAND, "sparked_by": "reference", "sparked_by_id": sid}
    created = writeback.apply(conn, run, q, _wb(candidate_problems=[cand]),
                              reads=[{"source_id": sid, "level": "card"}])
    assert not created["problems"] and "opened only that source's card" in created["warnings"][0]
    held = store.get(conn, "questions", created["questions"][0])
    assert held["title"] == "Idea held back: Planted widget recovery" and held["origin_id"] == sid
    assert "forward_map: mix a planted widget" in held["body"]
    created = writeback.apply(conn, run, q, _wb(candidate_problems=[cand]),
                              reads=[{"source_id": sid, "level": "section", "section": "1"}])
    assert len(created["problems"]) == 1


def test_candidates_beyond_the_room_are_held(tmp_path):
    conn, q, run = _world(tmp_path)
    two = [CAND, {**CAND, "name": "Second widget problem"}]
    created = writeback.apply(conn, run, q, _wb(candidate_problems=two), reads=[], candidate_room=1)
    assert len(created["problems"]) == 1
    assert any("Second widget problem" in w and "enough candidates" in w for w in created["warnings"])
    created = writeback.apply(conn, run, q, _wb(candidate_problems=two))   # rules off without the arguments
    assert len(created["problems"]) == 2


def _check_question(conn, q, run):
    pid = writeback.apply(conn, run, q, _wb(candidate_problems=[CAND]))["problems"][0]
    return pid, store.select(conn, "questions", "kind = 'novelty_check' AND problem_id = ?", (pid,))[0]


def test_check_closes_with_its_verdict_and_after_two_silent_runs(tmp_path):
    conn, q, run = _world(tmp_path)
    pid, check = _check_question(conn, q, run)
    run2 = store.insert(conn, "runs", question_id=check["id"], status="done")
    writeback.apply(conn, run2, check, _wb(novelty_checks=[{
        "problem_id": pid, "relation": "no_relative_found", "known_problem_id": "",
        "argument": "Compared with every catalog problem; none shares the group action."}]))
    assert store.get(conn, "questions", check["id"])["status"] == "closed"

    pid, check = _check_question(conn, q, run)
    writeback.apply(conn, run2, check, _wb())
    check = store.get(conn, "questions", check["id"])
    assert check["status"] == "open" and check["runs"] == 1
    created = writeback.apply(conn, run2, check, _wb(summary="Still comparing with MinRank."))
    assert store.get(conn, "questions", check["id"])["status"] == "closed"
    nov = store.get(conn, "problems", pid)["novelty"]
    assert nov["verdict"] == "open" and "Still comparing with MinRank" in nov["checks"][-1]["argument"]
    assert any("closed after two runs" in w for w in created["warnings"])


def test_check_runs_get_no_lab_tools(tmp_path):
    conn, q, run = _world(tmp_path)
    pid, check = _check_question(conn, q, run)
    provider = FakeProvider([
        FakeProvider.call("run_script", {"code": "print(1)", "engine": "python"}, "c1"),
        FakeProvider.final(_wb(status="closed", novelty_checks=[{
            "problem_id": pid, "relation": "no_relative_found", "known_problem_id": "",
            "argument": "Compared with every catalog problem; none shares the group action."}])),
    ])
    charter = Charter.from_dict(yaml.safe_load(DEFAULT_CHARTER))
    worker.run_question(conn, charter, provider, check, tmp_path)
    first = provider.requests[0]
    names = {t.name for t in first.tools}
    assert "read_source" in names and not names & worker.LAB_TOOLS
    assert first.effort == worker.CHECK_EFFORT
    refused = provider.requests[1].messages[-1].content[0]
    assert refused.is_error and "not available in a known-result check" in refused.content
    assert not store.select(conn, "experiments")


def test_latex_sources_keep_their_prose(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    tex = tmp_path / "paper"
    tex.mkdir()
    sampler = "To sample an instance, draw a uniformly random cubic and plant a zero corner of dimension h. " * 4
    (tex / "main.tex").write_text(
        r"\documentclass{article}\newtheorem{theorem}{Theorem}\title{Planted cubics}\begin{document}\maketitle"
        "\n\\section{Planted independent space}\n" + sampler +
        "\n\\begin{theorem}\\label{t}Planted corners are unique.\\end{theorem}\n"
        "\\section{Attacks}\nShort.\n\\end{document}\n")
    res = ingest.ingest_latex_dir(conn, tex)
    assert "prose kept in 1 sections" in res.note
    text = reading.read_source(conn, res.source_id, "section", section="Planted independent",
                               purpose="decide whether the sampler hides coordinates after planting")
    assert "draw a uniformly random cubic" in text and "units stated here" in text
    conn.execute("DELETE FROM source_sections")
    assert ingest.relink(conn)["sections"] == 1
