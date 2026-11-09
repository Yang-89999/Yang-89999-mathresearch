"""Lab families for tensor isomorphism and alternating trilinear form equivalence, and their attacks."""

import json
import random

from mathresearch import db, store
from mathresearch.lab import get_attack, get_family, gf
from mathresearch.lab import isomorphism as iso
from mathresearch.lab.ladder import check_not_trivially_broken, check_well_defined
from mathresearch.lab.script_attack import ScriptAttack, instance_format, instance_to_json


def _det3(a, b, c, i, j, k):
    m = [[a[i], a[j], a[k]], [b[i], b[j], b[k]], [c[i], c[j], c[k]]]
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def _form(coeffs, n, p, x, y, z):
    return sum(c * _det3(x, y, z, i, j, k) for (i, j, k), c in zip(iso.triples(n), coeffs)) % p


def test_atfe_matches_its_documented_definition():
    f = get_family("atfe")
    inst = f.generate(5, seed=3)
    n, p, A = 5, 3, inst.secret["x"]
    rng = random.Random(0)
    for _ in range(20):
        x, y, z = ([rng.randrange(p) for _ in range(n)] for _ in range(3))
        ax, ay, az = (gf.matvec(A, v, p) for v in (x, y, z))
        assert _form(inst.public["psi"], n, p, x, y, z) == _form(inst.public["phi"], n, p, ax, ay, az)
    assert f.verify(inst, A) and not f.verify(inst, f.random_candidate(inst, rng))
    assert not f.verify(inst, [[0] * n for _ in range(n)]) and not f.verify(inst, "junk")


def test_tensor_iso_is_well_defined():
    f = get_family("tensor_iso", p=5)
    inst = f.generate(4, seed=1)
    P, Q, R = inst.secret["x"]
    A, B = inst.public["A"], inst.public["B"]
    a, b, c = 1, 2, 3
    assert B[a][b][c] == sum(P[a][i] * Q[b][j] * R[c][k] * A[i][j][k]
                             for i in range(4) for j in range(4) for k in range(4)) % 5
    assert f.verify(inst, [P, Q, R]) and not f.verify(inst, [Q, P, R])
    wd = check_well_defined(f, 4)
    assert wd["passed"] and wd["random_false_positives"] == 0


def test_attacks_recover_small_secrets():
    for family, n, attack in [("atfe", 5, "iso_rank_filter"), ("atfe", 4, "iso_guess_linear"),
                              ("tensor_iso", 4, "iso_rank_filter"), ("tensor_iso", 3, "iso_guess_linear")]:
        f = get_family(family)
        inst = f.generate(n, seed=n)
        res = get_attack(attack).run(f, inst, 30)
        assert res.success and f.verify(inst, res.solution), (family, n, attack, res.notes)
        assert res.work > 0


def test_rank_filter_guesses_less_than_the_baseline():
    f = get_family("atfe")
    inst = f.generate(5, seed=5)
    base = get_attack("iso_guess_linear").run(f, inst, 3)
    rank = get_attack("iso_rank_filter").run(f, inst, 30)
    assert rank.success and rank.extra["chosen_ranks"][0] < 4
    assert rank.work < base.work


def test_ladder_sweeps_the_new_families():
    res = check_not_trivially_broken(get_family("atfe"), [4, 5], time_limit=5)
    assert {v["attack"] for v in res["verdicts"]} == {"iso_guess_linear", "iso_rank_filter"}


def test_scripts_get_the_public_instance_and_must_return_the_matrices(tmp_path):
    f = get_family("atfe")
    inst = f.generate(5, seed=2)
    data = instance_to_json(inst)
    assert set(data) == {"family", "n", "p", "params", "phi", "psi"} and len(data["phi"]) == 10
    assert "psi(x, y, z) = phi(Ax, Ay, Az)" in instance_format("atfe")
    assert set(instance_to_json(get_family("tensor_iso").generate(3, seed=1))) >= {"A", "B"}
    code = """
import json
d = json.load(open('instance.json'))
n = d['n']
print('RESULT:', json.dumps({'solution': [[int(i == j) for j in range(n)] for i in range(n)], 'work': 1}))
"""
    res = ScriptAttack("identity_guess", code, workdir=tmp_path).run(f, inst, 30)
    assert not res.success and res.notes == "claimed solution does not verify", res.notes
    # the planted answer, handed over, is decoded and accepted
    cheat = code.replace("[[int(i == j) for j in range(n)] for i in range(n)]", json.dumps(inst.secret["x"]))
    assert ScriptAttack("planted", cheat, workdir=tmp_path).run(f, inst, 30).success


def test_catalog_points_the_known_problem_at_the_lab(tmp_path):
    from mathresearch import catalog
    conn = db.connect(tmp_path / "r.db")
    catalog.load(conn)
    assert store.get(conn, "problems", "prob_known_tensor_isomorphism")["generator_path"] == "atfe"
    assert store.get(conn, "attacks", "atk_iso_rank_filter")["implementation"] == "iso_rank_filter"
