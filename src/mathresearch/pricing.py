"""Model prices and spending caps.

Prices are US dollars per million tokens. The built-in table covers the Claude
models whose prices are published in Anthropic's model documentation; every
other model (OpenAI's in particular, whose ids and prices change often) is
priced from the charter's ``pricing`` section, which also overrides the
built-in table. A model without a price still runs under the token caps, but
dollar caps cannot be enforced for it, so the loop refuses to start unless the
operator allows unpriced models explicitly.

Token accounting is normalised across providers: ``input_tokens`` is the
whole prompt, ``cached_input_tokens`` the part served from the prompt cache,
``cache_write_tokens`` the part written to it (Anthropic only).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .providers.base import Usage


@dataclass(frozen=True)
class Price:
    input: float                    # $ per million uncached input tokens
    output: float                   # $ per million output tokens (reasoning included)
    cached_input: float | None = None   # $ per million cache-read tokens; defaults to input
    cache_write: float | None = None    # $ per million cache-write tokens; defaults to input

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Price":
        unknown = set(data) - {"input", "output", "cached_input", "cache_write"}
        if unknown:
            raise ValueError(f"unknown price fields {sorted(unknown)}; use input, output, cached_input, cache_write")
        if "input" not in data or "output" not in data:
            raise ValueError("a price needs at least 'input' and 'output' ($ per million tokens)")
        return cls(input=float(data["input"]), output=float(data["output"]),
                   cached_input=float(data["cached_input"]) if data.get("cached_input") is not None else None,
                   cache_write=float(data["cache_write"]) if data.get("cache_write") is not None else None)

    def cost(self, usage: Usage) -> float:
        cached = usage.cached_input_tokens
        written = usage.cache_write_tokens
        fresh = max(0, usage.input_tokens - cached - written)
        total = (fresh * self.input
                 + cached * (self.cached_input if self.cached_input is not None else self.input)
                 + written * (self.cache_write if self.cache_write is not None else self.input)
                 + usage.output_tokens * self.output)
        return total / 1_000_000


# Claude list prices (Anthropic model documentation). Cache writes are the 5-minute TTL rate, 1.25x input.
# Claude Haiku 5.5 is priced at its <= 100K-token-prompt tier; longer prompts cost more.
BUILTIN_PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.0, output=20.0, cached_input=0.20, cache_write=5.0),
    "claude-sonnet-5-5": Price(input=2.0, output=10.0, cached_input=0.20, cache_write=2.5),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cached_input=0.01, cache_write=0.125),
    "claude-fable-5-1": Price(input=10.0, output=50.0, cached_input=0.25, cache_write=12.5),
    "claude-opus-5": Price(input=5.0, output=25.0, cached_input=0.50, cache_write=6.25),
    "fake-1": Price(input=0.0, output=0.0),
}


def price_for(model: str | None, overrides: dict[str, Price] | None = None) -> Price | None:
    if not model:
        return None
    if overrides and model in overrides:
        return overrides[model]
    return BUILTIN_PRICES.get(model)


def cost_of(usage: Usage, model: str | None, overrides: dict[str, Price] | None = None) -> float | None:
    price = price_for(model, overrides)
    return None if price is None else round(price.cost(usage), 6)


# -- caps -------------------------------------------------------------------------

@dataclass
class Budget:
    max_usd_per_run: float | None = 2.0
    max_tokens_per_run: int | None = 600_000
    max_usd_per_day: float | None = 20.0
    max_usd_total: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Budget":
        data = dict(data or {})
        unknown = set(data) - {"max_usd_per_run", "max_tokens_per_run", "max_usd_per_day", "max_usd_total"}
        if unknown:
            raise ValueError(f"unknown budget fields {sorted(unknown)}")
        b = cls()
        for key in ("max_usd_per_run", "max_usd_per_day", "max_usd_total"):
            if key in data:
                setattr(b, key, None if data[key] is None else float(data[key]))
        if "max_tokens_per_run" in data:
            b.max_tokens_per_run = None if data["max_tokens_per_run"] is None else int(data["max_tokens_per_run"])
        return b

    def uses_dollars(self) -> bool:
        return any(v is not None for v in (self.max_usd_per_run, self.max_usd_per_day, self.max_usd_total))


def run_over_budget(budget: Budget, usage: Usage, cost: float | None) -> str | None:
    """Why a single run must stop now, or None."""
    if budget.max_tokens_per_run is not None and usage.input_tokens + usage.output_tokens >= budget.max_tokens_per_run:
        return f"token cap per run ({budget.max_tokens_per_run:,}) reached"
    if budget.max_usd_per_run is not None and cost is not None and cost >= budget.max_usd_per_run:
        return f"dollar cap per run (${budget.max_usd_per_run:.2f}) reached"
    return None


def spend(conn: sqlite3.Connection) -> dict[str, Any]:
    """Dollars and tokens spent today (UTC) and in total, with per-model and per-lane totals."""
    today = conn.execute("SELECT strftime('%Y-%m-%d','now') AS d").fetchone()["d"]
    rows = conn.execute(
        "SELECT model, lane, substr(started_at, 1, 10) AS day, COUNT(*) AS runs, "
        "COALESCE(SUM(input_tokens),0) AS tin, COALESCE(SUM(output_tokens),0) AS tout, "
        "COALESCE(SUM(cost_usd),0) AS usd, SUM(CASE WHEN cost_usd IS NULL AND status != 'running' THEN 1 ELSE 0 END) "
        "AS unpriced FROM runs GROUP BY model, lane, day").fetchall()
    out: dict[str, Any] = {"today": today, "usd_today": 0.0, "usd_total": 0.0, "tokens_today": 0, "tokens_total": 0,
                           "unpriced_runs": 0, "by_model": {}, "by_lane": {}}
    for r in rows:
        tokens = r["tin"] + r["tout"]
        out["usd_total"] += r["usd"]
        out["tokens_total"] += tokens
        out["unpriced_runs"] += r["unpriced"] or 0
        if r["day"] == today:
            out["usd_today"] += r["usd"]
            out["tokens_today"] += tokens
        m = out["by_model"].setdefault(r["model"] or "?", {"runs": 0, "usd": 0.0, "input_tokens": 0, "output_tokens": 0})
        m["runs"] += r["runs"]; m["usd"] += r["usd"]; m["input_tokens"] += r["tin"]; m["output_tokens"] += r["tout"]
        lane = out["by_lane"].setdefault(r["lane"] or "?", {"runs": 0, "usd": 0.0})
        lane["runs"] += r["runs"]; lane["usd"] += r["usd"]
    for k in ("usd_today", "usd_total"):
        out[k] = round(out[k], 4)
    for m in out["by_model"].values():
        m["usd"] = round(m["usd"], 4)
    for lane in out["by_lane"].values():
        lane["usd"] = round(lane["usd"], 4)
    return out


MIN_RUN_USD = 0.01  # do not start a run with less than this left under a dollar cap


def room(budget: Budget, conn: sqlite3.Connection, reserved_usd: float = 0.0) -> tuple[float | None, str | None]:
    """Dollars a new run may still spend under the daily and total caps (None when uncapped), after
    ``reserved_usd`` held by runs in flight, and which cap binds."""
    s = spend(conn)
    limits = []
    if budget.max_usd_per_day is not None:
        limits.append((budget.max_usd_per_day - s["usd_today"] - reserved_usd,
                       f"daily cap ${budget.max_usd_per_day:.2f} reached (spent ${s['usd_today']:.2f} today)"))
    if budget.max_usd_total is not None:
        limits.append((budget.max_usd_total - s["usd_total"] - reserved_usd,
                       f"total cap ${budget.max_usd_total:.2f} reached (spent ${s['usd_total']:.2f})"))
    if not limits:
        return None, None
    left, why = min(limits, key=lambda x: x[0])
    return max(0.0, left), why


def run_allowance(budget: Budget, conn: sqlite3.Connection, reserved_usd: float = 0.0) -> tuple[float | None, str | None]:
    """The dollar cap for the next run, or a reason no run may start. A run gets its per-run cap, reduced to
    what is left under the daily and total caps; in-flight runs hold their allowance until they finish."""
    left, why = room(budget, conn, reserved_usd)
    if left is not None and left < MIN_RUN_USD:
        return None, why if not reserved_usd else f"{why}, counting runs in flight"
    caps = [c for c in (budget.max_usd_per_run, left) if c is not None]
    return (min(caps) if caps else None), None


def can_start_run(budget: Budget, conn: sqlite3.Connection, in_flight: int = 0) -> str | None:
    """Why no new run may start now, or None (in-flight runs counted at their full per-run cap)."""
    _, why = run_allowance(budget, conn, in_flight * (budget.max_usd_per_run or 0.0))
    return why
