import yaml

from mathresearch import db, store
from mathresearch.charter import DEFAULT_CHARTER, Charter
from mathresearch.cli import main
from mathresearch.context import user_prompt
from mathresearch.loop import due_for_reflection, run_cycles
from mathresearch.providers.base import FakeProvider
from mathresearch.reflect import ABANDON_RUNS, STALE_RUNS, digest_for_context, latest_digest, reflect


def _charter(**over):
    data = yaml.safe_load(DEFAULT_CHARTER)
    data.update(over)
    return Charter.from_dict(data)


def _run(conn, lane, status="done", tokens=(1000, 200), cost=None):
    return store.insert(conn, "runs", lane=lane, provider="fake", model="m", status=status,
                        input_tokens=tokens[0], output_tokens=tokens[1], cost_usd=cost)


def _populate(conn, charter):
    d0, d1 = charter.domains[0], charter.domains[1]
    # runs: 6 harvest, 2 deepen, 2 open, one failed
    for _ in range(6):
        _run(conn, "harvest")
    _run(conn, "deepen"); _run(conn, "deepen", status="failed"); _run(conn, "open"); _run(conn, "open", cost=0.5)
    # problems: two candidates with the same name, one broken with a negative finding, one promoted
    p_known = store.insert(conn, "problems", name="MQ", status="known")
    p1 = store.insert(conn, "problems", name="Projection inversion", status="idea", domains=[d0])
    p_dup = store.insert(conn, "problems", name="projection  inversion!", status="idea", domains=[d0])
    p_broken = store.insert(conn, "problems", name="Toy C*", status="broken", domains=[d0])
    p_good = store.insert(conn, "problems", name="Syzygy reconstruction", status="not_trivially_broken", domains=[d1])
    store.insert(conn, "findings", kind="negative", claim="Patarin relations solve every instance in O(n^6).",
                 evidence_kind="experiment", problem_id=p_broken, confidence=0.9)
    store.insert(conn, "findings", kind="anomaly", claim="Rank drops exactly at n = 2 theta.",
                 evidence_kind="experiment", problem_id=p_good, confidence=0.7)
    # questions
    q_broken = store.insert(conn, "questions", kind="investigate", lane="deepen", status="open",
                            title="Make 'Toy C*' well defined", problem_id=p_broken, domains=[d0])
    q_dup = store.insert(conn, "questions", kind="investigate", lane="deepen", status="open",
                         title="Make 'projection inversion!' well defined", problem_id=p_dup, domains=[d0])
    q_stale = store.insert(conn, "questions", kind="survey", lane="harvest", status="open",
                           title="Stale survey", runs=STALE_RUNS, priority=0.5, domains=[d1])
    q_dead = store.insert(conn, "questions", kind="explore", lane="open", status="open",
                          title="Endless exploration", runs=ABANDON_RUNS, priority=0.5, domains=[d1])
    q_fine = store.insert(conn, "questions", kind="survey", lane="harvest", status="closed",
                          title="Done survey", runs=2, domains=[d0])
    return locals()


def test_reflect_tidies_agenda_and_renders_digest(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    charter = _charter()
    ids = _populate(conn, charter)
    r = reflect(conn, charter, tmp_path)

    # duplicates
    dup = store.get(conn, "problems", ids["p_dup"])
    assert dup["status"] == "duplicate" and dup["parent_id"] == ids["p1"]
    # questions on dead problems abandoned (broken and the fresh duplicate)
    assert store.get(conn, "questions", ids["q_broken"])["status"] == "abandoned"
    assert store.get(conn, "questions", ids["q_dup"])["status"] == "abandoned"
    # stale decays, dead abandoned
    assert store.get(conn, "questions", ids["q_stale"])["priority"] < 0.5
    dead = store.get(conn, "questions", ids["q_dead"])
    assert dead["status"] == "abandoned" and "Abandoned by reflection" in dead["summary"]
    # starved domains reseeded: every charter domain except the ones with live questions
    live_domains = {d for q in store.select(conn, "questions", "status = 'open'") for d in (q["domains"] or [])}
    assert set(charter.domains) <= live_domains
    assert any(a.startswith("reseeded starved domain") for a in r.actions)

    # digest content
    d = r.digest
    assert d.startswith("# World digest") and "Runs: 10 (1 failed)" in d and "cost $0.50" in d
    assert "harvest 0.60/0.60" in d
    assert "Syzygy reconstruction — not_trivially_broken" in d and "Projection inversion — idea" in d
    assert "## Broken" in d and "Patarin relations solve every instance" in d
    assert "(anomaly) Rank drops exactly" in d
    assert "## Reflection actions" in d and (tmp_path / "digest.md").read_text() == d

    stored = latest_digest(conn)
    assert stored["id"] == r.digest_id and stored["run_count"] == 10 and stored["actions"] == r.actions
    assert stored["stats"]["problems"]["by_status"]["duplicate"] == 1

    # workers see the digest without the housekeeping log
    ctx = digest_for_context(conn)
    assert "## Reflection actions" not in ctx and "Syzygy reconstruction" in ctx
    q = store.get(conn, "questions", ids["q_stale"])
    assert "# State of the research (latest digest)" in user_prompt(conn, q)


def test_reflect_on_empty_workspace_is_quiet(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    r = reflect(conn, _charter())
    assert r.actions == [] and "none alive yet" in r.digest and db.table_counts(conn)["digests"] == 1


def test_loop_reflects_every_n_runs(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    charter = _charter(reflect_every_runs=2)
    wb = {"summary": "ok", "status": "open", "findings": [], "new_questions": [], "candidate_problems": []}
    provider = FakeProvider([FakeProvider.final(wb) for _ in range(5)])
    seen = []
    run_cycles(conn, charter, provider, tmp_path, cycles=5, seed=3, on_reflection=seen.append)
    assert len(seen) == 2 and db.table_counts(conn)["digests"] == 2
    assert [d["run_count"] for d in store.select(conn, "digests", order="rowid")] == [2, 4]
    assert not due_for_reflection(conn, charter)
    assert not due_for_reflection(conn, _charter(reflect_every_runs=0))


def test_cli_reflect(tmp_path, capsys):
    main(["--dir", str(tmp_path), "init"])
    capsys.readouterr()
    assert main(["--dir", str(tmp_path), "reflect"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("# World digest") and "digest dg_" in err
    assert (tmp_path / "digest.md").exists()
