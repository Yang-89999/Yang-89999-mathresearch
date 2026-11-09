"""The retrodiction harness: submitted attacks, hidden knowledge, and the pass check."""

import pytest
import yaml

from mathresearch import db, retro, store
from mathresearch.cli import main
from mathresearch.lab.script_attack import evaluate_script_attack, instance_to_json
from mathresearch.lab import get_family
from mathresearch.tools import registry

EXHAUSTIVE = r"""
import itertools, json
d = json.load(open("instance.json"))
n, p = d["n"], d["p"]
def ev(poly, x):
    s = 0
    for m, c in poly:
        t = c
        for i in m:
            t *= x[i]
        s += t
    return s % p
for x in itertools.product(range(p), repeat=n):
    if all(ev(f, x) == 0 for f in d["polys"]):
        print("RESULT:", json.dumps({"solution": list(x), "work": p ** n}))
        break
"""

WRONG = r"""
import json
d = json.load(open("instance.json"))
print("RESULT:", json.dumps({"solution": [0] * d["n"]}))
"""

# A self-contained rediscovery of Patarin's attack, as a worker would submit it: collect the
# bilinear relations satisfied by (x, P(x)) pairs, specialise to the target, solve.
PATARIN = r"""
import json, random, itertools
d = json.load(open("instance.json"))
n, p = d["n"], d["p"]
polys = [[(tuple(m), c) for m, c in poly] for poly in d["polys"]]

def ev(poly, x):
    s = 0
    for m, c in poly:
        t = c
        for i in m:
            t *= x[i]
        s += t
    return s % p

def P(x):
    return [ev(f, x) for f in polys]

def rref(rows):
    rows = [r[:] for r in rows]
    piv, r = [], 0
    for c in range(len(rows[0])):
        pr = next((i for i in range(r, len(rows)) if rows[i][c]), None)
        if pr is None:
            continue
        rows[r], rows[pr] = rows[pr], rows[r]
        inv = pow(rows[r][c], -1, p)
        rows[r] = [(v * inv) % p for v in rows[r]]
        for i in range(len(rows)):
            if i != r and rows[i][c]:
                f = rows[i][c]
                rows[i] = [(a - f * b) % p for a, b in zip(rows[i], rows[r])]
        piv.append(c)
        r += 1
        if r == len(rows):
            break
    return rows[:r], piv

N = n * n + 2 * n + 1
rng = random.Random(7)
rows = []
for _ in range(N + 12):
    x = [rng.randrange(p) for _ in range(n)]
    y = P(x)
    rows.append([(x[i] * y[j]) % p for i in range(n) for j in range(n)] + x + y + [1])
R, piv = rref(rows)
rels = []
for fc in [c for c in range(N) if c not in piv]:
    v = [0] * N
    v[fc] = 1
    for i, pc in enumerate(piv):
        v[pc] = (-R[i][fc]) % p
    rels.append(v)
# the published system vanishes at the wanted x, i.e. the target is y = 0: relations become linear in x
aug = [v[n * n:n * n + n] + [(-v[-1]) % p] for v in rels]
R2, piv2 = rref(aug) if aug else ([], [])
sol = None
if aug and n not in piv2:
    x0 = [0] * n
    for i, pc in enumerate(piv2):
        x0[pc] = R2[i][n]
    free = [c for c in range(n) if c not in piv2]
    basis = []
    for fc in free:
        v = [0] * n
        v[fc] = 1
        for i, pc in enumerate(piv2):
            v[pc] = (-R2[i][fc]) % p
        basis.append(v)
    if len(basis) <= 4:
        for coeffs in itertools.product(range(p), repeat=len(basis)):
            x = [(x0[i] + sum(k * b[i] for k, b in zip(coeffs, basis))) % p for i in range(n)]
            if all(v == 0 for v in P(x)):
                sol = x
                break
print("RESULT:", json.dumps({"solution": sol, "work": n ** 6, "relations": len(rels)}))
"""


def _ctx(conn, tmp_path, hidden=(), problem_id=None):
    qid = store.insert(conn, "questions", kind="attack", lane="deepen", status="open", title="Attack",
                       problem_id=problem_id)
    return registry.ToolContext(conn=conn, workspace=tmp_path, question=store.get(conn, "questions", qid),
                                run_id="run_test", hidden_attacks=frozenset(hidden))


# -- submitted attacks -----------------------------------------------------------------

def test_instance_json_is_public_only():
    inst = get_family("matsumoto_imai").generate(7, seed=1)
    data = instance_to_json(inst)
    assert data["n"] == 7 and data["p"] == 2 and "modulus" not in data["params"]
    assert "S" not in str(data) and len(data["polys"]) == 7 and data["polys"][0][0][0] in ([], [0], [0, 0], [0, 1])


def test_exhaustive_script_is_verified_but_not_polynomial(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    pid = store.insert(conn, "problems", name="toy", status="not_trivially_broken")
    res = evaluate_script_attack(conn, name="brute", code=EXHAUSTIVE, family_name="mq", family_kwargs={"p": 5},
                                 sizes=[3, 4, 5], time_limit=60, problem_id=pid, workspace=tmp_path)
    assert res["verdict"] == "broken_at_tested_sizes" and res["curve"]["growth"] == "exponential-like"
    assert all(p["success"] for p in res["curve"]["points"])
    assert res["attack_id"] and (tmp_path / "attacks" / "brute.py").read_text() == EXHAUSTIVE
    problem = store.get(conn, "problems", pid)
    assert problem["status"] == "not_trivially_broken"  # exponential success does not break it
    assert problem["best_attacks"][0]["attack_id"] == res["attack_id"]
    exp = store.get(conn, "experiments", res["experiment_id"])
    assert exp["params"]["submitted"] and exp["code"] == EXHAUSTIVE
    assert store.get(conn, "findings", res["finding_id"])["kind"] == "experimental"


def test_wrong_and_self_referencing_scripts(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    res = evaluate_script_attack(conn, name="wrong", code=WRONG, family_name="mq", family_kwargs={"p": 5},
                                 sizes=[3, 4], time_limit=20, workspace=tmp_path)
    assert res["verdict"] == "failed" and res["attack_id"] is None
    assert store.get(conn, "findings", res["finding_id"])["kind"] == "negative"
    assert "does not verify" in res["curve"]["points"][0]["notes"]
    with pytest.raises(ValueError):
        evaluate_script_attack(conn, name="cheat", code="from mathresearch.lab.attacks import ATTACKS",
                               family_name="mq", sizes=[3], workspace=tmp_path)


# -- scenario setup hides the answer ----------------------------------------------------

def test_setup_seeds_pre_1995_knowledge_without_the_answer(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    sc = retro.get_scenario("patarin_1995")
    info = retro.setup(conn, tmp_path, sc)
    assert info["leaks"] == [] and info["units"] >= 12 and info["sources"] == 4
    charter = yaml.safe_load((tmp_path / "charter.yaml").read_text())
    assert charter["hidden_attacks"] == ["patarin_linearization_equations"] and charter["catalog_max_year"] == 1994

    attacks = store.select(conn, "attacks")
    names = {a["id"] for a in attacks}
    assert "atk_patarin_linearization_equations" not in names and "atk_xl" not in names
    assert "atk_linearization" in names and "atk_exhaustive_search" in names  # kept as folklore
    assert all("Patarin" not in str(a) for a in attacks)
    assert store.get(conn, "problems", "prob_known_matsumoto_imai") is None
    target = store.get(conn, "problems", "prob_retro_cstar")
    assert target["status"] == "well_defined" and target["generator_path"] == "matsumoto_imai"
    mq = store.get(conn, "problems", "prob_known_mq")
    assert all("Patarin" not in r for r in mq["refs"])
    q = store.select(conn, "questions")[0]
    assert q["kind"] == "attack" and q["problem_id"] == "prob_retro_cstar"
    assert store.search_units(conn, "Frobenius") and not store.search_units(conn, "bilinear")

    with pytest.raises(RuntimeError):
        retro.setup(conn, tmp_path, sc)  # not on a used workspace

    # the worker's tools do not expose the hidden attack
    ctx = _ctx(conn, tmp_path, hidden=charter["hidden_attacks"], problem_id="prob_retro_cstar")
    text, _ = registry.execute("list_catalog", {"kind": "attacks"}, ctx)
    assert "patarin" not in text.lower()
    text, _ = registry.execute("run_attack", {"family": "matsumoto_imai", "attack": "patarin_linearization_equations",
                                              "sizes": "7"}, ctx)
    assert "unknown attack" in text
    text, _ = registry.execute("run_attack", {"family": "matsumoto_imai", "attack": "all", "sizes": "5,7",
                                              "seconds_per_size": 5}, ctx)
    assert "exhaustive_search" in text and "patarin" not in text.lower()
    text, _ = registry.execute("ladder_evaluate", {"problem_id": "prob_retro_cstar", "sizes": "5,7",
                                                   "seconds_per_size": 5}, ctx)
    assert "patarin" not in text.lower() and store.get(conn, "problems", "prob_retro_cstar")["status"] != "broken"

    assert not retro.check(conn, sc)["passed"]


def test_check_passes_when_the_attack_is_rediscovered(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    sc = retro.get_scenario("patarin_1995")
    retro.setup(conn, tmp_path, sc)
    ctx = _ctx(conn, tmp_path, hidden=sc.hidden_attacks, problem_id="prob_retro_cstar")
    text, err = registry.execute("sample_instance", {"family": "matsumoto_imai", "size": 7}, ctx)
    assert not err and "instance.json" in text and '"polys"' in text

    text, err = registry.execute("submit_attack", {
        "name": "input_output_relations", "code": PATARIN, "family": "matsumoto_imai", "sizes": "7,9,11,13",
        "seconds_per_size": 120, "description": "linear relations between x and P(x) that survive S and T"}, ctx)
    assert not err and "verdict: broken" in text and "registered attack atk_" in text, text

    result = retro.check(conn, sc)
    assert result["passed"] and result["passing_submissions"][0]["solved"] == [7, 9, 11, 13]
    assert store.get(conn, "problems", "prob_retro_cstar")["status"] == "broken"
    assert "PASSED" in retro.render_check(result)
    assert ctx.experiment_ids  # the run records the sweep


def test_cli_retro(tmp_path, capsys):
    assert main(["retro", "list"]) == 0
    assert "patarin_1995" in capsys.readouterr().out
    assert main(["--dir", str(tmp_path), "retro", "init"]) == 0
    out = capsys.readouterr().out
    assert "opening question q_" in out
    assert main(["--dir", str(tmp_path), "retro", "check"]) == 1
    assert "not yet" in capsys.readouterr().out
    assert main(["--dir", str(tmp_path), "retro", "init"]) == 1  # refuses a used workspace
