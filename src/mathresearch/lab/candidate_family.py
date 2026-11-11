"""A candidate problem's own family: generator and verifier written by a worker, as one script.

The built-in families (mq, tensor_iso, ...) cover known problems. A new candidate has none, so the lab could
neither sample it nor check an attack on it: workers found attacks and had nowhere to register them. Here the
candidate carries a script. The lab stores it as data, runs it only in the sandbox (the same guard as every
experiment: no writes outside its folder, no access to the harness), and never lets an attack see the secret:
attack scripts get the public part alone, and their answer is checked by the family's verifier in a
separate process.

Script contract. The working directory holds ``task.json``; the script prints one final line
``RESULT: <json>``:

  {"op": "generate", "size": n, "seed": s}       -> {"public": <json>, "secret": <json>}
  {"op": "verify", "public": ..., "candidates": [c1, ...]}  -> {"ok": [true, false, ...]}
  {"op": "random", "public": ..., "count": k, "seed": s}    -> {"candidates": [c1, ..., ck]}

``generate`` must be deterministic in (size, seed) and sample the candidate's stated distribution. ``verify``
sees only the public part and decides each candidate on its own; it must accept the planted secret and
any other valid solution. ``random`` draws well-formed candidates uniformly, to show that the verifier
rejects them.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import store
from ..tools.sage_runner import parse_result, run_script
from .families import Instance

PREFIX = "cand:"
FAMILIES_DIR = "families"
MAX_PUBLIC_CHARS = 2_000_000

CONTRACT = """\
The family script reads task.json from its working directory and ends with print("RESULT:", json.dumps(...)):
  {"op": "generate", "size": n, "seed": s}                 -> {"public": ..., "secret": ...}
  {"op": "verify", "public": ..., "candidates": [...]}     -> {"ok": [true, false, ...]}
  {"op": "random", "public": ..., "count": k, "seed": s}   -> {"candidates": [...]}
generate is deterministic in (size, seed) and samples the stated distribution; verify sees only the public
part; random draws well-formed candidates uniformly. Self-contained: standard library, numpy, sympy.
An attack script on this family reads instance.json = {"family", "size", "public"} and ends with
print("RESULT:", json.dumps({"solution": <candidate>, "work": <int or 0>})).
"""


class FamilyError(RuntimeError):
    pass


def family_name(problem_id: str) -> str:
    return PREFIX + problem_id


def problem_id_of(name: str) -> str | None:
    """The problem id in a family name ('cand:prob_x' or a bare 'prob_x'), or None for a built-in family."""
    name = (name or "").strip()
    if name.startswith(PREFIX):
        return name[len(PREFIX):]
    return name if name.startswith("prob_") else None


def code_path(workspace: Path, problem_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_]+", problem_id):
        raise FamilyError(f"bad problem id {problem_id!r}")
    return Path(workspace) / FAMILIES_DIR / f"{problem_id}.py"


class CandidateFamily:
    """ProblemFamily backed by a candidate's script. Every call runs the script in the sandbox."""

    description = "candidate-defined family (worker-written generator and verifier)"
    domains: list[str] = []

    def __init__(self, problem_id: str, code: str, engine: str = "python", timeout: float = 60.0,
                 memory_mb: int = 2048, workdir: Path | None = None):
        self.problem_id = problem_id
        self.name = family_name(problem_id)
        self.code = code
        self.engine = engine
        self.timeout = timeout
        self.memory_mb = memory_mb
        self.workdir = workdir
        self.code_sha256 = hashlib.sha256(code.encode()).hexdigest()

    def _call(self, task: dict[str, Any]) -> dict[str, Any]:
        base = Path(self.workdir) if self.workdir else Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        cwd = Path(tempfile.mkdtemp(prefix=f"family_{task['op']}_", dir=base))
        (cwd / "task.json").write_text(json.dumps(task), encoding="utf-8")
        res = run_script(self.code, engine=self.engine, timeout=self.timeout, memory_mb=self.memory_mb, cwd=cwd)
        if res.timed_out:
            raise FamilyError(f"family script timed out on {task['op']} after {self.timeout:.0f}s")
        data = parse_result(res.stdout)
        if not isinstance(data, dict):
            raise FamilyError(f"family script gave no RESULT object on {task['op']} "
                              f"(exit code {res.exit_code}): {res.stderr.strip()[-300:]}")
        return data

    def params(self, size: int) -> dict[str, Any]:
        return {"n": size}

    def generate(self, size: int, seed: int | None = None) -> Instance:
        data = self._call({"op": "generate", "size": int(size), "seed": int(seed or 0)})
        if "public" not in data or "secret" not in data:
            raise FamilyError("generate must return {'public': ..., 'secret': ...}")
        if len(json.dumps(data["public"])) > MAX_PUBLIC_CHARS:
            raise FamilyError("the public part is too large at this size; use smaller sizes")
        return Instance(family=self.name, size=int(size), params={"n": int(size)},
                        public={"data": data["public"]}, secret={"x": data["secret"]}, seed=seed)

    def verify_many(self, instance: Instance, candidates: list[Any]) -> list[bool]:
        if not candidates:
            return []
        ok = self._call({"op": "verify", "public": instance.public["data"], "candidates": candidates}).get("ok")
        if not isinstance(ok, list) or len(ok) != len(candidates):
            raise FamilyError("verify must return {'ok': [...]} with one entry per candidate")
        return [v is True for v in ok]

    def verify(self, instance: Instance, candidate: Any) -> bool:
        try:
            return self.verify_many(instance, [candidate])[0]
        except FamilyError:
            return False

    def forward(self, instance: Instance, candidate: Any) -> Any:
        return self.verify(instance, candidate)

    def random_candidates(self, instance: Instance, count: int, seed: int = 0) -> list[Any]:
        out = self._call({"op": "random", "public": instance.public["data"], "count": count,
                          "seed": seed}).get("candidates")
        if not isinstance(out, list) or not out:
            raise FamilyError("random must return {'candidates': [...]}")
        return out

    def random_candidate(self, instance: Instance, rng: random.Random) -> Any:
        return self.random_candidates(instance, 1, seed=rng.randrange(1 << 30))[0]

    @staticmethod
    def decode(value: Any) -> Any:
        return value

    def public_json(self, instance: Instance) -> dict[str, Any]:
        return {"family": self.name, "size": instance.size, "public": instance.public["data"]}

    def check(self, size: int, seed: int = 0, trials: int = 50) -> dict[str, Any]:
        """Well-definedness at one size: the generator is deterministic, different seeds give different
        instances, the planted secret verifies, and random candidates almost never do."""
        t0 = time.perf_counter()
        inst = self.generate(size, seed=seed)
        gen_seconds = time.perf_counter() - t0
        again = self.generate(size, seed=seed)
        other = self.generate(size, seed=seed + 1)
        randoms = self.random_candidates(inst, trials, seed=seed + 1)
        t1 = time.perf_counter()
        verdicts = self.verify_many(inst, [inst.secret["x"], *randoms])
        verify_seconds = (time.perf_counter() - t1) / (1 + len(randoms))
        false_positives = sum(verdicts[1:])
        out = {"size": size, "generate_seconds": round(gen_seconds, 4), "forward_seconds": round(verify_seconds, 6),
               "deterministic": again.public == inst.public and again.secret == inst.secret,
               "seed_changes_instance": other.public != inst.public,
               "secret_verifies": verdicts[0], "random_false_positives": false_positives, "trials": len(randoms),
               "public_chars": len(json.dumps(inst.public["data"]))}
        out["passed"] = bool(out["deterministic"] and out["seed_changes_instance"] and out["secret_verifies"]
                             and false_positives <= max(1, len(randoms) // 50))
        return out


def load(conn: sqlite3.Connection, workspace: Path, name: str, **kwargs: Any) -> CandidateFamily:
    """The stored family of a candidate. Raises KeyError when the candidate has none."""
    pid = problem_id_of(name)
    problem = store.get(conn, "problems", pid) if pid else None
    if problem is None:
        raise KeyError(f"no candidate problem {pid!r}")
    path = code_path(workspace, pid)
    if problem.get("generator_path") != family_name(pid) or not path.exists():
        raise KeyError(f"candidate {pid} has no family yet: define one with define_family (generator and "
                       f"verifier script), then sample and attack it")
    return CandidateFamily(pid, path.read_text(encoding="utf-8"), **kwargs)


def define(conn: sqlite3.Connection, workspace: Path, problem_id: str, code: str, *, size: int = 4,
           engine: str = "python", timeout: float = 60.0, question_id: str | None = None,
           run_id: str | None = None) -> dict[str, Any]:
    """Check a family script and, when it passes, store it as the candidate's family and move the candidate
    from 'idea' to 'well_defined'. Records the check as an experiment and a finding either way."""
    problem = store.get(conn, "problems", problem_id)
    if problem is None:
        raise KeyError(f"no problem {problem_id!r}")
    if problem["status"] == "known":
        raise FamilyError("known problems use the built-in families")
    if re.search(r"\bmathresearch\b", code):
        raise FamilyError("a family script must be self-contained: it may not import or read the mathresearch "
                          "package")
    workdir = Path(workspace) / "experiments" / (run_id or "manual")
    fam = CandidateFamily(problem_id, code, engine=engine, timeout=timeout, workdir=workdir)
    try:
        check = fam.check(size)
    except FamilyError as exc:
        check = {"size": size, "passed": False, "error": str(exc)}
    exp_id = store.insert(conn, "experiments", kind="check", engine=engine, problem_id=problem_id,
                          question_id=question_id, run_id=run_id, code=code, code_sha256=fam.code_sha256,
                          params={"family": fam.name, "size": size, "define_family": True}, seed=0,
                          stdout=json.dumps(check), exit_code=0, wall_seconds=check.get("generate_seconds") or 0.0,
                          result=check)
    if check["passed"]:
        claim = (f"{problem['name']}: family script runs at size {size}; generation is deterministic, the planted "
                 f"secret verifies and {check['random_false_positives']}/{check['trials']} random candidates do; "
                 f"verification costs {check['forward_seconds']:.2e}s.")
    else:
        claim = f"{problem['name']}: family script failed its well-definedness check at size {size}: {check}"
    fnd_id = store.insert(conn, "findings", problem_id=problem_id, question_id=question_id, run_id=run_id,
                          kind="experimental" if check["passed"] else "negative", claim=claim[:2000],
                          evidence_kind="experiment", evidence_id=exp_id, confidence=0.9)
    if check["passed"]:
        path = code_path(workspace, problem_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
        changes: dict[str, Any] = {"generator_path": fam.name}
        if problem["status"] == "idea":
            changes["status"] = "well_defined"
        store.update(conn, "problems", problem_id, **changes)
    return {"passed": check["passed"], "check": check, "experiment_id": exp_id, "finding_id": fnd_id,
            "family": fam.name, "code_sha256": fam.code_sha256}
