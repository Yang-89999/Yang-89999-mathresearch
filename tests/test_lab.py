import math
import random

import pytest

from mathresearch import catalog, db, store
from mathresearch.lab import ATTACKS, get_attack, get_family, sweep
from mathresearch.lab import gf
from mathresearch.lab.families import MQ, MatsumotoImai, evaluate, monomials
from mathresearch.lab.ladder import check_not_trivially_broken, check_well_defined, evaluate as ladder_evaluate


# -- field arithmetic ----------------------------------------------------------

def test_rref_and_solve_over_gf7():
    A = [[2, 1, 0], [1, 3, 1], [0, 1, 4]]
    x = [3, 5, 6]
    b = gf.matvec(A, x, 7)
    assert gf.solve_linear(A, b, 7) == x
    assert gf.rank(A, 7) == 3
    assert gf.solve_linear([[1, 1], [2, 2]], [1, 1], 7) is None  # inconsistent
    assert gf.is_prime(31) and not gf.is_prime(33)


# -- families ------------------------------------------------------------------

def test_mq_plants_a_zero_and_rejects_random_points():
    fam = MQ(p=7)
    inst = fam.generate(6, seed=1)
    assert fam.verify(inst, inst.secret["x"])
    assert fam.forward(inst, inst.secret["x"]) == [0] * 6
    rng = random.Random(2)
    assert sum(fam.verify(inst, fam.random_candidate(inst, rng)) for _ in range(100)) <= 2
    assert len(monomials(3)) == 6 + 3 + 1


def test_mq_generation_is_deterministic_in_seed():
    a, b = MQ(p=5).generate(4, seed=9), MQ(p=5).generate(4, seed=9)
    assert a.public == b.public and a.secret == b.secret


def test_matsumoto_imai_public_map_matches_composition():
    fam = MatsumotoImai(theta=1)
    inst = fam.generate(7, seed=3)
    assert fam.verify(inst, inst.secret["x"])
    # the public polynomials are the secret composition shifted by y: check on random points
    S, s, T, t, y = inst.secret["S"], inst.secret["s"], inst.secret["T"], inst.secret["t"], inst.secret["y"]
    mod = inst.params["modulus"]
    rng = random.Random(4)
    for _ in range(20):
        x = [rng.randrange(2) for _ in range(7)]
        u = [(v + c) % 2 for v, c in zip(gf.matvec(S, x, 2), s)]
        w = fam._pow(u, 1 + 2 ** 1, mod)
        expect = [(v + c - yy) % 2 for v, c, yy in zip(gf.matvec(T, w, 2), t, y)]
        assert fam.forward(inst, x) == expect


def test_matsumoto_imai_rejects_bad_parameters():
    with pytest.raises(ValueError):
        MatsumotoImai(p=3)
    with pytest.raises(ValueError):
        MatsumotoImai(theta=1).generate(6, seed=0)  # gcd(3, 63) != 1


def test_get_family_and_attack_registry():
    assert get_family("mq", p=11).p == 11
    assert get_attack("linearization").name == "linearization"
    with pytest.raises(KeyError):
        get_family("nope")


# -- attacks -------------------------------------------------------------------

def test_exhaustive_search_breaks_tiny_instances_and_reports_search_space():
    fam = MQ(p=5)
    inst = fam.generate(3, seed=1)
    res = ATTACKS["exhaustive_search"].run(fam, inst, 10)
    assert res.success and fam.verify(inst, res.solution) and res.work == 5 ** 3


def test_exhaustive_search_times_out_cleanly():
    fam = MQ(p=31)
    inst = fam.generate(8, seed=1)
    res = ATTACKS["exhaustive_search"].run(fam, inst, 0.05)
    assert not res.success and res.timed_out


def test_linearization_needs_enough_equations():
    over = MQ(p=7, ratio=4.0)  # n=4 -> m=16 >= 14 unknown monomials
    inst = over.generate(4, seed=4)
    res = ATTACKS["linearization"].run(over, inst, 10)
    assert res.success and over.verify(inst, res.solution)
    assert res.extra["rank"] == res.extra["unknowns"] == 14

    square = MQ(p=7)
    res = ATTACKS["linearization"].run(square, square.generate(6, seed=1), 10)
    assert not res.success and res.extra["rank"] < res.extra["unknowns"]


def test_patarin_breaks_matsumoto_imai_but_finds_nothing_in_random_mq():
    mi = MatsumotoImai(theta=1)
    attack = ATTACKS["patarin_linearization_equations"]
    for n in (11, 13):
        inst = mi.generate(n, seed=n)
        res = attack.run(mi, inst, 120)
        assert res.success and mi.verify(inst, res.solution)
        assert res.extra["relations"] >= n  # the C* structure yields at least n bilinear relations
    rnd = MQ(p=2)
    res = attack.run(rnd, rnd.generate(11, seed=11), 120)
    assert not res.success and res.extra["relations"] == 0


# -- sweeps and ladder ---------------------------------------------------------

def test_sweep_classifies_exhaustive_search_as_exponential():
    fam = MQ(p=5)
    curve = sweep(fam, ATTACKS["exhaustive_search"], [3, 4, 5, 6], time_limit=30)
    assert curve.growth == "exponential-like" and curve.largest_broken == 6
    assert abs(curve.exp_slope - math.log(5)) < 1e-3  # work = 5^n exactly; slope is rounded to 4 places


def test_sweep_classifies_patarin_as_polynomial():
    mi = MatsumotoImai(theta=1)
    curve = sweep(mi, ATTACKS["patarin_linearization_equations"], [7, 9, 11, 13], time_limit=120)
    assert curve.growth == "polynomial-like" and curve.largest_broken == 13


def test_well_defined_check():
    wd = check_well_defined(MQ(p=7), 5)
    assert wd["passed"] and wd["secret_verifies"] and wd["forward_seconds"] > 0


def test_ladder_marks_matsumoto_imai_broken_and_random_mq_surviving():
    mi = check_not_trivially_broken(MatsumotoImai(theta=1), [7, 9, 11, 13], time_limit=120)
    by_name = {v["attack"]: v for v in mi["verdicts"]}
    assert by_name["patarin_linearization_equations"]["verdict"] == "broken"
    assert not mi["passed"]

    mq = check_not_trivially_broken(MQ(p=7), [3, 4, 5, 6], time_limit=30)
    by_name = {v["attack"]: v for v in mq["verdicts"]}
    assert by_name["exhaustive_search"]["verdict"] == "broken_at_tested_sizes"
    assert by_name["linearization"]["verdict"] == "resisted"
    assert by_name["patarin_linearization_equations"]["verdict"] == "resisted"
    assert mq["passed"]


def test_ladder_evaluate_records_findings_and_status(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    pid = store.insert(conn, "problems", name="C* test", status="idea")
    result = ladder_evaluate(conn, pid, MatsumotoImai(theta=1), [7, 9, 11, 13], time_limit=120)
    assert result["status"] == "broken"
    problem = store.get(conn, "problems", pid)
    assert problem["status"] == "broken" and problem["generator_path"] == "matsumoto_imai"
    findings = store.select(conn, "findings", "problem_id = ?", (pid,))
    kinds = {f["kind"] for f in findings}
    assert "negative" in kinds and all(f["evidence_kind"] == "experiment" for f in findings)
    assert db.table_counts(conn)["experiments"] == 2


# -- catalog -------------------------------------------------------------------

def test_catalog_loads_and_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    first = catalog.load(conn)
    assert first["problems"] >= 15 and first["attacks"] >= 20
    second = catalog.load(conn)
    assert second == {"problems": 0, "attacks": 0}
    mq = store.get(conn, "problems", "prob_known_mq")
    assert mq["status"] == "known" and mq["generator_path"] == "mq" and isinstance(mq["best_attacks"], list)
    patarin = store.get(conn, "attacks", "atk_patarin_linearization_equations")
    assert patarin["implementation"] == "patarin_linearization_equations"
    # every implementation named in the catalog exists in the lab
    for a in store.select(conn, "attacks"):
        if a.get("implementation"):
            assert a["implementation"] in ATTACKS
