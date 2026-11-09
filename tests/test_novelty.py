"""The known-result check on candidates, and the discovery trend report."""

from mathresearch import catalog, db, novelty, report, store, writeback
from mathresearch.cli import main

TRANSPORTER = {
    "name": "Arrow-mixed Kronecker transporter",
    "objects": "3-tensors over GF(q), acted on by triples of invertible matrices (tensor isomorphism style)",
    "forward_map": "apply (P, Q, R) in GL_n x GL_n x GL_n to a tensor, mode by mode",
    "distribution": "random tensor, random invertible triple", "hidden": "the triple (P, Q, R)",
    "verification": "apply and compare tensors", "domains": "multilinear algebra, group actions",
    "known_relatives": "maybe tensor isomorphism", "notes": "", "sparked_by": "prior", "sparked_by_id": "",
}


def _wb(**extra):
    data = {"summary": "s", "status": "open", "findings": [], "new_questions": [], "candidate_problems": []}
    data.update(extra)
    return writeback.validate(data)


def _setup(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    catalog.load(conn)
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="explore", lane="open", title="Q"))
    return conn, q


def test_new_candidates_get_the_closest_known_problems_and_a_separate_check(tmp_path):
    conn, q = _setup(tmp_path)
    run1 = store.insert(conn, "runs", question_id=q["id"], status="done", cost_usd=1.0)
    created = writeback.apply(conn, run1, q, _wb(candidate_problems=[TRANSPORTER]))
    pid = created["problems"][0]
    p = store.get(conn, "problems", pid)
    assert p["novelty"]["verdict"] == "unchecked"
    assert p["novelty"]["closest"][0]["id"] == "prob_known_tensor_isomorphism"
    check_q = next(x for x in store.select(conn, "questions", "problem_id = ?", (pid,)) if x["kind"] == "novelty_check")
    assert check_q["lane"] == "deepen" and "prob_known_tensor_isomorphism" in check_q["body"]

    # the proposer cannot certify its own idea new ...
    created = writeback.apply(conn, run1, q, _wb(novelty_checks=[{
        "problem_id": pid, "relation": "no_relative_found", "known_problem_id": "",
        "argument": "I checked everything I know and found nothing similar at all."}]))
    assert any("counts only from its known-result check" in w for w in created["warnings"])
    assert store.get(conn, "problems", pid)["novelty"]["verdict"] == "unchecked"
    # ... and "known" needs the known problem named
    created = writeback.apply(conn, run1, q, _wb(novelty_checks=[{
        "problem_id": pid, "relation": "equivalent", "known_problem_id": "",
        "argument": "It is the same as tensor isomorphism by renaming the modes."}]))
    assert any("must name the known problem" in w for w in created["warnings"])

    # the separate check run shows it is tensor isomorphism: a duplicate
    run2 = store.insert(conn, "runs", question_id=check_q["id"], status="done", cost_usd=0.5)
    created = writeback.apply(conn, run2, check_q, _wb(novelty_checks=[{
        "problem_id": pid, "relation": "equivalent", "known_problem_id": "prob_known_tensor_isomorphism",
        "argument": "The arrow mixing is a change of basis in each mode: (P, Q, R) acts exactly as in TI; "
                    "the identity map sends instances both ways."}]))
    assert not created["warnings"]
    p = store.get(conn, "problems", pid)
    assert p["status"] == "duplicate" and p["novelty"]["verdict"] == "known"
    assert p["reductions"][-1]["to"] == "prob_known_tensor_isomorphism"


def test_an_open_check_and_the_ladder_make_a_survivor(tmp_path):
    conn, q = _setup(tmp_path)
    run1 = store.insert(conn, "runs", question_id=q["id"], status="done", lane="open", cost_usd=2.0)
    cand = {**TRANSPORTER, "name": "Twisted cubic lattice walks", "objects": "walks on a twisted cubic curve",
            "domains": "algebraic geometry"}
    pid = writeback.apply(conn, run1, q, _wb(candidate_problems=[cand]))["problems"][0]
    check_q = next(x for x in store.select(conn, "questions", "problem_id = ?", (pid,)) if x["kind"] == "novelty_check")
    store.update(conn, "problems", pid, status="not_trivially_broken")
    run2 = store.insert(conn, "runs", question_id=check_q["id"], status="done", lane="deepen", cost_usd=1.0)
    writeback.apply(conn, run2, check_q, _wb(novelty_checks=[{
        "problem_id": pid, "relation": "no_relative_found", "known_problem_id": "",
        "argument": "Tried MQ (no quadratic structure), isogenies (no group action), lattices (no norm)."}]))
    p = store.get(conn, "problems", pid)
    assert p["status"] == "not_known" and p["novelty"]["verdict"] == "open"

    rows = report.trend_rows(conn, by="batch", batch=10)
    assert len(rows) == 1
    r = rows[0]
    assert (r["runs"], r["candidates"], r["survivors"], r["novelty_checks"]) == (2, 1, 1, 1)
    assert r["deepen_share"] == 0.5 and r["cost"] == 3.0
    text = report.trend(conn, by="batch")
    assert "1 survivors ($3.000 per survivor)" in text
    assert "known-result check: open" in report.problems(conn)


def test_promote_after_the_ladder(tmp_path):
    conn, _ = _setup(tmp_path)
    pid = store.insert(conn, "problems", name="X", status="not_trivially_broken",
                       novelty={"closest": [], "checks": [{"relation": "generalizes"}], "verdict": "open"})
    assert novelty.promote_if_open(conn, pid) and store.get(conn, "problems", pid)["status"] == "not_known"


def test_report_trend_on_the_command_line(tmp_path, capsys):
    main(["--dir", str(tmp_path), "init"])
    assert main(["--dir", str(tmp_path), "report", "trend"]) == 0
    assert "No runs yet" in capsys.readouterr().out


def test_catalog_load_opens_checks_for_older_candidates(tmp_path, capsys):
    main(["--dir", str(tmp_path), "init"])
    conn = db.connect(tmp_path / "research.db")
    old = store.insert(conn, "problems", name="Old idea", status="idea", objects="3-tensors under GL_n")
    store.insert(conn, "problems", name="Dead idea", status="broken")
    assert main(["--dir", str(tmp_path), "catalog", "load"]) == 0
    assert "known-result check for 1 earlier candidates" in capsys.readouterr().out
    assert store.get(conn, "problems", old)["novelty"]["verdict"] == "unchecked"
    assert main(["--dir", str(tmp_path), "catalog", "load"]) == 0
    assert "earlier candidates" not in capsys.readouterr().out
