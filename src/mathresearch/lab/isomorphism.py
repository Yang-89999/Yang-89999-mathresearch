"""Isomorphism problems on 3-tensors: tensor isomorphism (TI) and alternating trilinear form equivalence (ATFE).

These are the group-action problems behind ALTEQ and MEDS, and the area the research agents kept asking the lab
to support. Two families and two attacks:

* ``tensor_iso``: two n x n x n tensors A, B over GF(q) with B = (P, Q, R) . A for secret P, Q, R in GL_n(q).
* ``atfe``: two alternating trilinear forms phi, psi on GF(q)^n with psi(x, y, z) = phi(Ax, Ay, Az), A in GL_n(q).

Both attacks guess where a few vectors go and finish by linear algebra, the way the known algorithms do
(Bouillaguet-Fouque-Veber for TI-type problems; Beullens, "Graph-theoretic algorithms for the alternating
trilinear form equivalence problem", Crypto 2023, for ATFE):

* ``iso_guess_linear`` guesses the images among all vectors: the generic baseline, about q^(kn) guesses.
* ``iso_rank_filter`` first sorts vectors by an invariant the secret map preserves (the rank of the slice or
  of the bilinear form a vector induces) and only guesses images inside the same rank class. Rare low-rank
  vectors make the guess space much smaller: this is the structure the published attacks exploit.

Pure Python over small prime fields: the point is cost curves at small n, not records.
"""

from __future__ import annotations

import itertools
import random
import time
from typing import Any

from . import gf
from .attacks import ATTACKS, AttackResult
from .families import FAMILIES, Instance

Matrix = list[list[int]]
Tensor = list[list[list[int]]]


def _random_tensor(n: int, p: int, rng: random.Random) -> Tensor:
    return [[[rng.randrange(p) for _ in range(n)] for _ in range(n)] for _ in range(n)]


def act(P: Matrix, Q: Matrix, R: Matrix, T: Tensor, p: int) -> Tensor:
    """(P, Q, R) . T: out[a][b][c] = sum P[a][i] Q[b][j] R[c][k] T[i][j][k], one mode at a time (O(n^4))."""
    n = len(T)
    t1 = [[[sum(P[a][i] * T[i][j][k] for i in range(n)) % p for k in range(n)] for j in range(n)] for a in range(n)]
    t2 = [[[sum(Q[b][j] * t1[a][j][k] for j in range(n)) % p for k in range(n)] for b in range(n)] for a in range(n)]
    return [[[sum(R[c][k] * t2[a][b][k] for k in range(n)) % p for c in range(n)] for b in range(n)] for a in range(n)]


def slice_at(T: Tensor, x: list[int], p: int) -> Matrix:
    """sum_i x_i T[i]: the matrix a vector picks out of the first mode."""
    n = len(T)
    return [[sum(x[i] * T[i][j][k] for i in range(n)) % p for k in range(n)] for j in range(n)]


def _is_invertible(m: Any, n: int, p: int) -> bool:
    return (isinstance(m, list) and len(m) == n and all(isinstance(r, list) and len(r) == n for r in m)
            and gf.rank([[int(v) % p for v in r] for r in m], p) == n)


def _matrix(value: Any) -> Matrix | None:
    try:
        return [[int(v) for v in row] for row in value]
    except (TypeError, ValueError):
        return None


class TensorIso:
    """3-tensor isomorphism. size = n; public A, B in GF(q)^(n x n x n); secret (P, Q, R)."""

    name = "tensor_iso"
    description = ("Given 3-tensors A and B over GF(q), find P, Q, R in GL_n(q) with B = (P, Q, R) . A, i.e. "
                   "B[a][b][c] = sum P[a][i] Q[b][j] R[c][k] A[i][j][k]. TI-complete (Grochow-Qiao); the "
                   "problem behind MEDS-type and tensor-based group-action signatures.")
    domains = ["multilinear algebra", "finite fields", "group actions"]

    def __init__(self, p: int = 3):
        if not gf.is_prime(p):
            raise ValueError("tensor_iso needs a prime field size")
        self.p = p

    def params(self, size: int) -> dict[str, Any]:
        return {"p": self.p, "n": size}

    def generate(self, size: int, seed: int | None = None) -> Instance:
        rng = random.Random(seed)
        n, p = size, self.p
        A = _random_tensor(n, p, rng)
        P, Q, R = (gf.random_invertible(n, p, rng) for _ in range(3))
        return Instance(self.name, size, self.params(size), {"A": A, "B": act(P, Q, R, A, p)},
                        {"x": [P, Q, R]}, seed)

    def forward(self, instance: Instance, candidate: list[Matrix]) -> Tensor:
        P, Q, R = candidate
        return act(P, Q, R, instance.public["A"], instance.params["p"])

    def verify(self, instance: Instance, candidate: Any) -> bool:
        n, p = instance.params["n"], instance.params["p"]
        if not isinstance(candidate, list) or len(candidate) != 3 or \
                not all(_is_invertible(m, n, p) for m in candidate):
            return False
        mats = [[[int(v) % p for v in r] for r in m] for m in candidate]
        return self.forward(instance, mats) == instance.public["B"]

    def random_candidate(self, instance: Instance, rng: random.Random) -> list[Matrix]:
        n, p = instance.params["n"], instance.params["p"]
        return [gf.random_invertible(n, p, rng) for _ in range(3)]

    def decode(self, value: Any) -> list[Matrix] | None:
        if not isinstance(value, list) or len(value) != 3:
            return None
        mats = [_matrix(m) for m in value]
        return None if any(m is None for m in mats) else mats

    def public_json(self, instance: Instance) -> dict[str, Any]:
        return {"A": instance.public["A"], "B": instance.public["B"]}


def triples(n: int) -> list[tuple[int, int, int]]:
    return list(itertools.combinations(range(n), 3))


def full_form(coeffs: list[int], n: int, p: int) -> Tensor:
    """The alternating tensor T of a trilinear form given by its coefficients on i < j < k."""
    T = [[[0] * n for _ in range(n)] for _ in range(n)]
    for (i, j, k), c in zip(triples(n), coeffs):
        for (a, b, d), s in (((i, j, k), 1), ((j, k, i), 1), ((k, i, j), 1),
                             ((j, i, k), -1), ((i, k, j), -1), ((k, j, i), -1)):
            T[a][b][d] = (s * c) % p
    return T


def transform_form(coeffs: list[int], A: Matrix, n: int, p: int) -> list[int]:
    """Coefficients of psi(x, y, z) = phi(Ax, Ay, Az)."""
    T = full_form(coeffs, n, p)
    At = gf.transpose(A)
    U = act(At, At, At, T, p)
    return [U[i][j][k] for i, j, k in triples(n)]


class ATFE:
    """Alternating trilinear form equivalence. size = n; public phi, psi (coefficients on i < j < k); secret A."""

    name = "atfe"
    description = ("Given alternating trilinear forms phi and psi on GF(q)^n, find A in GL_n(q) with "
                   "psi(x, y, z) = phi(Ax, Ay, Az). The hard problem of ALTEQ; equivalent to tensor isomorphism "
                   "under polynomial reductions (Grochow-Qiao).")
    domains = ["multilinear algebra", "finite fields", "group actions"]

    def __init__(self, p: int = 3):
        if not gf.is_prime(p):
            raise ValueError("atfe needs a prime field size")
        self.p = p

    def params(self, size: int) -> dict[str, Any]:
        if size < 3:
            raise ValueError("atfe needs n >= 3")
        return {"p": self.p, "n": size}

    def generate(self, size: int, seed: int | None = None) -> Instance:
        rng = random.Random(seed)
        prm = self.params(size)
        n, p = size, self.p
        phi = [rng.randrange(p) for _ in triples(n)]
        A = gf.random_invertible(n, p, rng)
        return Instance(self.name, size, prm, {"phi": phi, "psi": transform_form(phi, A, n, p)}, {"x": A}, seed)

    def forward(self, instance: Instance, candidate: Matrix) -> list[int]:
        return transform_form(instance.public["phi"], candidate, instance.params["n"], instance.params["p"])

    def verify(self, instance: Instance, candidate: Any) -> bool:
        n, p = instance.params["n"], instance.params["p"]
        if not _is_invertible(candidate, n, p):
            return False
        return self.forward(instance, [[int(v) % p for v in r] for r in candidate]) == instance.public["psi"]

    def random_candidate(self, instance: Instance, rng: random.Random) -> Matrix:
        return gf.random_invertible(instance.params["n"], instance.params["p"], rng)

    def decode(self, value: Any) -> Matrix | None:
        return _matrix(value) if isinstance(value, list) else None

    def public_json(self, instance: Instance) -> dict[str, Any]:
        n = instance.params["n"]
        return {"phi": [[i, j, k, c] for (i, j, k), c in zip(triples(n), instance.public["phi"])],
                "psi": [[i, j, k, c] for (i, j, k), c in zip(triples(n), instance.public["psi"])]}


INSTANCE_FORMATS = {
    "tensor_iso": """\
instance.json = {"family": "tensor_iso", "n": n, "p": q, "params": {...},
                 "A": n x n x n nested list A[i][j][k], "B": same shape}
B[a][b][c] = sum_{i,j,k} P[a][i] Q[b][j] R[c][k] A[i][j][k]  (mod q) for secret invertible n x n P, Q, R.
Your script must end with:  print("RESULT:", json.dumps({"solution": [P, Q, R], "work": <int or 0>}))
with P, Q, R as lists of rows. Any (P, Q, R) that maps A to B counts.
""",
    "atfe": """\
instance.json = {"family": "atfe", "n": n, "p": q, "params": {...},
                 "phi": [[i, j, k, c], ...], "psi": [[i, j, k, c], ...]}   # every i < j < k, c in GF(q)
phi(x, y, z) = sum_{i<j<k} c_ijk det[[x_i, x_j, x_k], [y_i, y_j, y_k], [z_i, z_j, z_k]], likewise psi;
psi(x, y, z) = phi(Ax, Ay, Az) for a secret invertible n x n matrix A (so psi's coefficient on (a, b, c) is
phi(A e_a, A e_b, A e_c), where A e_a is column a of A).
Your script must end with:  print("RESULT:", json.dumps({"solution": A, "work": <int or 0>}))
with A as a list of rows. Any A that maps phi to psi counts.
""",
}


# -- attacks ----------------------------------------------------------------------------------------------------

def _vectors(n: int, p: int):
    return (list(v) for v in itertools.product(range(p), repeat=n) if any(v))


def _rank_classes(matrix_at, n: int, p: int) -> dict[int, list[list[int]]]:
    """Nonzero vectors grouped by the rank of the matrix they induce."""
    classes: dict[int, list[list[int]]] = {}
    for v in _vectors(n, p):
        classes.setdefault(gf.rank(matrix_at(v), p), []).append(v)
    return classes


def _pick(classes: dict[int, list[list[int]]], k: int, p: int) -> list[tuple[list[int], int]]:
    """k linearly independent vectors, taken from the rarest rank classes first, each with its rank."""
    chosen: list[tuple[list[int], int]] = []
    for r in sorted(classes, key=lambda r: (len(classes[r]), r)):
        for v in classes[r]:
            if gf.rank([c for c, _ in chosen] + [v], p) == len(chosen) + 1:
                chosen.append((v, r))
                if len(chosen) == k:
                    return chosen
    return chosen


class _System:
    """A linear system over GF(p) built a few equations at a time, kept in echelon form so a wrong guess shows up
    as soon as it makes the system inconsistent. Rows are never changed once stored, so a copy is cheap."""

    def __init__(self, ncols: int, p: int):
        self.ncols, self.p = ncols, p
        self.pivots: list[tuple[int, list[int]]] = []   # (column, row with 1 at column; last entry = rhs)

    def copy(self) -> "_System":
        other = _System(self.ncols, self.p)
        other.pivots = list(self.pivots)
        return other

    def add(self, row: list[int]) -> bool:
        p = self.p
        for col, prow in self.pivots:
            c = row[col] % p
            if c:
                row = [(a - c * b) % p for a, b in zip(row, prow)]
        lead = next((i for i in range(self.ncols) if row[i] % p), None)
        if lead is None:
            return row[-1] % p == 0
        s = gf.inv(row[lead], p)
        self.pivots.append((lead, [(a * s) % p for a in row]))
        return True

    def free(self) -> int:
        return self.ncols - len(self.pivots)

    def solutions(self):
        rows = [r[:-1] for _, r in self.pivots]
        rhs = [r[-1] for _, r in self.pivots]
        sol = gf.solve_affine(rows or [[0] * self.ncols], rhs or [0], self.p, ncols=self.ncols)
        if sol is None:
            return
        x0, basis = sol
        for coeffs in itertools.product(range(self.p), repeat=len(basis)):
            yield [(x + sum(c * b[i] for c, b in zip(coeffs, basis))) % self.p for i, x in enumerate(x0)]


def _atfe_rows(S: Tensor, T: Tensor, u: list[int], v: list[int], n: int, p: int) -> list[list[int]]:
    """psi(x,y,z) = phi(Ax,Ay,Az) gives Psi_u = A^T Phi_{Au} A (Psi_u = slice of psi at u). With v = Au guessed:
    D Psi_u = Phi_v A for D = A^-T, linear in the 2n^2 entries of (A, D); and A u = v."""
    N = n * n
    Su, Tv = slice_at(S, u, p), slice_at(T, v, p)
    rows = []
    for r in range(n):
        row = [0] * (2 * N + 1)
        for c in range(n):
            row[r * n + c] = u[c] % p
        row[-1] = v[r] % p
        rows.append(row)
    for r in range(n):
        for c in range(n):
            row = [0] * (2 * N + 1)
            for k in range(n):
                row[N + r * n + k] = (row[N + r * n + k] + Su[k][c]) % p      # D[r][k] Psi_u[k][c]
                row[k * n + c] = (row[k * n + c] - Tv[r][k]) % p             # - Phi_v[r][k] A[k][c]
            rows.append(row)
    return rows


def _ti_rows(S: Tensor, T: Tensor, y: list[int], x: list[int], n: int, p: int) -> list[list[int]]:
    """Slices: S_y = Q T_{P^T y} R^T. With x = P^T y guessed: Q' S_y = T_x R^T for Q' = Q^-1, linear and
    homogeneous in (Q', R)."""
    N = n * n
    Sy, Tx = slice_at(S, y, p), slice_at(T, x, p)
    rows = []
    for r in range(n):
        for c in range(n):
            row = [0] * (2 * N + 1)
            for k in range(n):
                row[r * n + k] = (row[r * n + k] + Sy[k][c]) % p              # Q'[r][k] S_y[k][c]
                row[N + c * n + k] = (row[N + c * n + k] - Tx[r][k]) % p      # - T_x[r][k] R[c][k]
            rows.append(row)
    return rows


def _atfe_finish(family, instance, x: list[int], T: Tensor, S: Tensor):
    n = instance.params["n"]
    A = [x[r * n:(r + 1) * n] for r in range(n)]
    return A if family.verify(instance, A) else None


def _ti_finish(family, instance, x: list[int], T: Tensor, S: Tensor):
    """From (Q', R), each row of P follows linearly from Q' S_a R^-T = sum_i P[a][i] T_i."""
    n, p = instance.params["n"], instance.params["p"]
    N = n * n
    Qp = [x[r * n:(r + 1) * n] for r in range(n)]
    R = [x[N + r * n:N + (r + 1) * n] for r in range(n)]
    Q, Rinv = gf.inverse(Qp, p), gf.inverse(R, p)
    if Q is None or Rinv is None:
        return None
    RinvT = gf.transpose(Rinv)
    cols = [[T[i][j][k] for i in range(n)] for j in range(n) for k in range(n)]   # one equation per (j, k)
    P = []
    for a in range(n):
        M = gf.matmul(gf.matmul(Qp, S[a], p), RinvT, p)
        row = gf.solve_affine(cols, [M[j][k] for j in range(n) for k in range(n)], p, ncols=n)
        if row is None:
            return None
        P.append(row[0])
    return [P, Q, R] if family.verify(instance, [P, Q, R]) else None


class _OutOfTime(Exception):
    pass


LEAF_DIM = 3   # once at most this many free unknowns remain, try every solution (q^3 candidates)


class IsoGuessLinear:
    """Guess images of vectors one at a time; each guess adds linear equations, a wrong one soon makes them
    inconsistent (backtracking), and once the solution space is small every solution is checked."""

    name = "iso_guess_linear"
    description = ("Guess where basis vectors go, one at a time, among all vectors of GF(q)^n. Each guess adds "
                   "linear equations on the secret map (with its inverse as extra unknowns); a wrong guess soon "
                   "makes them inconsistent and is abandoned. Ends when the equations leave few solutions.")
    exploits = "nothing beyond the bilinear structure of the action: the generic baseline"
    requires: list[str] = []
    use_ranks = False

    def applies(self, family) -> bool:
        return family.name in ("tensor_iso", "atfe")

    def run(self, family, instance: Instance, time_limit: float) -> AttackResult:
        start = time.perf_counter()
        n, p = instance.params["n"], instance.params["p"]
        if family.name == "atfe":
            T = full_form(instance.public["phi"], n, p)
            S = full_form(instance.public["psi"], n, p)
            rows_for, finish, homogeneous = _atfe_rows, _atfe_finish, False
        else:
            T, S = instance.public["A"], instance.public["B"]
            rows_for, finish, homogeneous = _ti_rows, _ti_finish, True
        if self.use_ranks:
            src = _rank_classes(lambda v: slice_at(S, v, p), n, p)    # the side we choose vectors on
            dst = _rank_classes(lambda v: slice_at(T, v, p), n, p)    # where their images must lie
            chosen = _pick(src, n, p)
            pools = [dst.get(r, []) for _, r in chosen]
            extra: dict[str, Any] = {"classes": {str(r): len(vs) for r, vs in sorted(src.items())},
                                     "chosen_ranks": [r for _, r in chosen]}
        else:
            chosen = [([1 if i == j else 0 for i in range(n)], None) for j in range(n)]
            pools = [list(_vectors(n, p))] * n
            extra = {}
        us = [u for u, _ in chosen]
        tried = 0

        def search(depth: int, system: _System, images: list[list[int]]):
            nonlocal tried
            if system.free() <= LEAF_DIM or depth == len(us):
                if system.free() > LEAF_DIM + 2:
                    return None
                for x in system.solutions():
                    sol = finish(family, instance, x, T, S)
                    if sol is not None:
                        return sol
                return None
            for v in pools[depth]:
                tried += 1
                if tried % 32 == 0 and time.perf_counter() - start > time_limit:
                    raise _OutOfTime
                if gf.rank(images + [v], p) <= depth:
                    continue                                  # the map is invertible: images stay independent
                nxt = system.copy()
                if not all(nxt.add(r) for r in rows_for(S, T, us[depth], v, n, p)):
                    continue                                  # inconsistent: wrong guess
                if homogeneous and nxt.free() == 0:
                    continue                                  # only the zero solution left
                sol = search(depth + 1, nxt, images + [v])
                if sol is not None:
                    return sol
            return None

        try:
            sol = search(0, _System(2 * n * n, p), [])
        except _OutOfTime:
            return AttackResult(self.name, False, time.perf_counter() - start, tried, timed_out=True,
                                extra={**extra, "guesses": tried})
        seconds = time.perf_counter() - start
        if sol is None:
            return AttackResult(self.name, False, seconds, tried, notes="search exhausted without a solution",
                                extra={**extra, "guesses": tried})
        return AttackResult(self.name, True, seconds, tried, solution=sol, extra={**extra, "guesses": tried})


class IsoRankFilter(IsoGuessLinear):
    name = "iso_rank_filter"
    description = ("Sort the vectors of GF(q)^n by the rank of the matrix they induce (the slice of a tensor, or "
                   "the bilinear form phi(v, ., .)); the secret map preserves it. Guess images from the rarest "
                   "rank classes first, with the same backtracking linear algebra as iso_guess_linear.")
    exploits = "rank invariants of points: rare low-rank vectors shrink the guess space (Beullens 2023 for ATFE)"
    use_ranks = True


FAMILIES.update({TensorIso.name: TensorIso, ATFE.name: ATFE})
ATTACKS.update({a.name: a for a in (IsoGuessLinear(), IsoRankFilter())})
