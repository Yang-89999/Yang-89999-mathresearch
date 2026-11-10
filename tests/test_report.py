"""Readable reports of runs, candidates and the agenda."""

from mathresearch import db, report, store
from mathresearch.cli import main


def _world(conn):
    qid = store.insert(conn, "questions", kind="learn", lane="harvest", status="open", title="Read about quivers",
                       priority=0.7, origin="charter")
    rid = store.insert(conn, "runs", question_id=qid, lane="harvest", provider="openai", model="gpt-6-astra",
                       status="done", input_tokens=1000, output_tokens=200, cost_usd=0.02,
                       result={"summary": "Quiver Grassmannians give a planted-subrepresentation problem."})
    store.insert(conn, "findings", question_id=qid, run_id=rid, kind="fact", claim="Claim one about quivers.",
                 evidence_kind="reasoning", confidence=0.6)
    sid = store.insert(conn, "sources", kind="paper", title="A paper on quivers")
    store.insert(conn, "reading_notes", question_id=qid, run_id=rid, source_id=sid, decision="whether to pursue it",
                 surprises="the moduli are smooth")
    store.insert(conn, "questions", kind="attack", lane="deepen", status="open", title="Attack the planted problem",
                 created_by_run=rid, priority=0.9, origin="reference")
    store.insert(conn, "problems", name="Planted subrepresentation", status="well_defined", created_by_run=rid,
                 forward_map="embed a random subrepresentation", hidden="the subrepresentation")
    store.insert(conn, "problems", name="Known MQ", status="known")
    store.insert(conn, "problems", name="Dead idea", status="broken")
    return rid


def test_runs_report_shows_everything_a_run_produced(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    rid = _world(conn)
    text = report.runs(conn)
    assert "## [done] Read about quivers" in text and rid in text and "$0.020" in text
    for piece in ("planted-subrepresentation problem", "Claim one about quivers", "fact (60%)",
                  "A paper on quivers", "the moduli are smooth", "Attack the planted problem",
                  "Planted subrepresentation (well_defined)"):
        assert piece in text, piece
    assert report.runs(db.connect(":memory:")).startswith("No runs yet")


def test_problems_and_questions_reports(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    _world(conn)
    text = report.problems(conn)
    assert "Planted subrepresentation" in text and "Known MQ" not in text and "] Dead idea  (" not in text
    assert "# Settled candidates (1)" in text and "- [broken] Dead idea" in text     # one line, with the reason
    assert "## [broken] Dead idea" in report.problems(conn, include_dead=True)
    q = report.questions(conn)
    assert q.index("Attack the planted problem") < q.index("Read about quivers") and "from reference" in q


def test_cli_report(tmp_path, capsys):
    main(["--dir", str(tmp_path), "init"])
    _world(db.connect(tmp_path / "research.db"))
    capsys.readouterr()
    assert main(["--dir", str(tmp_path), "report", "runs", "--last", "1"]) == 0
    assert "Claim one about quivers" in capsys.readouterr().out
    assert main(["--dir", str(tmp_path), "report", "problems"]) == 0
    assert "Planted subrepresentation" in capsys.readouterr().out
