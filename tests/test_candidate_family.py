"""A candidate's own family: defined by a script, checked, sampled and attacked through the lab."""

import json

from mathresearch import db, store
from mathresearch.lab import candidate_family
from mathresearch.tools import registry

# Toy candidate: recover x in GF(101)^n from A and A x (linear, so it is broken in polynomial time).
FAMILY = r'''
import json, random
t = json.load(open("task.json"))
P = 101
def mul(A, x): return [sum(a * b for a, b in zip(row, x)) % P for row in A]
if t["op"] == "generate":
    rng = random.Random(t["seed"] * 1000 + t["size"]); n = t["size"]
    A = [[rng.randrange(P) for _ in range(n)] for _ in range(n + 2)]
    x = [rng.randrange(P) for _ in range(n)]
    out = {"public": {"A": A, "y": mul(A, x)}, "secret": x}
elif t["op"] == "verify":
    pub = t["public"]
    out = {"ok": [isinstance(c, list) and len(c) == len(pub["A"][0]) and mul(pub["A"], c) == pub["y"]
                  for c in t["candidates"]]}
else:
    rng = random.Random(t["seed"]); n = len(t["public"]["A"][0])
    out = {"candidates": [[rng.randrange(P) for _ in range(n)] for _ in range(t["count"])]}
print("RESULT:", json.dumps(out))
'''

ATTACK = r'''
import json
inst = json.load(open("instance.json")); P = 101
A = [row[:] + [y] for row, y in zip(inst["public"]["A"], inst["public"]["y"])]
n = len(A[0]) - 1; work = 0; r = 0
for c in range(n):
    piv = next(i for i in range(r, len(A)) if A[i][c] % P)
    A[r], A[piv] = A[piv], A[r]
    inv = pow(A[r][c], P - 2, P); A[r] = [v * inv % P for v in A[r]]
    for i in range(len(A)):
        if i != r and A[i][c]:
            f = A[i][c]; A[i] = [(v - f * w) % P for v, w in zip(A[i], A[r])]; work += n
    r += 1
print("RESULT:", json.dumps({"solution": [A[i][n] for i in range(n)], "work": work}))
'''


def _ctx(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    pid = store.insert(conn, "problems", name="Toy linear recovery", status="idea")
    qid = store.insert(conn, "questions", kind="attack", lane="deepen", title="Attack it", problem_id=pid)
    run = store.insert(conn, "runs", question_id=qid, status="running")
    ctx = registry.ToolContext(conn=conn, workspace=tmp_path, question=store.get(conn, "questions", qid), run_id=run)
    return conn, pid, ctx


def test_candidate_gets_a_family_then_an_attack_is_registered(tmp_path):
    conn, pid, ctx = _ctx(tmp_path)
    fam = candidate_family.family_name(pid)
    text, err = registry.execute("sample_instance", {"family": fam, "size": 4}, ctx)
    assert "has no family yet" in text and "define_family" in text

    text, err = registry.execute("define_family", {"code": FAMILY, "size": 4}, ctx)
    assert not err and "well defined" in text, text
    p = store.get(conn, "problems", pid)
    assert p["status"] == "well_defined" and p["generator_path"] == fam
    assert candidate_family.code_path(tmp_path, pid).exists()

    text, _ = registry.execute("sample_instance", {"family": fam, "size": 4, "seed": 3}, ctx)
    sample = json.loads(text.split("withheld):\n", 1)[1])
    assert set(sample) == {"family", "size", "public"} and "secret" not in text.split("withheld):")[1]

    text, _ = registry.execute("submit_attack", {"name": "gauss", "code": ATTACK, "family": fam,
                                                 "sizes": "4,6,8,10", "seconds_per_size": 20}, ctx)
    assert "solved" in text and "registered attack" in text, text
    p = store.get(conn, "problems", pid)
    assert p["best_attacks"] and p["status"] in ("broken", "well_defined")

    wrong = ATTACK.replace('[A[i][n] for i in range(n)]', '[0] * n')
    text, _ = registry.execute("submit_attack", {"name": "zeros", "code": wrong, "family": fam, "sizes": "4,6"}, ctx)
    assert "verdict: failed" in text and "does not verify" in text


def test_a_verifier_that_accepts_everything_is_refused(tmp_path):
    conn, pid, ctx = _ctx(tmp_path)
    lax = FAMILY.replace('mul(pub["A"], c) == pub["y"]', 'True')
    text, _ = registry.execute("define_family", {"code": lax}, ctx)
    assert "did NOT pass" in text
    assert store.get(conn, "problems", pid)["status"] == "idea"
    assert not candidate_family.code_path(tmp_path, pid).exists()
    text, _ = registry.execute("define_family", {"code": "import mathresearch"}, ctx)
    assert "self-contained" in text


def test_questions_are_tied_to_the_candidate_they_name(tmp_path):
    from mathresearch import loop, writeback
    conn = db.connect(tmp_path / "r.db")
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="propose", lane="harvest", title="Propose"))
    run = store.insert(conn, "runs", question_id=q["id"], status="done")
    cand = {"objects": "o", "forward_map": "f", "distribution": "d", "hidden": "h", "verification": "v",
            "domains": "", "known_relatives": "", "notes": "", "sparked_by": "prior", "sparked_by_id": ""}
    data = writeback.validate({
        "summary": "s", "status": "closed", "findings": [],
        "candidate_problems": [{**cand, "name": "Punctured tensor transporter recovery"},
                               {**cand, "name": "Planted cubic polar-flag recovery"}],
        "new_questions": [
            {"kind": "attack", "lane": "deepen", "title": "Attack cubic polar flags through joint contractions",
             "body": "", "priority": 0.8, "sparked_by": "prior", "sparked_by_id": ""},
            {"kind": "attack", "lane": "deepen", "title": "Complete erased coordinates", "body": "",
             "problem_id": "new:Punctured tensor transporter recovery", "priority": 0.8, "sparked_by": "prior",
             "sparked_by_id": ""},
            {"kind": "explore", "lane": "open", "title": "Something unrelated", "body": "", "priority": 0.3,
             "sparked_by": "prior", "sparked_by_id": ""}]})
    created = writeback.apply(conn, run, q, data)
    polar, punct = (store.select(conn, "problems", "name LIKE ?", (f"{n}%",))[0]["id"] for n in ("Planted cubic", "Punctured"))
    by_title = {x["title"]: x for x in (store.get(conn, "questions", i) for i in created["questions"])}
    assert by_title["Attack cubic polar flags through joint contractions"]["problem_id"] == polar
    assert by_title["Complete erased coordinates"]["problem_id"] == punct
    assert by_title["Something unrelated"]["problem_id"] is None

    orphan = store.insert(conn, "questions", kind="attack", lane="deepen", status="open", created_by_run=run,
                          title="Attack coordinate-erased punctured tensor transporters by completion")
    assert writeback.link_orphans(conn) == [orphan]
    assert store.get(conn, "questions", orphan)["problem_id"] == punct

    assert loop.too_little_left(conn, "deepen", 0.5) is None          # no history yet
    for cost in (1.8, 2.0, 1.6):
        store.insert(conn, "runs", question_id=q["id"], lane="deepen", status="done", cost_usd=cost)
    assert "too little for a deepen run" in loop.too_little_left(conn, "deepen", 0.86)
    assert loop.too_little_left(conn, "deepen", 1.5) is None and loop.too_little_left(conn, "deepen", None) is None
