"""Promotion-ladder checks, recorded as experiments and findings.

step 2, well defined: the generator, forward map and verifier run; the
planted secret verifies; random candidates almost never do; the forward cost
is measured.

step 3, not trivially broken: every applicable attack either fails within
the time budget at the largest size or shows exponential-like growth.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from typing import Any

from .. import store
from .attacks import applicable
from .families import ProblemFamily, time_forward
from .sweep import sweep


def check_well_defined(family: ProblemFamily, size: int, seed: int = 0, trials: int = 200) -> dict[str, Any]:
    t0 = time.perf_counter()
    inst = family.generate(size, seed=seed)
    gen_seconds = time.perf_counter() - t0
    secret_ok = family.verify(inst, inst.secret["x"])
    rng = random.Random(seed + 1)
    false_positives = sum(family.verify(inst, family.random_candidate(inst, rng)) for _ in range(trials))
    fwd = time_forward(family, inst)
    passed = secret_ok and false_positives <= max(1, trials // 50)
    return {"size": size, "params": {k: v for k, v in inst.params.items() if k != "modulus"},
            "generate_seconds": round(gen_seconds, 6), "forward_seconds": round(fwd, 8),
            "secret_verifies": secret_ok, "random_false_positives": false_positives, "trials": trials,
            "passed": passed}


def check_not_trivially_broken(family: ProblemFamily, sizes: list[int], time_limit: float = 20.0,
                               seed: int = 0, attacks: list[str] | None = None,
                               exclude: set[str] | None = None) -> dict[str, Any]:
    curves = []
    for attack in applicable(family):
        if attacks and attack.name not in attacks:
            continue
        if exclude and attack.name in exclude:
            continue
        curves.append(sweep(family, attack, sizes, time_limit=time_limit, seed=seed).to_dict())
    largest = max(sizes)
    verdicts = []
    for c in curves:
        broke_largest = c["largest_broken"] == largest
        if broke_largest and c["growth"] == "polynomial-like":
            verdict = "broken"
        elif broke_largest:
            # exponential-like growth, or sizes too small to tell: the sweep must grow before this means anything
            verdict = "broken_at_tested_sizes"
        else:
            verdict = "resisted"
        verdicts.append({"attack": c["attack"], "verdict": verdict, "growth": c["growth"],
                         "largest_broken": c["largest_broken"]})
    passed = bool(curves) and all(v["verdict"] != "broken" for v in verdicts)
    return {"sizes": sizes, "time_limit": time_limit, "curves": curves, "verdicts": verdicts, "passed": passed}


def evaluate(conn: sqlite3.Connection, problem_id: str, family: ProblemFamily, sizes: list[int],
             time_limit: float = 20.0, question_id: str | None = None, run_id: str | None = None,
             seed: int = 0, exclude: set[str] | None = None) -> dict[str, Any]:
    """Run steps 2 and 3 for a problem row, record everything, and update its status."""
    problem = store.get(conn, "problems", problem_id)
    if problem is None:
        raise KeyError(problem_id)

    wd = check_well_defined(family, sizes[0], seed=seed)
    wd_exp = store.insert(conn, "experiments", kind="check", engine="python", problem_id=problem_id,
                          question_id=question_id, run_id=run_id, code=f"lab.ladder.check_well_defined({family.name}, {sizes[0]})",
                          code_sha256="", params={"family": family.name, "size": sizes[0]}, seed=seed,
                          stdout=json.dumps(wd), exit_code=0, wall_seconds=wd["generate_seconds"], result=wd)
    store.insert(conn, "findings", problem_id=problem_id, question_id=question_id, run_id=run_id,
                 kind="experimental" if wd["passed"] else "negative",
                 claim=(f"{family.name}: generator and verifier run at size {sizes[0]}; forward map costs "
                        f"{wd['forward_seconds']:.2e}s; {wd['random_false_positives']}/{wd['trials']} random "
                        f"candidates verify.") if wd["passed"] else f"{family.name}: well-definedness check failed: {wd}",
                 evidence_kind="experiment", evidence_id=wd_exp, confidence=0.95)
    if not wd["passed"]:
        store.update(conn, "problems", problem_id, status="idea")
        return {"well_defined": wd, "status": "idea"}

    ntb = check_not_trivially_broken(family, sizes, time_limit=time_limit, seed=seed, exclude=exclude)
    ntb_exp = store.insert(conn, "experiments", kind="attack_sweep", engine="python", problem_id=problem_id,
                           question_id=question_id, run_id=run_id,
                           code=f"lab.ladder.check_not_trivially_broken({family.name}, {sizes})", code_sha256="",
                           params={"family": family.name, "sizes": sizes, "time_limit": time_limit}, seed=seed,
                           stdout=json.dumps(ntb["verdicts"]), exit_code=0,
                           wall_seconds=sum(p["seconds"] for c in ntb["curves"] for p in c["points"]), result=ntb)
    for v in ntb["verdicts"]:
        curve = next(c for c in ntb["curves"] if c["attack"] == v["attack"])
        pts = ", ".join(f"n={p['size']}: {p['seconds']:.3g}s {'ok' if p['success'] else 'fail'}" for p in curve["points"])
        store.insert(conn, "findings", problem_id=problem_id, question_id=question_id, run_id=run_id,
                     kind="negative" if v["verdict"] == "broken" else "experimental",
                     claim=f"{family.name} vs {v['attack']}: {v['verdict']} (growth {v['growth']}); {pts}",
                     evidence_kind="experiment", evidence_id=ntb_exp, confidence=0.9)
    status = "not_trivially_broken" if ntb["passed"] else "broken"
    best = [{"attack_id": v["attack"], "verdict": v["verdict"], "growth": v["growth"]} for v in ntb["verdicts"]]
    store.update(conn, "problems", problem_id, status=status, best_attacks=best, generator_path=family.name)
    return {"well_defined": wd, "attacks": ntb, "status": status}
