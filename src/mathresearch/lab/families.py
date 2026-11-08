"""Problem families: a candidate problem as code.

A family knows how to sample an instance of a given size with a planted
secret, compute the forward map, and verify a claimed solution. Everything
the ladder and the attacks need is expressed through this interface, so a
new candidate becomes testable by adding one class.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from itertools import combinations_with_replacement
from typing import Any, Protocol

from . import gf

Monomial = tuple[int, ...]  # () constant, (i,) linear, (i, j) with i <= j quadratic
Poly = dict[Monomial, int]


@dataclass
class Instance:
    family: str
    size: int
    params: dict[str, Any]
    public: dict[str, Any]
    secret: dict[str, Any] = field(default_factory=dict, repr=False)
    seed: int | None = None


class ProblemFamily(Protocol):
    name: str
    description: str
    domains: list[str]

    def params(self, size: int) -> dict[str, Any]: ...
    def generate(self, size: int, seed: int | None = None) -> Instance: ...
    def forward(self, instance: Instance, candidate: Any) -> Any: ...
    def verify(self, instance: Instance, candidate: Any) -> bool: ...
    def random_candidate(self, instance: Instance, rng: random.Random) -> Any: ...


# -- polynomial helpers -------------------------------------------------------

def monomials(n: int) -> list[Monomial]:
    """All monomials of degree <= 2 in n variables: quadratic, linear, constant."""
    quad = list(combinations_with_replacement(range(n), 2))
    return quad + [(i,) for i in range(n)] + [()]


def evaluate(poly: Poly, x: list[int], p: int) -> int:
    total = 0
    for mono, c in poly.items():
        term = c
        for i in mono:
            term = term * x[i]
        total += term
    return total % p


def random_quadratic(n: int, p: int, rng: random.Random, affine: bool = True) -> Poly:
    poly: Poly = {m: rng.randrange(p) for m in combinations_with_replacement(range(n), 2)}
    if affine:
        for i in range(n):
            poly[(i,)] = rng.randrange(p)
        poly[()] = 0
    return poly


class MQ:
    """Random multivariate quadratic system over GF(p) with a planted solution.

    size = number of variables n; the number of equations is m = ratio * n.
    Public: the m polynomials (constants adjusted so the planted x is a zero).
    Secret: x. Candidate: a vector x' with F(x') = 0.
    """

    name = "mq"
    description = ("Solve m random quadratic equations in n variables over GF(p). The forward map is "
                   "evaluating the system; the hidden input is a common zero. NP-hard in general; the "
                   "foundation of multivariate cryptography.")
    domains = ["multivariate polynomials", "finite fields", "algebraic geometry"]

    def __init__(self, p: int = 31, ratio: float = 1.0):
        if not gf.is_prime(p):
            raise ValueError("MQ family needs a prime field size")
        self.p, self.ratio = p, ratio

    def params(self, size: int) -> dict[str, Any]:
        return {"p": self.p, "n": size, "m": max(1, round(self.ratio * size))}

    def generate(self, size: int, seed: int | None = None) -> Instance:
        rng = random.Random(seed)
        prm = self.params(size)
        n, m, p = prm["n"], prm["m"], prm["p"]
        x = [rng.randrange(p) for _ in range(n)]
        polys = []
        for _ in range(m):
            f = random_quadratic(n, p, rng)
            f[()] = (-evaluate(f, x, p)) % p  # plant x as a zero
            polys.append(f)
        return Instance(self.name, size, prm, {"polys": polys}, {"x": x}, seed)

    def forward(self, instance: Instance, candidate: list[int]) -> list[int]:
        return [evaluate(f, candidate, instance.params["p"]) for f in instance.public["polys"]]

    def verify(self, instance: Instance, candidate: list[int]) -> bool:
        if candidate is None or len(candidate) != instance.params["n"]:
            return False
        return all(v == 0 for v in self.forward(instance, candidate))

    def random_candidate(self, instance: Instance, rng: random.Random) -> list[int]:
        return [rng.randrange(instance.params["p"]) for _ in range(instance.params["n"])]


class MatsumotoImai:
    """The C* scheme of Matsumoto and Imai (1988), as a trapdoored MQ family.

    Over GF(p) with n variables, the central map is x -> x^(1 + p^theta) on
    GF(p^n), hidden by two secret affine bijections S and T. The public key is
    the composed quadratic map. Broken by Patarin's linearization equations
    (1995): this family exists so the system can be tested on a known answer.

    Implementation note: GF(p^n) arithmetic is done through a fixed
    irreducible polynomial found by search; sizes stay small, which is all the
    retrodiction test needs.
    """

    name = "matsumoto_imai"
    description = ("C*: public quadratic map P = T o (x -> x^(1+2^theta)) o S over GF(2^n), with S, T secret affine "
                   "bijections. Inverting P on a given output is the problem; the trapdoor is the inverse exponent.")
    domains = ["multivariate polynomials", "finite fields", "trapdoor"]

    def __init__(self, p: int = 2, theta: int = 1):
        # 1 + p^theta must be coprime to p^n - 1; for odd p both are even, so C* lives in characteristic 2.
        if p != 2:
            raise ValueError("Matsumoto-Imai is defined over GF(2^n): p must be 2")
        self.p, self.theta = p, theta

    def params(self, size: int) -> dict[str, Any]:
        return {"p": self.p, "n": size, "m": size, "theta": self.theta}

    # polynomial arithmetic in GF(p)[t] / (modulus), elements as coefficient lists of length n
    def _modulus(self, n: int, rng: random.Random) -> list[int]:
        p = self.p
        while True:
            coeffs = [rng.randrange(p) for _ in range(n)] + [1]
            if coeffs[0] and self._irreducible(coeffs):
                return coeffs

    def _irreducible(self, f: list[int]) -> bool:
        # Ben-Or style test by trial: check gcd(f, t^(p^i) - t) = 1 for i <= n/2 via brute roots in small cases
        p, n = self.p, len(f) - 1
        if n == 1:
            return True
        # brute force: no polynomial of degree 1..n//2 divides f (only feasible for tiny p^d; sizes are small)
        from itertools import product
        for d in range(1, n // 2 + 1):
            for low in product(range(p), repeat=d):
                g = list(low) + [1]
                if self._polymod(f, g) == [0] * d:
                    return False
        return True

    def _polymod(self, a: list[int], g: list[int]) -> list[int]:
        p = self.p
        a = a[:]
        dg = len(g) - 1
        for i in range(len(a) - 1, dg - 1, -1):
            c = a[i] % p
            if c:
                for j in range(dg + 1):
                    a[i - dg + j] = (a[i - dg + j] - c * g[j]) % p
        return [x % p for x in a[:dg]] + [0] * max(0, dg - len(a))

    def _mul(self, a: list[int], b: list[int], mod: list[int]) -> list[int]:
        n, p = len(mod) - 1, self.p
        prod = [0] * (2 * n - 1)
        for i, x in enumerate(a):
            if x:
                for j, y in enumerate(b):
                    prod[i + j] = (prod[i + j] + x * y) % p
        return self._polymod(prod, mod)

    def _pow(self, a: list[int], e: int, mod: list[int]) -> list[int]:
        n = len(mod) - 1
        result = [1] + [0] * (n - 1)
        base = a[:]
        while e:
            if e & 1:
                result = self._mul(result, base, mod)
            base = self._mul(base, base, mod)
            e >>= 1
        return result

    def generate(self, size: int, seed: int | None = None) -> Instance:
        rng = random.Random(seed)
        n, p, theta = size, self.p, self.theta
        from math import gcd
        if gcd(1 + p ** theta, p ** n - 1) != 1:
            raise ValueError(f"1 + p^theta must be coprime to p^n - 1 (p={p}, theta={theta}, n={n})")
        mod = self._modulus(n, rng)
        S, s = gf.random_invertible(n, p, rng), [rng.randrange(p) for _ in range(n)]
        T, t = gf.random_invertible(n, p, rng), [rng.randrange(p) for _ in range(n)]
        e = 1 + p ** theta
        # P is quadratic over GF(2); with x_i^2 = x_i it is determined by P(0), P(e_i) and P(e_i + e_j).
        def P(x: list[int]) -> list[int]:
            u = [(v + c) % p for v, c in zip(gf.matvec(S, x, p), s)]
            w = self._pow(u, e, mod)
            return [(v + c) % p for v, c in zip(gf.matvec(T, w, p), t)]
        P0 = P([0] * n)
        Pe = [P([1 if k == i else 0 for k in range(n)]) for i in range(n)]
        polys: list[Poly] = [{(): P0[k]} for k in range(n)]
        for i in range(n):
            for k in range(n):
                polys[k][(i,)] = (Pe[i][k] - P0[k]) % p
        for i in range(n):
            for j in range(i + 1, n):
                Pij = P([1 if k in (i, j) else 0 for k in range(n)])
                for k in range(n):
                    polys[k][(i, j)] = (Pij[k] - P0[k] - polys[k][(i,)] - polys[k][(j,)]) % p
        x = [rng.randrange(p) for _ in range(n)]
        y = [evaluate(f, x, p) for f in polys]
        public_polys = [dict(f) for f in polys]
        for k in range(n):  # shift so that the planted x is a zero of the published system
            public_polys[k][()] = (public_polys[k][()] - y[k]) % p
        return Instance(self.name, size, {**self.params(size), "modulus": mod}, {"polys": public_polys},
                        {"x": x, "S": S, "s": s, "T": T, "t": t, "y": y}, seed)

    def forward(self, instance: Instance, candidate: list[int]) -> list[int]:
        return [evaluate(f, candidate, instance.params["p"]) for f in instance.public["polys"]]

    def verify(self, instance: Instance, candidate: list[int]) -> bool:
        if candidate is None or len(candidate) != instance.params["n"]:
            return False
        return all(v == 0 for v in self.forward(instance, candidate))

    def random_candidate(self, instance: Instance, rng: random.Random) -> list[int]:
        return [rng.randrange(instance.params["p"]) for _ in range(instance.params["n"])]


FAMILIES: dict[str, type] = {MQ.name: MQ, MatsumotoImai.name: MatsumotoImai}


def get_family(name: str, **kwargs: Any) -> ProblemFamily:
    if name not in FAMILIES:
        raise KeyError(f"unknown problem family {name!r}; known: {sorted(FAMILIES)}")
    return FAMILIES[name](**kwargs)


def time_forward(family: ProblemFamily, instance: Instance, repeats: int = 20) -> float:
    """Seconds per forward-map evaluation on the planted secret."""
    x = instance.secret["x"]
    start = time.perf_counter()
    for _ in range(repeats):
        family.forward(instance, x)
    return (time.perf_counter() - start) / repeats
