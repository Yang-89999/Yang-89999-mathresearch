"""Run an attack over a range of sizes and summarise how its cost grows."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .attacks import Attack, AttackResult
from .families import ProblemFamily


@dataclass
class CostCurve:
    family: str
    attack: str
    points: list[dict[str, Any]] = field(default_factory=list)  # size, seconds, success, work, timed_out
    growth: str = "unknown"          # polynomial-like | exponential-like | flat | unknown
    exp_slope: float | None = None   # d ln(time) / d size
    poly_degree: float | None = None  # d ln(time) / d ln(size)
    largest_broken: int | None = None
    smallest_unbroken: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"family": self.family, "attack": self.attack, "points": self.points, "growth": self.growth,
                "exp_slope": self.exp_slope, "poly_degree": self.poly_degree,
                "largest_broken": self.largest_broken, "smallest_unbroken": self.smallest_unbroken}


def sweep(family: ProblemFamily, attack: Attack, sizes: list[int], time_limit: float = 30.0,
          seed: int = 0, stop_after_timeout: bool = True) -> CostCurve:
    curve = CostCurve(family.name, attack.name)
    for size in sizes:
        instance = family.generate(size, seed=seed + size)
        res: AttackResult = attack.run(family, instance, time_limit)
        curve.points.append({"size": size, "seconds": round(res.seconds, 6), "success": res.success,
                             "work": res.work, "timed_out": res.timed_out, "notes": res.notes, **res.extra})
        if res.success:
            curve.largest_broken = size
        elif curve.smallest_unbroken is None:
            curve.smallest_unbroken = size
        if res.timed_out and stop_after_timeout:
            break
    _fit(curve)
    return curve


MIN_SECONDS = 0.005  # wall-clock points below this are timer noise and are not fitted


def _fit(curve: CostCurve) -> None:
    """Classify growth from the attack's deterministic work count when it reports one,
    otherwise from wall-clock time above the noise floor."""
    ok = [p for p in curve.points if p["success"]]
    if ok and all(p.get("work", 0) > 0 for p in ok) and len({p["work"] for p in ok}) > 1:
        pts = [(p["size"], float(p["work"])) for p in ok]
    else:
        pts = [(p["size"], p["seconds"]) for p in ok if p["seconds"] >= MIN_SECONDS]
    if len(pts) < 3:
        curve.growth = "unknown"
        return
    xs = [s for s, _ in pts]
    ys = [math.log(t) for _, t in pts]
    exp_slope, exp_r2 = _linfit(xs, ys)
    poly_degree, poly_r2 = _linfit([math.log(s) for s in xs], ys)
    curve.exp_slope, curve.poly_degree = round(exp_slope, 4), round(poly_degree, 3)
    if exp_slope <= 0.05 and poly_degree <= 0.5:
        curve.growth = "flat"
    elif exp_r2 >= poly_r2 and exp_slope > 0.3:
        curve.growth = "exponential-like"  # ln(cost) linear in n fits at least as well as in ln(n)
    else:
        curve.growth = "polynomial-like"


def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, 0.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys) or 1e-12
    return slope, 1 - ss_res / ss_tot
