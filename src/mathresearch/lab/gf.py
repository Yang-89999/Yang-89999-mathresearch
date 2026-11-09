"""Minimal arithmetic over prime fields GF(p), enough for small attack experiments.

Pure Python on purpose: tests and the harvest lane must run on any machine.
Serious sizes go through SageMath / msolve via the ``groebner`` attack.
"""

from __future__ import annotations

import random


def is_prime(n: int) -> bool:
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    return True


def inv(a: int, p: int) -> int:
    return pow(a % p, p - 2, p)


def rref(matrix: list[list[int]], p: int) -> tuple[list[list[int]], list[int]]:
    """Reduced row echelon form over GF(p). Returns (rows, pivot_columns)."""
    rows = [r[:] for r in matrix]
    if not rows:
        return rows, []
    ncols = len(rows[0])
    pivots: list[int] = []
    r = 0
    for c in range(ncols):
        if r >= len(rows):
            break
        pivot = next((i for i in range(r, len(rows)) if rows[i][c] % p), None)
        if pivot is None:
            continue
        rows[r], rows[pivot] = rows[pivot], rows[r]
        s = inv(rows[r][c], p)
        rows[r] = [(x * s) % p for x in rows[r]]
        for i in range(len(rows)):
            if i != r and rows[i][c] % p:
                f = rows[i][c]
                rows[i] = [(x - f * y) % p for x, y in zip(rows[i], rows[r])]
        pivots.append(c)
        r += 1
    return rows, pivots


def solve_linear(matrix: list[list[int]], rhs: list[int], p: int) -> list[int] | None:
    """Solve A x = b over GF(p). Returns one solution (free variables set to 0) or None."""
    aug = [row[:] + [b % p] for row, b in zip(matrix, rhs)]
    rows, pivots = rref(aug, p)
    n = len(matrix[0]) if matrix else 0
    for row in rows:
        if all(x % p == 0 for x in row[:n]) and row[n] % p:
            return None  # inconsistent
    x = [0] * n
    for r, c in enumerate(pivots):
        if c < n:
            x[c] = rows[r][n] % p
    return x


def rank(matrix: list[list[int]], p: int) -> int:
    return len(rref(matrix, p)[1])


def random_matrix(rows: int, cols: int, p: int, rng: random.Random) -> list[list[int]]:
    return [[rng.randrange(p) for _ in range(cols)] for _ in range(rows)]


def random_invertible(n: int, p: int, rng: random.Random) -> list[list[int]]:
    while True:
        m = random_matrix(n, n, p, rng)
        if rank(m, p) == n:
            return m


def matvec(m: list[list[int]], v: list[int], p: int) -> list[int]:
    return [sum(a * b for a, b in zip(row, v)) % p for row in m]


def solve_affine(matrix: list[list[int]], rhs: list[int], p: int,
                 ncols: int | None = None) -> tuple[list[int], list[list[int]]] | None:
    """All solutions of A x = b over GF(p): (one solution, basis of the kernel of A), or None if inconsistent."""
    n = ncols if ncols is not None else (len(matrix[0]) if matrix else 0)
    aug = [row[:] + [b % p] for row, b in zip(matrix, rhs)]
    rows, pivots = rref(aug, p)
    for row in rows:
        if all(x % p == 0 for x in row[:n]) and row[n] % p:
            return None
    pivots = [c for c in pivots if c < n]
    x = [0] * n
    for r, c in enumerate(pivots):
        x[c] = rows[r][n] % p
    pivot_set = set(pivots)
    basis = []
    for f in range(n):
        if f in pivot_set:
            continue
        v = [0] * n
        v[f] = 1
        for r, c in enumerate(pivots):
            v[c] = (-rows[r][f]) % p
        basis.append(v)
    return x, basis


def inverse(m: list[list[int]], p: int) -> list[list[int]] | None:
    n = len(m)
    aug = [row[:] + [1 if i == j else 0 for j in range(n)] for i, row in enumerate(m)]
    rows, pivots = rref(aug, p)
    if pivots[:n] != list(range(n)):
        return None
    return [row[n:] for row in rows[:n]]


def matmul(a: list[list[int]], b: list[list[int]], p: int) -> list[list[int]]:
    bt = list(zip(*b))
    return [[sum(x * y for x, y in zip(row, col)) % p for col in bt] for row in a]


def transpose(m: list[list[int]]) -> list[list[int]]:
    return [list(r) for r in zip(*m)]
