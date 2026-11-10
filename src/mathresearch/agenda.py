"""The agenda: seeding the first questions and picking the next one.

Picking is a two-step draw: choose a lane by the charter's shares, then the
highest-priority open question in that lane (with a little randomness so the
same question does not monopolise a lane). If the lane is empty, fall back to
the others. Blocked questions are skipped until their children close.

Two rules keep the funnel fed (84 runs once produced a single candidate, whose
checks then never ran because agents filled the deepen lane with attacks on
known problems):

- candidate pressure: while too few candidates are in play, ``ensure_proposal``
  opens a 'propose' question, which goes first in the harvest lane;
- candidates first: in the deepen lane a known-result check goes before
  anything else, then the questions about a candidate still in play, and only
  then questions about known problems.
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
            domains=[domain], priority=0.5, origin="charter",
        ))
    if len(charter.domains) >= 2:
        a, b = charter.domains[0].split("(")[0].strip(), charter.domains[1].split("(")[0].strip()
        ids.append(store.insert(
            conn, "questions", kind="explore", lane="open", status="open",
            title=f"Open exploration: unexpected connections between {a} and {b}",
            body="Follow mathematical interest. Look for invariants, correspondences or constructions that link "
                 "these areas and yield something computable. Record what is interesting even without a "
                 "cryptographic reading.",
            domains=[charter.domains[0], charter.domains[1]], priority=0.5, origin="charter",
        ))
    return ids


IN_PLAY = ("idea", "well_defined", "not_trivially_broken")   # candidates the funnel is still working on
PROPOSE_PRIORITY = 0.95                                      # above every seed read


def in_play(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    marks = ",".join("?" for _ in IN_PLAY)
    return store.select(conn, "problems", f"status IN ({marks})", IN_PLAY, order="created_at")


def ensure_proposal(conn: sqlite3.Connection, charter: Charter) -> str | None:
    """Open a 'propose' question when fewer than ``candidate_target`` candidates are in play, none is already
    waiting, and ``propose_every_runs`` runs have started since the last one. Returns the new question id."""
    target = charter.candidate_target
    if target <= 0 or charter.lanes.get("harvest", 0) <= 0:
        return None
    playing = in_play(conn)
    if len(playing) >= target:
        return None
    if store.select(conn, "questions", "kind = 'propose' AND status IN ('open', 'running', 'blocked')", limit=1):
        return None
    last = conn.execute("SELECT MAX(created_at) AS t FROM questions WHERE kind = 'propose'").fetchone()["t"]
    if last is not None:
        since = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE started_at > ?", (last,)).fetchone()["n"]
        if since < charter.propose_every_runs:
            return None
    return store.insert(conn, "questions", kind="propose", lane="harvest", status="open",
                        priority=PROPOSE_PRIORITY, origin="system",
                        title=f"Propose new candidate problems ({len(playing)} in play, target {target})",
                        body=_proposal_body(conn, playing))


def _proposal_body(conn: sqlite3.Connection, playing: list[dict[str, Any]]) -> str:
    """What a proposer needs to see: the ideas waiting on the agenda, and what was already proposed."""
    sparks = store.select(
        conn, "questions", "status = 'open' AND origin IN ('reference', 'experiment') AND problem_id IS NULL",
        order="created_at DESC", limit=12)
    marks = ",".join("?" for _ in IN_PLAY)
    settled = store.select(conn, "problems", f"status NOT IN ('known', {marks})", IN_PLAY,
                           order="created_at DESC", limit=20)
    lines = ["Too few candidate problems are in play, so this run's deliverable is new candidates: one to three "
             "candidate_problems, each fully specified (objects, forward map, instance distribution, hidden data, "
             "verification), each with its closest known problem named and the exact structural difference from "
             "it stated. Start from the ideas below (follow the one you find most promising to its source with "
             "search_findings and read_source), not from a blank page.", ""]
    lines.append("Ideas from the reading and the experiments that have not become candidates yet:")
    lines += [f"- {q['id']}: {q['title']}" for q in sparks] or ["- (none recorded; use the reading notes and your own knowledge)"]
    lines += ["", "Candidates in play (do not repeat them; a variant must say what it changes and why):"]
    lines += [f"- {p['id']} [{p['status']}]: {p['name']}" for p in playing] or ["- (none)"]
    if settled:
        lines += ["", "Already settled (known in disguise, broken, or abandoned): do not propose these again:"]
        lines += [f"- {p['id']} [{p['status']}]: {p['name']}" for p in settled]
    return "\n".join(lines)


def _deepen_first(conn: sqlite3.Connection, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The deepen questions to draw from: the known-result checks if any are open, else the questions about a
    candidate in play, else all of them. The priority order is kept."""
    playing = {p["id"] for p in in_play(conn)}
    checks = [q for q in questions if q["kind"] == "novelty_check"]
    about_candidates = [q for q in questions if q.get("problem_id") in playing]
    return checks or about_candidates or questions


def pick(conn: sqlite3.Connection, charter: Charter, rng: random.Random | None = None) -> dict[str, Any] | None:
    rng = rng or random.Random()
    ensure_proposal(conn, charter)
    lanes = [lane for lane, share in charter.lanes.items() if share > 0]
    weights = [charter.lanes[lane] for lane in lanes]
    order = _weighted_order(lanes, weights, rng)
    for lane in order:
        if lane == "deepen":
            candidates = _deepen_first(conn, store.open_questions(conn, lane=lane, limit=200))[:5]
        else:
            candidates = store.open_questions(conn, lane=lane, limit=5)
            if candidates and candidates[0]["kind"] == "propose":
                return candidates[0]
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
