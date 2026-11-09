"""The known-result check: is a candidate problem a known problem in disguise?

The first candidate the system proposed (an "arrow-mixed Kronecker transporter") was plain 3-tensor isomorphism,
and nothing noticed. Now every new candidate gets:

1. the known problems it most resembles, found by structured search over the catalog (BM25 over the problems'
   objects, forward map, hidden data and structure, plus shared domains), stored on the problem;
2. its own ``novelty_check`` question, run separately from the run that proposed it, whose job is adversarial:
   show the candidate is known (explicit maps between instances, a textbook or paper that has it).

A check's outcome is one of RELATIONS. Showing it is known (equivalent, a special case, or reducing to a known
problem it is no harder than, or found in the literature) marks the candidate a duplicate. Generalizing a
known problem, or no relative found after checking the closest ones, leaves it open, and only a check made in
a novelty_check run by a different run than the one that proposed the candidate counts for that: a proposer
cannot certify its own idea new. A candidate that is open on novelty and not trivially broken on the ladder
reaches status ``not_known``.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import store

KNOWN_RELATIONS = {
    "equivalent": "equivalent to the known problem (instance maps both ways)",
    "special_case": "a special case of the known problem",
    "reduces_to_known": "reduces to the known problem, so it is no harder",
    "known_in_literature": "already studied in a paper or textbook",
}
OPEN_RELATIONS = {
    "generalizes": "contains the known problem as a special case; the general case may be new",
    "no_relative_found": "no known problem relates to it after checking the closest ones",
}
RELATIONS = {**KNOWN_RELATIONS, **OPEN_RELATIONS}

_STOP = {"the", "and", "for", "from", "with", "over", "that", "this", "where", "which", "given", "find", "into",
         "random", "uniform", "uniformly", "matrix", "matrices", "vector", "vectors", "element", "elements",
         "problem", "secret", "public", "hidden", "compute", "computing", "recover", "efficient", "efficiently",
         "polynomial", "time", "each", "some", "such", "their", "them", "then", "than", "when", "into", "under"}
FIELDS = ("name", "objects", "forward_map", "distribution", "hidden", "verification")


def _terms(text: str, limit: int = 24) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9\-]{2,}", text.lower())
    out = []
    for w in words:
        for part in w.split("-"):
            if len(part) > 2 and part not in _STOP and part not in out:
                out.append(part)
    return out[:limit]


def closest_known(conn: sqlite3.Connection, problem: dict[str, Any], k: int = 3) -> list[dict[str, Any]]:
    """The k known problems that share the most specific vocabulary and domains with a candidate."""
    text = " ".join(str(problem.get(f) or "") for f in FIELDS)
    terms = _terms(text)
    scores: dict[str, float] = {}
    names: dict[str, str] = {}
    if terms:
        query = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = conn.execute(
                "SELECT p.id, p.name, bm25(problems_fts) AS score FROM problems_fts "
                "JOIN problems p ON p.rowid = problems_fts.rowid "
                "WHERE problems_fts MATCH ? AND p.status = 'known' ORDER BY score LIMIT 20", (query,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for r in rows:
            scores[r["id"]] = -float(r["score"])           # bm25: lower is better
            names[r["id"]] = r["name"]
    domains = {d.lower() for d in problem.get("domains") or []}
    if domains:
        for kp in store.select(conn, "problems", "status = 'known'"):
            shared = domains & {d.lower() for d in kp.get("domains") or []}
            if shared:
                scores[kp["id"]] = scores.get(kp["id"], 0.0) + 0.5 * len(shared)
                names[kp["id"]] = kp["name"]
    best = sorted(scores, key=lambda i: -scores[i])[:k]
    return [{"id": i, "name": names[i], "score": round(scores[i], 3)} for i in best]


def open_check(conn: sqlite3.Connection, problem_id: str, run_id: str | None = None,
               relatives_note: str | None = None, priority: float = 0.7) -> str:
    """Record the closest known problems on a new candidate and open its known-result check question."""
    problem = store.get(conn, "problems", problem_id)
    closest = closest_known(conn, problem)
    store.update(conn, "problems", problem_id, novelty={"closest": closest, "checks": [], "verdict": "unchecked"})
    listing = "\n".join(f"- {c['id']}: {c['name']}" for c in closest) or "- (no catalog problem shares its vocabulary)"
    body = (f"Candidate {problem_id} ('{problem['name']}') was proposed in run {problem.get('created_by_run')}. "
            f"Your job is adversarial: try to show it is a known problem in disguise.\n"
            f"Closest known problems by structure:\n{listing}\n"
            + (f"The proposer named these relatives: {relatives_note}\n" if relatives_note else "")
            + "For each, try to write explicit maps between instances in both directions (or show it is a special "
            "case). Search the textbooks first (search_units in: books), then papers and the literature. Record "
            "the outcome in novelty_checks: relation " + ", ".join(RELATIONS) + ", with the known problem id and "
            "the argument (the maps, or where the literature has it). Say 'no_relative_found' only after you "
            "have tried the closest ones; 'generalizes' needs the special case named.")
    return store.insert(conn, "questions", kind="novelty_check", lane="deepen", status="open",
                        title=f"Known-result check: is '{problem['name'][:80]}' a known problem in disguise?",
                        body=body, problem_id=problem_id, domains=problem.get("domains"), priority=priority,
                        created_by_run=run_id, origin="prior")


def record(conn: sqlite3.Connection, problem_id: str, relation: str, known_id: str | None, argument: str,
           run_id: str, question: dict[str, Any]) -> str | None:
    """Apply one check. Returns a warning when it is not counted."""
    problem = store.get(conn, "problems", problem_id)
    if problem is None:
        return f"novelty check for unknown problem {problem_id!r} ignored"
    if relation not in RELATIONS:
        return f"novelty check with unknown relation {relation!r} ignored"
    argument = " ".join((argument or "").split())
    if len(argument) < 20:
        return f"novelty check on {problem_id} needs an argument (the maps or the reference); ignored"
    if known_id and store.get(conn, "problems", known_id) is None:
        known_id = None
    if relation in ("equivalent", "special_case", "reduces_to_known", "generalizes") and not known_id:
        return f"novelty check '{relation}' on {problem_id} must name the known problem id; ignored"
    if relation in OPEN_RELATIONS and (question.get("kind") != "novelty_check"
                                       or run_id == problem.get("created_by_run")):
        return (f"'{relation}' on {problem_id} counts only from its known-result check question, run separately "
                f"from the run that proposed it; recorded as a note, verdict unchanged")
    nov = dict(problem.get("novelty") or {"closest": [], "checks": []})
    nov["checks"] = list(nov.get("checks") or []) + [{"relation": relation, "known_id": known_id,
                                                      "argument": argument[:2000], "run_id": run_id,
                                                      "question_id": question.get("id")}]
    changes: dict[str, Any] = {}
    if relation in KNOWN_RELATIONS:
        nov["verdict"] = "known"
        if problem["status"] not in ("known", "broken", "abandoned"):
            changes["status"] = "duplicate"
        reductions = list(problem.get("reductions") or [])
        reductions.append({"to": known_id, "relation": relation, "note": argument[:500]})
        changes["reductions"] = reductions
    elif nov.get("verdict") != "known":
        nov["verdict"] = "open"
        if problem["status"] == "not_trivially_broken":
            changes["status"] = "not_known"
    store.update(conn, "problems", problem_id, novelty=nov, **changes)
    return None


def promote_if_open(conn: sqlite3.Connection, problem_id: str) -> bool:
    """After the ladder: a candidate not trivially broken and open on novelty reaches 'not_known'."""
    problem = store.get(conn, "problems", problem_id)
    if problem and problem["status"] == "not_trivially_broken" and (problem.get("novelty") or {}).get("verdict") == "open":
        store.update(conn, "problems", problem_id, status="not_known")
        return True
    return False


ACTIVE = ("idea", "well_defined", "not_trivially_broken", "not_known", "useful_structure", "human_review")


def backfill(conn: sqlite3.Connection) -> list[str]:
    """Open known-result checks for candidates proposed before checks existed. Returns the new question ids."""
    marks = ",".join("?" for _ in ACTIVE)
    out = []
    for p in store.select(conn, "problems", f"status IN ({marks}) AND novelty IS NULL", ACTIVE):
        note = "; ".join(r.get("note", "") for r in p.get("reductions") or [] if isinstance(r, dict)) or None
        out.append(open_check(conn, p["id"], relatives_note=note))
    return out
