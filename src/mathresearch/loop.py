"""The research loop: pick, run, write back, repeat.

The database is the only shared state, so running several workers at once is
a matter of giving each its own connection. The main thread alone picks
questions (and marks them running before handing them out, so two workers
never get the same one), checks the spending caps, and runs the reflection
step; worker threads only run ``run_question``.
"""

from __future__ import annotations

import random
import sqlite3
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from . import agenda, db, pricing, store
from .charter import Charter
from .providers.base import Provider
from .reflect import Reflection, reflect
from .worker import RunOutcome, recover_interrupted, run_question


class UnpricedModelError(RuntimeError):
    pass


class LoopResult(list):
    """The outcomes, in completion order, plus why the loop stopped early (if it did)."""

    def __init__(self) -> None:
        super().__init__()
        self.stopped_because: str | None = None
        self.warnings: list[str] = []


def check_pricing(charter: Charter, provider: Provider, model: str | None, allow_unpriced: bool) -> str | None:
    """Refuse to start when dollar caps are set but the model has no price; return a warning otherwise."""
    model_id = model or getattr(provider, "model", None) or provider.default_model
    if pricing.price_for(model_id, charter.pricing) is not None:
        return None
    msg = (f"no price for model {model_id!r}: dollar caps cannot be enforced. Add it to the charter's pricing "
           f"section ($ per million tokens), or pass --allow-unpriced to run under the token cap alone.")
    if charter.budget.uses_dollars() and not allow_unpriced:
        raise UnpricedModelError(msg)
    return msg


def _db_path(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    path = row["file"] if row is not None else ""
    return path or None


def run_cycles(conn: sqlite3.Connection, charter: Charter, provider: Provider, workspace: Path, cycles: int,
               seed: int | None = None, model: str | None = None, max_steps: int | None = None,
               on_outcome: Callable[[RunOutcome], None] | None = None,
               on_reflection: Callable[[Reflection], None] | None = None,
               workers: int = 1, allow_unpriced: bool = False) -> LoopResult:
    """Run up to ``cycles`` worker runs, ``workers`` at a time, within the charter's budget."""
    result = LoopResult()
    warning = check_pricing(charter, provider, model, allow_unpriced)
    if warning:
        result.warnings.append(warning)
    recovered = recover_interrupted(conn)
    if recovered["runs"] or recovered["questions"]:
        result.warnings.append(
            f"recovered from an earlier stop: {len(recovered['runs'])} unfinished runs marked cancelled, "
            f"{len(recovered['questions'])} questions reopened")
    rng = random.Random(seed)
    agenda.seed(conn, charter)
    path = _db_path(conn)
    workers = max(1, int(workers))
    if workers > 1 and path is None:
        workers = 1  # an in-memory database cannot be shared between connections
        result.warnings.append("in-memory database: running sequentially")

    def after(outcome: RunOutcome) -> None:
        result.append(outcome)
        if on_outcome:
            on_outcome(outcome)
        if due_for_reflection(conn, charter):
            reflection = reflect(conn, charter, workspace)
            if on_reflection:
                on_reflection(reflection)

    if workers == 1:
        for _ in range(cycles):
            allowance, why = pricing.run_allowance(charter.budget, conn)
            if why:
                result.stopped_because = why
                break
            question = agenda.pick(conn, charter, rng)
            if question is None:
                break
            after(run_question(conn, charter, provider, question, workspace, max_steps=max_steps, model=model,
                               max_usd=allowance))
        return result

    local = threading.local()

    def worker_conn() -> sqlite3.Connection:
        if getattr(local, "conn", None) is None:
            local.conn = db.connect(path)
        return local.conn

    def job(question: dict, allowance: float | None) -> RunOutcome:
        return run_question(worker_conn(), charter, provider, question, workspace, max_steps=max_steps, model=model,
                            max_usd=allowance)

    launched = 0
    running: dict[Future, tuple[str, float]] = {}  # future -> (question id, dollars reserved)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="worker") as pool:
        while True:
            while launched < cycles and len(running) < workers and result.stopped_because is None:
                reserved = sum(r for _, r in running.values())
                allowance, why = pricing.run_allowance(charter.budget, conn, reserved)
                if why:
                    if not running:
                        result.stopped_because = why
                    break  # with runs in flight, wait: their reservations are released as they finish
                question = agenda.pick(conn, charter, rng)
                if question is None:
                    break
                store.update(conn, "questions", question["id"], status="running")  # claim it before handing out
                running[pool.submit(job, question, allowance)] = (question["id"], allowance or 0.0)
                launched += 1
            if not running:
                break
            done, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for fut in done:
                qid, _ = running.pop(fut)
                try:
                    outcome = fut.result()
                except Exception as exc:  # run_question records its own failures; this is a crash outside it
                    store.update(conn, "questions", qid, status="open")
                    result.warnings.append(f"worker crashed on {qid}: {type(exc).__name__}: {exc}")
                    continue
                after(outcome)
    return result


def due_for_reflection(conn: sqlite3.Connection, charter: Charter) -> bool:
    """True when ``reflect_every_runs`` finished runs have accumulated since the last digest."""
    every = charter.reflect_every_runs
    if every <= 0:
        return False
    total = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status != 'running'").fetchone()["n"]
    last = conn.execute("SELECT MAX(run_count) AS n FROM digests").fetchone()["n"] or 0
    return total - last >= every
