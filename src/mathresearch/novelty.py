"""The known-result check: is a candidate problem a known problem in disguise?

The first candidate the system proposed (an "arrow-mixed Kronecker transporter") was plain 3-tensor isomorphism,
and nothing noticed. Now every new candidate gets:

1. the known problems it most resembles, found by structured search over the catalog (BM25 over the problems'
   objects, forward map, hidden data and structure, plus shared domains), stored on the problem;
2. its own ``novelty_check`` question, run separately from the run that proposed it, whose job is adversarial:
   show the candidate is known (explicit maps between instances, a textbook or paper that has it).

A check's outcome is one of RELATIONS. The check is deliberately loose: ideas are gathered at scale and the
operator double-checks them by hand, so only one outcome rejects a candidate (status ``duplicate``): it is
equivalent to a known problem, with instance maps both ways that keep the distribution. A special case of a
known problem, a reduction to one, and a citation where the literature has it are recorded as relatives for
the operator and do not reject: nearly everything reduces to MQ, a planted special case (UOV inside MQ) is how
assumptions arise, and a cited object is often not the planted search problem. The first rules rejected on all
of these, and 9 of the first 10 candidates died before any attack ran; ``reconsider`` puts them back in play.

Anything other than a rejection counts only from a check made in a novelty_check run by a different run than
the one that proposed the candidate: a proposer cannot certify its own idea new. A candidate that passed the
check (verdict ``open`` or ``related``) and is not trivially broken on the ladder reaches status ``not_known``.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import store

KNOWN_RELATIONS = {
    "equivalent": "the same problem as the known one: explicit instance maps both ways that preserve the "
                  "instance distribution (the only rejection)",
}
RELATED_RELATIONS = {
    "special_case": "a special case of the known problem with extra planted structure; not a rejection: the lab "
                    "decides whether the structure makes it easier",
    "reduces_to_known": "reduces to the known problem (most problems reduce to MQ); not a rejection: it bounds "
                        "the hardness from above and names an attack to run",
    "known_in_literature": "a paper or textbook you cite studies this problem or its underlying object; not a "
                           "rejection: the citation is recorded for the operator to double-check by hand",
}
OPEN_RELATIONS = {
    "generalizes": "contains the known problem as a special case; the general case may be new",
    "no_relative_found": "no known problem relates to it after checking the closest ones",
}
RELATIONS = {**KNOWN_RELATIONS, **RELATED_RELATIONS, **OPEN_RELATIONS}

_STOP = {"the", "and", "for", "from", "with", "over", "that", "this", "where", "which", "given", "find", "into",
         "random", "uniform", "uniformly", "matrix", "matrices", "vector", "vectors", "element", "elements",
         "problem", "secret", "public", "hidden", "compute", "computing", "recover", "efficient", "efficiently",
         "polynomial", "time", "each", "some", "such", "their", "them", "then", "than", "when", "into", "under"}
FIELDS = ("name", "objects", "forward_map", "distribution", "hidden", "verification")


def _terms(text: str, limit: int = 60) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9\-]{2,}", text.lower())
    out = []
    for w in words:
        for part in w.split("-"):
            if len(part) > 2 and part not in _STOP and part not in out:
                out.append(part)
    return out[:limit]


def closest_known(conn: sqlite3.Connection, problem: dict[str, Any], k: int = 5) -> list[dict[str, Any]]:
    """The k known problems that share the most specific vocabulary and domains with a candidate. Each catalog
    problem lists the disguises it is known under (structure.also_known_as), so a candidate phrased in another
    area's words (a 'quiver representation with arrow mixing' for a 3-tensor) still meets it. A known problem
    the proposer itself named as a relative is always on the list."""
    text = " ".join(str(problem.get(f) or "") for f in FIELDS)
    relatives = " ".join(str(r.get("note") or "") for r in problem.get("reductions") or [] if isinstance(r, dict))
    text = f"{text} {relatives}"
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
    named = set()
    if relatives:
        low = relatives.lower()
        for kp in store.select(conn, "problems", "status = 'known'"):
            head = re.split(r"[(/]", kp["name"])[0].strip().lower()
            if kp["id"].lower() in low or (len(head) > 3 and head in low):
                named.add(kp["id"])
                names[kp["id"]] = kp["name"]
                scores[kp["id"]] = scores.get(kp["id"], 0.0) + 100.0
    best = sorted(scores, key=lambda i: -scores[i])[:max(k, len(named))]
    return [{"id": i, "name": names[i], "score": round(scores[i], 3)} for i in best]


def _check_body(problem: dict[str, Any], closest: list[dict[str, Any]], relatives_note: str | None) -> str:
    listing = "\n".join(f"- {c['id']}: {c['name']}" for c in closest) or "- (no catalog problem shares its vocabulary)"
    return (f"Candidate {problem['id']} ('{problem['name']}') was proposed in run {problem.get('created_by_run')}. "
            f"Your job is adversarial: try to show it is a known problem in disguise.\n"
            f"Closest known problems by structure:\n{listing}\n"
            + (f"The proposer named these relatives: {relatives_note}\n" if relatives_note else "")
            + "The only rejection is 'equivalent': instance maps both ways that keep the distribution. A special "
            "case of a known problem, a reduction to one such as MQ, or a paper that has the problem or its "
            "underlying object is not a rejection: record it as special_case, reduces_to_known or "
            "known_in_literature (with the exact citation), and say which known attack it suggests running. The "
            "operator double-checks these by hand. "
            "The list is found by shared vocabulary, so it can miss a problem stated in another area's words: "
            "also go through the whole catalog (list the known problems) and ask of each whether the candidate's "
            "objects and group action are the same up to renaming. "
            "For each, try to write explicit maps between instances in both directions (or show it is a special "
            "case). Search the textbooks first (search_units in: books), then papers and the literature. Record "
            "the outcome in novelty_checks: relation " + ", ".join(RELATIONS) + ", with the known problem id and "
            "the argument (the maps, or where the literature has it). Say 'no_relative_found' only after you "
            "have tried the closest ones; 'generalizes' needs the special case named.")


def _relatives_note(problem: dict[str, Any]) -> str | None:
    return "; ".join(r.get("note", "") for r in problem.get("reductions") or []
                     if isinstance(r, dict) and r.get("note")) or None


def open_check(conn: sqlite3.Connection, problem_id: str, run_id: str | None = None,
               relatives_note: str | None = None, priority: float = 0.9) -> str:
    """Record the closest known problems on a new candidate and open its known-result check question."""
    problem = store.get(conn, "problems", problem_id)
    closest = closest_known(conn, problem)
    store.update(conn, "problems", problem_id, novelty={"closest": closest, "checks": [], "verdict": "unchecked"})
    return store.insert(conn, "questions", kind="novelty_check", lane="deepen", status="open",
                        title=f"Known-result check: is '{problem['name'][:80]}' a known problem in disguise?",
                        body=_check_body(problem, closest, relatives_note or _relatives_note(problem)),
                        problem_id=problem_id, domains=problem.get("domains"), priority=priority,
                        created_by_run=run_id, origin="prior")


def refresh_unchecked(conn: sqlite3.Connection) -> list[str]:
    """Recompute the closest known problems of candidates whose check has not run yet (the catalog or the
    matching may have improved since), and rewrite the waiting check question. Returns the changed problem ids."""
    changed = []
    for p in store.select(conn, "problems", "novelty IS NOT NULL"):
        nov = p.get("novelty") or {}
        if nov.get("verdict") != "unchecked" or nov.get("checks"):
            continue
        closest = closest_known(conn, p)
        if [c["id"] for c in closest] == [c["id"] for c in nov.get("closest") or []]:
            continue
        store.update(conn, "problems", p["id"], novelty={**nov, "closest": closest})
        for q in store.select(conn, "questions", "kind = 'novelty_check' AND problem_id = ? AND status = 'open'",
                              (p["id"],)):
            store.update(conn, "questions", q["id"], body=_check_body(p, closest, _relatives_note(p)))
        changed.append(p["id"])
    return changed


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
    if relation not in KNOWN_RELATIONS and (question.get("kind") != "novelty_check"
                                       or run_id == problem.get("created_by_run")):
        return (f"'{relation}' on {problem_id} counts only from its known-result check question, run separately "
                f"from the run that proposed it; recorded as a note, verdict unchanged")
    nov = dict(problem.get("novelty") or {"closest": [], "checks": []})
    nov["checks"] = list(nov.get("checks") or []) + [{"relation": relation, "known_id": known_id,
                                                      "argument": argument[:2000], "run_id": run_id,
                                                      "question_id": question.get("id")}]
    changes: dict[str, Any] = {}
    if relation in KNOWN_RELATIONS or relation in RELATED_RELATIONS:
        reductions = list(problem.get("reductions") or [])
        reductions.append({"to": known_id, "relation": relation, "note": argument[:500]})
        changes["reductions"] = reductions
    if relation in KNOWN_RELATIONS:
        nov["verdict"] = "known"
        if problem["status"] not in ("known", "broken", "abandoned"):
            changes["status"] = "duplicate"
    elif nov.get("verdict") != "known":
        # a special case of, or a reduction to, a known problem is a relative, not a rejection
        nov["verdict"] = "related" if relation in RELATED_RELATIONS or nov.get("verdict") == "related" else "open"
        if problem["status"] == "not_trivially_broken":
            changes["status"] = "not_known"
    store.update(conn, "problems", problem_id, novelty=nov, **changes)
    return None


def promote_if_open(conn: sqlite3.Connection, problem_id: str) -> bool:
    """After the ladder: a candidate not trivially broken and open on novelty reaches 'not_known'."""
    problem = store.get(conn, "problems", problem_id)
    if problem and problem["status"] == "not_trivially_broken" and (problem.get("novelty") or {}).get("verdict") in PASSED:
        store.update(conn, "problems", problem_id, status="not_known")
        return True
    return False


PASSED = ("open", "related")    # verdicts that let a candidate go on: not shown to be a known problem


def reconsider(conn: sqlite3.Connection) -> list[str]:
    """Candidates rejected under the first rules (as a special case of, a reduction to, or a citation for a
    known problem) come back in play as 'related'; an 'equivalent' stands. Returns the problem ids."""
    out = []
    for p in store.select(conn, "problems", "status = 'duplicate' AND novelty IS NOT NULL"):
        nov = dict(p.get("novelty") or {})
        checks = nov.get("checks") or []
        if nov.get("verdict") != "known" or not checks or any(c["relation"] in KNOWN_RELATIONS for c in checks):
            continue
        store.update(conn, "problems", p["id"], status="idea", novelty={**nov, "verdict": "related"})
        for q in store.select(conn, "questions", "problem_id = ? AND kind = 'investigate' AND status = 'abandoned' "
                              "AND runs = 0", (p["id"],)):
            store.update(conn, "questions", q["id"], status="open", closed_at=None)
        out.append(p["id"])
    return out


CHECK_TITLE = "Known-result check:"


def drop_copies(conn: sqlite3.Connection) -> list[str]:
    """Abandon open known-result check questions that agents opened themselves before that was blocked: each
    candidate has the system's own check, and the copies repeat it without counting. Returns their ids."""
    out = []
    for q in store.select(conn, "questions", "kind = 'novelty_check' AND status IN ('open', 'blocked')"):
        if q["title"].startswith(CHECK_TITLE):
            continue
        store.update(conn, "questions", q["id"], summary=((q.get("summary") or "") + "\nAbandoned: a copy of the "
                     "candidate's own known-result check.").strip())
        store.close_question(conn, q["id"], status="abandoned")
        out.append(q["id"])
    return out


ACTIVE = ("idea", "well_defined", "not_trivially_broken", "not_known", "useful_structure", "human_review")


def backfill(conn: sqlite3.Connection) -> list[str]:
    """Open known-result checks for candidates proposed before checks existed. Returns the new question ids."""
    marks = ",".join("?" for _ in ACTIVE)
    out = []
    for p in store.select(conn, "problems", f"status IN ({marks}) AND novelty IS NULL", ACTIVE):
        out.append(open_check(conn, p["id"]))
    return out
