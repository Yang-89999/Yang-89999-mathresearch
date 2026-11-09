"""Attacks written by workers, as scripts.

A worker that believes it has an algorithm submits code. The lab runs it in
the sandbox against fresh instances it generates itself (the script never
sees the secret), checks every claimed solution with the family's verifier,
sweeps sizes for a cost curve, and records the result. Only this path can
turn "I think this breaks it" into a finding.

Script contract: the working directory holds ``instance.json``; the script
must print a final line ``RESULT: {"solution": [...], "work": <int>}`` where
``work`` is an optional deterministic operation count used for the growth fit.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import store
from ..tools.sage_runner import parse_result, run_script
from .attacks import AttackResult
from .families import FAMILIES, Instance, ProblemFamily, get_family
from .sweep import sweep

INSTANCE_FORMAT = """\
instance.json = {
  "family": "mq" | "matsumoto_imai",   # tensor_iso and atfe have their own format (see sample_instance)
  "n": <number of variables>, "p": <field size (prime; 2 for matsumoto_imai)>,
  "params": {...family parameters, e.g. "m" equations, "theta"; never the secret...},
  "polys": [ poly_1, ..., poly_m ]      # the public system; a solution x in GF(p)^n makes every poly_k(x) == 0
}
poly_k = [[monomial, coefficient], ...] with monomial [] (constant), [i] (x_i) or [i, j] with i <= j (x_i x_j).
Your script must end with:  print("RESULT:", json.dumps({"solution": [x_1, ..., x_n], "work": <int or 0>}))
Sampling more pairs (x, P(x)) is allowed and cheap: evaluate the public polys on random x yourself.
"""


def instance_to_json(instance: Instance) -> dict[str, Any]:
    """Public view of an instance: what an attacker legitimately has."""
    params = {k: v for k, v in instance.params.items() if k not in ("modulus",)}
    data = {"family": instance.family, "n": instance.params["n"], "p": instance.params["p"], "params": params}
    if "polys" in instance.public:
        data["polys"] = [[[list(m), c] for m, c in poly.items()] for poly in instance.public["polys"]]
    else:
        data.update(FAMILIES[instance.family]().public_json(instance))
    return data


def instance_format(family_name: str) -> str:
    """What instance.json holds for a family, and what the script must print."""
    from .isomorphism import INSTANCE_FORMATS  # noqa: PLC0415
    return INSTANCE_FORMATS.get(family_name, INSTANCE_FORMAT)


class ScriptAttack:
    """An Attack backed by worker-written code."""

    requires: list[str] = []
    exploits = "whatever the submitted script exploits"

    def __init__(self, name: str, code: str, engine: str = "python", memory_mb: int = 2048,
                 workdir: Path | None = None):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{0,60}", name):
            raise ValueError("attack name must be an identifier-like string")
        self.name = name
        self.code = code
        self.engine = engine
        self.memory_mb = memory_mb
        self.workdir = workdir
        self.description = f"script attack {name} ({engine})"
        self.code_sha256 = hashlib.sha256(code.encode()).hexdigest()

    def applies(self, family: ProblemFamily) -> bool:
        return family.name in FAMILIES

    def run(self, family: ProblemFamily, instance: Instance, time_limit: float) -> AttackResult:
        base = Path(self.workdir) if self.workdir else Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        cwd = Path(tempfile.mkdtemp(prefix=f"{self.name}_n{instance.size}_", dir=base))
        (cwd / "instance.json").write_text(json.dumps(instance_to_json(instance)), encoding="utf-8")
        start = time.perf_counter()
        res = run_script(self.code, engine=self.engine, timeout=time_limit, memory_mb=self.memory_mb, cwd=cwd)
        seconds = time.perf_counter() - start
        data = parse_result(res.stdout)
        decode = getattr(family, "decode", _int_list)
        solution = decode(data.get("solution")) if isinstance(data, dict) else None
        work = int(data.get("work") or 0) if isinstance(data, dict) and str(data.get("work", 0)).isdigit() else 0
        ok = solution is not None and family.verify(instance, solution)
        notes = ""
        if res.timed_out:
            notes = "timed out"
        elif res.exit_code not in (0, None):
            notes = f"exit code {res.exit_code}: {res.stderr.strip()[-300:]}"
        elif data is None:
            notes = "no RESULT line in stdout"
        elif not ok:
            notes = "claimed solution does not verify" if solution is not None else "no solution returned"
        return AttackResult(self.name, bool(ok), seconds, work, timed_out=res.timed_out,
                            solution=solution if ok else None, notes=notes,
                            extra={"exit_code": res.exit_code, "stdout_tail": res.stdout[-400:],
                                   "stderr_tail": res.stderr[-400:]})


def _int_list(value: Any) -> list[int] | None:
    if not isinstance(value, list):
        return None
    try:
        return [int(v) for v in value]
    except (TypeError, ValueError):
        return None


def verdict_for(curve_dict: dict[str, Any], largest: int) -> str:
    broke_largest = curve_dict["largest_broken"] == largest
    if broke_largest and curve_dict["growth"] == "polynomial-like":
        return "broken"
    if broke_largest:
        return "broken_at_tested_sizes"
    if curve_dict["largest_broken"] is not None:
        return "partial"
    return "failed"


def evaluate_script_attack(conn: sqlite3.Connection, *, name: str, code: str, family_name: str,
                           sizes: list[int], time_limit: float = 30.0, engine: str = "python",
                           family_kwargs: dict[str, Any] | None = None, problem_id: str | None = None,
                           question_id: str | None = None, run_id: str | None = None,
                           description: str = "", workspace: Path | None = None, seed: int = 1000) -> dict[str, Any]:
    """Sweep a submitted attack, record experiment + finding, register it as an attack when it works,
    and demote the problem when it is broken in polynomial time."""
    if re.search(r"\bmathresearch\b", code):
        raise ValueError("submitted scripts must be self-contained: they may not import or read the mathresearch "
                         "package (that would let a script call the lab's own attacks)")
    family = get_family(family_name, **(family_kwargs or {}))
    workdir = (workspace / "experiments" / (run_id or "manual")) if workspace else None
    attack = ScriptAttack(name, code, engine=engine, workdir=workdir)
    curve = sweep(family, attack, sizes, time_limit=time_limit, seed=seed, stop_after_timeout=True)
    cd = curve.to_dict()
    verdict = verdict_for(cd, max(sizes))
    exp_id = store.insert(conn, "experiments", kind="attack_sweep", engine=engine, question_id=question_id,
                          problem_id=problem_id, run_id=run_id, code=code, code_sha256=attack.code_sha256,
                          params={"family": family.name, "family_kwargs": family_kwargs or {}, "attack": name,
                                  "sizes": sizes, "seconds": time_limit, "submitted": True}, seed=seed,
                          stdout="\n".join(p.get("stdout_tail", "") for p in curve.points)[-4000:],
                          exit_code=0, wall_seconds=sum(p["seconds"] for p in curve.points), result=cd)
    solved = [p["size"] for p in curve.points if p["success"]]
    summary = (f"Submitted attack '{name}' on {family.name}: solved sizes {solved or 'none'} of {sizes}; "
               f"growth {cd['growth']} (exp_slope={cd['exp_slope']}, poly_degree={cd['poly_degree']}); verdict {verdict}.")
    finding_kind = "experimental" if solved else "negative"
    confidence = {"broken": 0.9, "broken_at_tested_sizes": 0.6, "partial": 0.4, "failed": 0.7}[verdict]
    fnd_id = store.insert(conn, "findings", kind=finding_kind, claim=summary, evidence_kind="experiment",
                          evidence_id=exp_id, confidence=confidence, question_id=question_id,
                          problem_id=problem_id, run_id=run_id)
    attack_id = None
    if verdict in ("broken", "broken_at_tested_sizes"):
        path = None
        if workspace is not None:
            adir = workspace / "attacks"
            adir.mkdir(parents=True, exist_ok=True)
            path = adir / f"{name}.py"
            path.write_text(code, encoding="utf-8")
        attack_id = store.insert(conn, "attacks", name=name, family="submitted",
                                 exploits=description or "see script", applies_to=[problem_id] if problem_id else [],
                                 complexity=f"{cd['growth']} on sizes {sizes}",
                                 implementation=f"script:{path}" if path else "script:inline",
                                 requires=[engine] if engine != "python" else [],
                                 refs=[], notes=f"submitted in run {run_id}; experiment {exp_id}")
        if problem_id:
            problem = store.get(conn, "problems", problem_id)
            if problem:
                best = list(problem.get("best_attacks") or [])
                best.append({"attack_id": attack_id, "complexity": cd["growth"], "ref": exp_id})
                changes: dict[str, Any] = {"best_attacks": best}
                if verdict == "broken" and problem["status"] not in ("known", "broken"):
                    changes["status"] = "broken"
                store.update(conn, "problems", problem_id, **changes)
    return {"verdict": verdict, "curve": cd, "experiment_id": exp_id, "finding_id": fnd_id, "attack_id": attack_id,
            "summary": summary}
