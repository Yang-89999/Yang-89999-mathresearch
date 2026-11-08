"""Attacks as code. Each attack takes an instance and a time budget and reports
whether it recovered a valid solution, how long it took, and a work count.

The pure-Python attacks are deliberately simple; their purpose is to catch
candidates that are trivially broken and to produce cost curves at small
sizes. Gröbner-basis attacks run through SageMath when it is installed.
"""

from __future__ import annotations

import itertools
import random
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import gf
from .families import Instance, ProblemFamily, monomials


@dataclass
class AttackResult:
    attack: str
    success: bool
    seconds: float
    work: int = 0                 # attack-specific unit: candidates tried, rows reduced, ...
    timed_out: bool = False
    solution: Any = None
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Attack(Protocol):
    name: str
    description: str
    exploits: str
    requires: list[str]

    def applies(self, family: ProblemFamily) -> bool: ...
    def run(self, family: ProblemFamily, instance: Instance, time_limit: float) -> AttackResult: ...


def _quadratic_family(family: ProblemFamily) -> bool:
    return family.name in ("mq", "matsumoto_imai")


class ExhaustiveSearch:
    name = "exhaustive_search"
    description = "Try every assignment of the n variables over GF(p). Cost p^n evaluations."
    exploits = "nothing: the generic baseline every candidate must beat"
    requires: list[str] = []

    def applies(self, family: ProblemFamily) -> bool:
        return _quadratic_family(family)

    def run(self, family: ProblemFamily, instance: Instance, time_limit: float) -> AttackResult:
        p, n = instance.params["p"], instance.params["n"]
        start = time.perf_counter()
        tried = 0
        space = p ** n  # reported as the work unit: the full search space, deterministic in n
        for x in itertools.product(range(p), repeat=n):
            tried += 1
            if family.verify(instance, list(x)):
                return AttackResult(self.name, True, time.perf_counter() - start, space, solution=list(x),
                                    extra={"tried": tried})
            if tried % 256 == 0 and time.perf_counter() - start > time_limit:
                return AttackResult(self.name, False, time.perf_counter() - start, space, timed_out=True,
                                    extra={"tried": tried})
        return AttackResult(self.name, False, time.perf_counter() - start, space, notes="no solution found",
                            extra={"tried": tried})


class Linearization:
    name = "linearization"
    description = ("Treat every monomial x_i x_j as an independent unknown and solve the resulting linear system "
                   "over GF(p). Succeeds when m is about n(n+3)/2 or more, i.e. for heavily overdetermined systems.")
    exploits = "an excess of equations over the number of distinct monomials"
    requires: list[str] = []

    def applies(self, family: ProblemFamily) -> bool:
        return _quadratic_family(family)

    def run(self, family: ProblemFamily, instance: Instance, time_limit: float) -> AttackResult:
        p, n = instance.params["p"], instance.params["n"]
        polys = instance.public["polys"]
        monos = monomials(n)
        col = {m: i for i, m in enumerate(monos)}
        const = col[()]
        start = time.perf_counter()
        rows, rhs = [], []
        for f in polys:
            row = [0] * len(monos)
            for mono, c in f.items():
                row[col[mono]] = c % p
            # move the constant to the right-hand side: sum(c_m * mono) = -c_0
            rhs.append((-row[const]) % p)
            row[const] = 0
            rows.append(row)
        # drop the constant column
        rows = [r[:const] + r[const + 1:] for r in rows]
        rk = gf.rank(rows, p)
        unknowns = len(monos) - 1
        sol = gf.solve_linear(rows, rhs, p)
        seconds = time.perf_counter() - start
        extra = {"rank": rk, "unknowns": unknowns, "equations": len(polys)}
        if sol is None:
            return AttackResult(self.name, False, seconds, rk, notes="inconsistent linear system", extra=extra)
        # read x_i from the linear monomials (which precede the constant in `monomials`)
        lin_start = len(monos) - 1 - n
        x = [sol[lin_start + i] % p for i in range(n)]
        ok = family.verify(instance, x)
        notes = "" if ok else f"rank {rk} < {unknowns} unknowns: solution space too large to read x directly"
        return AttackResult(self.name, ok, seconds, rk, solution=x if ok else None, notes=notes, extra=extra)


class PatarinLinearizationEquations:
    """Patarin's 1995 attack on Matsumoto-Imai.

    For C*, every (x, y = P(x)) satisfies bilinear relations sum a_ij x_i y_j
    + sum b_i x_i + sum c_j y_j + d = 0. The attacker collects them from
    known pairs (which anyone can generate from the public key), then for a
    target y the relations become linear in x. With enough independent
    relations x is recovered by linear algebra, no trapdoor needed.
    """

    name = "patarin_linearization_equations"
    description = ("Find bilinear relations between inputs and outputs of the public map from random pairs, then "
                   "solve for the preimage of a target output by linear algebra.")
    exploits = "the hidden monomial structure x^(1+p^theta): x^(p^theta) y = x y^(p^theta) over GF(p^n)"
    requires: list[str] = []

    def applies(self, family: ProblemFamily) -> bool:
        return _quadratic_family(family)

    def run(self, family: ProblemFamily, instance: Instance, time_limit: float) -> AttackResult:
        p, n = instance.params["p"], instance.params["n"]
        rng = random.Random(instance.seed if instance.seed is not None else 0)
        start = time.perf_counter()
        # Work with the unshifted public map Q(x) = P(x) + y0 so the target is y0 (the published system is P - y0).
        polys = instance.public["polys"]
        def Q(x: list[int]) -> list[int]:
            return [v % p for v in family.forward(instance, x)]
        # unknown coefficients: a_ij (n*n), b_i (n), c_j (n), d (1)
        n_unknowns = n * n + 2 * n + 1
        samples = n_unknowns + 10
        rows = []
        for _ in range(samples):
            x = [rng.randrange(p) for _ in range(n)]
            y = Q(x)
            row = [(x[i] * y[j]) % p for i in range(n) for j in range(n)] + x + y + [1]
            rows.append(row)
            if time.perf_counter() - start > time_limit:
                return AttackResult(self.name, False, time.perf_counter() - start, len(rows), timed_out=True)
        # kernel of the sample matrix = the space of valid relations
        rref_rows, pivots = gf.rref(rows, p)
        free = [c for c in range(n_unknowns) if c not in pivots]
        relations = []
        for fcol in free:
            vec = [0] * n_unknowns
            vec[fcol] = 1
            for r, pc in enumerate(pivots):
                vec[pc] = (-rref_rows[r][fcol]) % p
            relations.append(vec)
        if not relations:
            return AttackResult(self.name, False, time.perf_counter() - start, len(rows),
                                notes="no bilinear relations found: the map does not look like C*",
                                extra={"relations": 0})
        # Target: the published system has the planted x as a zero, i.e. Q(x) = 0 vector. Substitute y = 0:
        # relations become sum b_i x_i + d = 0.
        y = [0] * n
        lin_rows, lin_rhs = [], []
        for rel in relations:
            a = rel[: n * n]
            b = rel[n * n: n * n + n]
            c = rel[n * n + n: n * n + 2 * n]
            d = rel[-1]
            coeffs = [(b[i] + sum(a[i * n + j] * y[j] for j in range(n))) % p for i in range(n)]
            rhs = (-(d + sum(c[j] * y[j] for j in range(n)))) % p
            lin_rows.append(coeffs)
            lin_rhs.append(rhs)
        rk = gf.rank(lin_rows, p)
        seconds = time.perf_counter() - start
        extra = {"relations": len(relations), "linear_rank_on_target": rk, "n": n}
        if rk < n:
            # The relations pin x down to an affine space of dimension n - rk; finish by brute force over it
            # when that space is small, otherwise report partial success.
            sol0 = gf.solve_linear(lin_rows, lin_rhs, p)
            if sol0 is not None and (n - rk) <= 3:
                # enumerate the affine solution space: basis of the kernel of lin_rows
                rr, piv = gf.rref(lin_rows, p)
                free_cols = [c for c in range(n) if c not in piv]
                basis = []
                for fc in free_cols:
                    v = [0] * n
                    v[fc] = 1
                    for r, pc in enumerate(piv):
                        v[pc] = (-rr[r][fc]) % p
                    basis.append(v)
                for coeffs in itertools.product(range(p), repeat=len(basis)):
                    x = [(sol0[i] + sum(k * bv[i] for k, bv in zip(coeffs, basis))) % p for i in range(n)]
                    if family.verify(instance, x):
                        return AttackResult(self.name, True, time.perf_counter() - start, len(rows), solution=x,
                                            notes=f"{len(relations)} relations, rank {rk}: finished by enumerating "
                                                  f"p^{n - rk} candidates", extra=extra)
            return AttackResult(self.name, False, seconds, len(rows), extra=extra,
                                notes=f"{len(relations)} relations but rank {rk} < {n} on the target")
        x = gf.solve_linear(lin_rows, lin_rhs, p)
        ok = x is not None and family.verify(instance, x)
        return AttackResult(self.name, ok, time.perf_counter() - start, len(rows), solution=x if ok else None,
                            notes=f"{len(relations)} relations, rank {rk}", extra=extra)


class GroebnerSage:
    name = "groebner_sage"
    description = ("Compute a Groebner basis of the ideal (F, field equations) with SageMath (Singular's F4/slimgb "
                   "or msolve when available) and read off the variety.")
    exploits = "algebraic structure: low degree of regularity, few variables, or overdetermined systems"
    requires = ["sage"]

    def applies(self, family: ProblemFamily) -> bool:
        return _quadratic_family(family) and shutil.which("sage") is not None

    def run(self, family: ProblemFamily, instance: Instance, time_limit: float) -> AttackResult:
        from ..tools.sage_runner import run_script
        p, n = instance.params["p"], instance.params["n"]
        polys = instance.public["polys"]
        terms = []
        for f in polys:
            parts = []
            for mono, c in f.items():
                if c % p == 0:
                    continue
                if mono == ():
                    parts.append(str(c))
                else:
                    parts.append(str(c) + "*" + "*".join(f"x{i}" for i in mono))
            terms.append(" + ".join(parts) or "0")
        code = (
            "import json, time\n"
            f"R = PolynomialRing(GF({p}), {n}, 'x', order='degrevlex')\n"
            "x = R.gens()\n"
            "for i, g in enumerate(x): globals()[f'x{i}'] = g\n"
            f"F = [{', '.join(terms)}]\n"
            f"F += [g**{p} - g for g in x]\n"
            "t0 = time.time()\n"
            "I = R.ideal(F)\n"
            "G = I.groebner_basis()\n"
            "V = I.variety()\n"
            "print('RESULT:', json.dumps({'seconds': time.time() - t0, 'basis_size': len(G), "
            "'solutions': [[int(v[g]) for g in x] for v in V]}))\n"
        )
        res = run_script(code, engine="sage", timeout=time_limit)
        if res.timed_out or not res.ok or not isinstance(res.result, dict):
            return AttackResult(self.name, False, res.wall_seconds, 0, timed_out=res.timed_out,
                                notes=(res.stderr or "")[-500:])
        sols = res.result.get("solutions", [])
        good = next((s for s in sols if family.verify(instance, s)), None)
        return AttackResult(self.name, good is not None, res.wall_seconds, res.result.get("basis_size", 0),
                            solution=good, extra={"sage_seconds": res.result.get("seconds"), "solutions": len(sols)})


ATTACKS: dict[str, Attack] = {a.name: a for a in (ExhaustiveSearch(), Linearization(),
                                                   PatarinLinearizationEquations(), GroebnerSage())}


def get_attack(name: str) -> Attack:
    if name not in ATTACKS:
        raise KeyError(f"unknown attack {name!r}; known: {sorted(ATTACKS)}")
    return ATTACKS[name]


def applicable(family: ProblemFamily) -> list[Attack]:
    return [a for a in ATTACKS.values() if a.applies(family)]
