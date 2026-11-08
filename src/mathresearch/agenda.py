"""The agenda: seeding the first questions and picking the next one.

Picking is a two-step draw: choose a lane by the charter's shares, then the
highest-priority open question in that lane (with a little randomness so the
same question does not monopolise a lane). If the lane is empty, fall back to
the others. Blocked questions are skipped until their children close.
"""

from __future__ import annotations

import random
import sqlite3
from typing import Any

from . import store
from .charter import Charter


def seed(conn: sqlite3.Connection, charter: Charter) -> list[str]:
    """Create the first questions from the charter if the agenda is empty."""
    n = conn.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"]
    if n:
        return []
    ids = []
    for domain in charter.domains:
        ids.append(store.insert(
            conn, "questions", kind="survey", lane="harvest", status="open",
            title=f"Survey {domain.split('(')[0].strip()} for computational operations with hard inverses",
            body=(f"Domain: {domain}. Identify the main mathematical objects and the operations computed on them. "
                  f"For each operation with an efficient forward algorithm, state the natural inverse, "
                  f"reconstruction or classification problem, whether it lives over finite fields, what is "
                  f"known about its difficulty, and which catalogued problem it most resembles. Emit candidate "
                  f"problems and open 'learn' questions for material the knowledge base lacks."),
            domains=[domain], priority=0.5,
        ))
    if len(charter.domains) >= 2:
        a, b = charter.domains[0].split("(")[0].strip(), charter.domains[1].split("(")[0].strip()
        ids.append(store.insert(
            conn, "questions", kind="explore", lane="open", status="open",
            title=f"Open exploration: unexpected connections between {a} and {b}",
            body="Follow mathematical interest. Look for invariants, correspondences or constructions that link "
                 "these areas and yield something computable. Record what is interesting even without a "
                 "cryptographic reading.",
            domains=[charter.domains[0], charter.domains[1]], priority=0.5,
        ))
    return ids


def pick(conn: sqlite3.Connection, charter: Charter, rng: random.Random | None = None) -> dict[str, Any] | None:
    rng = rng or random.Random()
    lanes = [lane for lane, share in charter.lanes.items() if share > 0]
    weights = [charter.lanes[lane] for lane in lanes]
    order = _weighted_order(lanes, weights, rng)
    for lane in order:
        candidates = store.open_questions(conn, lane=lane, limit=5)
        if candidates:
            # Mostly the top one; sometimes another from the top few.
            if len(candidates) > 1 and rng.random() < 0.25:
                return rng.choice(candidates[1:])
            return candidates[0]
    return None


def _weighted_order(items: list[str], weights: list[float], rng: random.Random) -> list[str]:
    items, weights = list(items), list(weights)
    out = []
    while items:
        i = rng.choices(range(len(items)), weights=weights, k=1)[0]
        out.append(items.pop(i))
        weights.pop(i)
    return out
