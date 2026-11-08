"""The research loop: pick, run, write back, repeat.

Sequential for now; the database is the only shared state, so concurrency
later is a matter of running several ``run_question`` calls at once.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from typing import Callable

from . import agenda
from .charter import Charter
from .providers.base import Provider
from .reflect import Reflection, reflect
from .worker import RunOutcome, run_question


def run_cycles(conn: sqlite3.Connection, charter: Charter, provider: Provider, workspace: Path, cycles: int,
               seed: int | None = None, model: str | None = None, max_steps: int = 12,
               on_outcome: Callable[[RunOutcome], None] | None = None,
               on_reflection: Callable[[Reflection], None] | None = None) -> list[RunOutcome]:
    rng = random.Random(seed)
    agenda.seed(conn, charter)
    outcomes: list[RunOutcome] = []
    for _ in range(cycles):
        question = agenda.pick(conn, charter, rng)
        if question is None:
            break
        outcome = run_question(conn, charter, provider, question, workspace, max_steps=max_steps, model=model)
        outcomes.append(outcome)
        if on_outcome:
            on_outcome(outcome)
        if due_for_reflection(conn, charter):
            reflection = reflect(conn, charter, workspace)
            if on_reflection:
                on_reflection(reflection)
    return outcomes


def due_for_reflection(conn: sqlite3.Connection, charter: Charter) -> bool:
    """True when ``reflect_every_runs`` finished runs have accumulated since the last digest."""
    every = charter.reflect_every_runs
    if every <= 0:
        return False
    total = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status != 'running'").fetchone()["n"]
    last = conn.execute("SELECT MAX(run_count) AS n FROM digests").fetchone()["n"] or 0
    return total - last >= every
