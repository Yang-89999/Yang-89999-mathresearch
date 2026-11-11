"""The structured result every worker run must return, and how it is applied.

The schema is deliberately flat and null-free so that one definition serves
the strict modes of both providers. Every claim carries its evidence; every
knowledge gap becomes a question; every candidate becomes a problem row.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import novelty, reading, store

FINDING_KINDS = ["fact", "derivation", "experimental", "negative", "gap", "anomaly", "novelty"]
EVIDENCE_KINDS = ["unit", "source", "experiment", "reasoning"]
QUESTION_KINDS = ["learn", "explore", "investigate", "attack", "novelty_check", "survey"]
LANES = ["harvest", "deepen", "open"]
SPARKED_BY = ["reference", "experiment", "prior"]

_SPARK = {
    "sparked_by": {"type": "string", "enum": SPARKED_BY,
                   "description": "What gave you this idea: a reference you read, an experiment's result, or your "
                                  "own prior knowledge."},
    "sparked_by_id": {"type": "string",
                      "description": "The source id (reference) or experiment id (experiment); empty for prior."},
}

WRITEBACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Rewritten state of knowledge on this question after this run: what is known, "
                           "with evidence ids, what remains open, and what should happen next. Replaces the old summary.",
        },
        "status": {
            "type": "string",
            "enum": ["open", "closed", "abandoned"],
            "description": "closed when the question is answered; abandoned when it is not worth pursuing; open otherwise.",
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": FINDING_KINDS},
                    "claim": {"type": "string", "description": "One precise mathematical or computational claim."},
                    "evidence_kind": {"type": "string", "enum": EVIDENCE_KINDS},
                    "evidence_id": {"type": "string",
                                    "description": "unit id, source id or experiment id the claim rests on; empty string if reasoning only."},
                    "evidence_note": {"type": "string", "description": "Page, label or the specific output that supports the claim."},
                    "confidence": {"type": "number", "description": "0 to 1."},
                },
            },
        },
        "new_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": [k for k in QUESTION_KINDS if k != "novelty_check"]},
                    "lane": {"type": "string", "enum": LANES},
                    "title": {"type": "string"},
                    "body": {"type": "string", "description": "The question in full, including why it matters here."},
                    "blocks_parent": {"type": "boolean",
                                      "description": "true if the current question cannot proceed until this one is answered (a knowledge gap)."},
                    "priority": {"type": "number", "description": "0 to 1."},
                    "problem_id": {"type": "string",
                                   "description": "The candidate this question is about: its id, or 'new:<name>' for "
                                                  "one proposed in this write-back. Always set it on attack and "
                                                  "definition questions: the lab works on questions tied to a "
                                                  "candidate first. Empty when it is about no candidate."},
                    **_SPARK,
                },
            },
        },
        "candidate_problems": {
            "type": "array",
            "description": "New candidate problems. A candidate sparked by a reference enters play only if this "
                           "run opened that reference's text (a section or a statement, not only its card or "
                           "outline); and outside a propose question only while the charter's target of "
                           "candidates in play is not reached. Otherwise it is kept as a question.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "objects": {"type": "string", "description": "Mathematical objects involved, with the finite structure if any."},
                    "forward_map": {"type": "string", "description": "The easy direction and why it is efficient."},
                    "distribution": {"type": "string", "description": "How a random instance is sampled."},
                    "hidden": {"type": "string", "description": "What a solver must recover."},
                    "verification": {"type": "string", "description": "How a claimed solution is checked efficiently."},
                    "domains": {"type": "string", "description": "Comma-separated domain tags."},
                    "known_relatives": {"type": "string",
                                        "description": "Catalog problems this resembles or reduces to, or 'none found'."},
                    "notes": {"type": "string"},
                    **_SPARK,
                },
            },
        },
        "reading_notes": {
            "type": "array",
            "description": "One note per source you read in this run (card, outline or sections). Empty if you read nothing.",
            "items": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "prior": {"type": "string", "description": "What you expected the paper to say before reading."},
                    "decision": {"type": "string", "description": "The decision the reading was meant to change."},
                    "confirmed": {"type": "string", "description": "What matched your prior."},
                    "surprises": {"type": "string", "description": "Where the paper differed from your prior; 'none' if nothing."},
                    "hardness": {"type": "string", "description": "The hardness assumption at stake."},
                    "structure_exploited": {"type": "string",
                                            "description": "The structure an attack in the paper exploits and why it works; 'none' if no attack."},
                    "limits": {"type": "string", "description": "Limits and open problems the authors admit."},
                    "ideas": {"type": "array", "items": {"type": "string"},
                              "description": "Concrete ideas for PQC design or attack; each should also appear as a "
                                             "new question or candidate with sparked_by 'reference'."},
                    "weakness_patterns": {"type": "array", "items": {"type": "string"},
                                          "description": "Ids of weakness patterns the paper illustrates."},
                    "unit_ids": {"type": "array", "items": {"type": "string"}, "description": "Units you read."},
                },
            },
        },
        "source_cards": {
            "type": "array",
            "description": "Paper cards for sources you read that have none yet (required on a seed question).",
            "items": {
                "type": "object",
                "properties": {"source_id": {"type": "string"},
                               **{k: {"type": "string", "description": d} for k, d in reading.CARD_FIELDS.items()}},
            },
        },
        "novelty_checks": {
            "type": "array",
            "description": "Known-result checks: is a candidate a known problem in disguise? On a novelty_check "
                           "question this is the result. Only 'equivalent' rejects a candidate, and it "
                           "counts from any run; every other relation counts only there.",
            "items": {
                "type": "object",
                "properties": {
                    "problem_id": {"type": "string",
                                   "description": "A problem id, or 'new:<candidate name>' for a candidate in this write-back."},
                    "relation": {"type": "string", "enum": list(novelty.RELATIONS),
                                 "description": "; ".join(f"{k}: {v}" for k, v in novelty.RELATIONS.items())},
                    "known_problem_id": {"type": "string",
                                         "description": "The known problem's id (prob_known_...); empty only for "
                                                        "known_in_literature or no_relative_found."},
                    "argument": {"type": "string",
                                 "description": "The maps between instances, or where the literature has it, or which "
                                                "relatives you tried and why each fails."},
                },
            },
        },
        "weakness_audits": {
            "type": "array",
            "description": "Audit of candidate problems against weakness patterns (list_weakness_patterns). 'avoided' "
                           "and 'vulnerable' need an experiment id; without one they are recorded as untested.",
            "items": {
                "type": "object",
                "properties": {
                    "problem_id": {"type": "string",
                                   "description": "A problem id, or 'new:<candidate name>' for a candidate in this write-back."},
                    "pattern_id": {"type": "string"},
                    "status": {"type": "string", "enum": list(reading.AUDIT_STATUSES)},
                    "evidence_id": {"type": "string", "description": "Experiment id; empty if none."},
                    "note": {"type": "string"},
                },
            },
        },
    },
}

OPTIONAL_KEYS = ("reading_notes", "source_cards", "weakness_audits", "novelty_checks")


class WritebackError(ValueError):
    pass


def validate(data: Any) -> dict[str, Any]:
    """Light validation; the strict schema has already done the heavy lifting."""
    if not isinstance(data, dict):
        raise WritebackError("write-back must be an object")
    for key in ("summary", "status", "findings", "new_questions", "candidate_problems"):
        if key not in data:
            raise WritebackError(f"write-back missing key {key!r}")
    for key in OPTIONAL_KEYS:
        data.setdefault(key, [])
    if data["status"] not in ("open", "closed", "abandoned"):
        raise WritebackError(f"bad status {data['status']!r}")
    for f in data["findings"]:
        if f["kind"] not in FINDING_KINDS or f["evidence_kind"] not in EVIDENCE_KINDS:
            raise WritebackError(f"bad finding {f}")
    for q in data["new_questions"]:
        if q["kind"] not in QUESTION_KINDS or q["lane"] not in LANES:
            raise WritebackError(f"bad question {q}")
    for a in data["weakness_audits"]:
        if a.get("status") not in reading.AUDIT_STATUSES:
            raise WritebackError(f"bad weakness audit {a}")
    return data


_WORD = re.compile(r"[a-z]{4,}")
_COMMON = frozenset("planted recovery problem problems attack attacks random hidden from with through recover "
                    "candidate using over that this than rather beyond into their".split())


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in _WORD.findall((text or "").lower()) if w not in _COMMON}


def best_match(text: str, problems: list[dict[str, Any]]) -> str | None:
    """The candidate a question's text is about, judged by the distinctive words it shares with each
    candidate's name: one shared word when there is a single candidate, else the clear best with at least
    two. None when no candidate stands out."""
    problems = [p for p in problems if p]
    have = _words(text)
    if len(problems) == 1:
        return problems[0]["id"] if have & _words(problems[0]["name"]) else None
    scored = sorted(((len(have & _words(p["name"])), p["id"]) for p in problems), reverse=True)
    if scored and scored[0][0] >= 2 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None


def link_orphans(conn: sqlite3.Connection) -> list[str]:
    """Tie open questions that are about a candidate but carry no problem id to that candidate: questions
    opened by the run that proposed it. The lab lane works on a candidate's questions first, and about twenty
    attack questions once sat unseen because they were not tied to theirs. Returns the linked ids."""
    out = []
    for q in store.select(conn, "questions", "status IN ('open', 'blocked') AND problem_id IS NULL AND "
                          "created_by_run IS NOT NULL AND kind IN ('attack', 'investigate', 'learn')"):
        problems = store.select(conn, "problems", "created_by_run = ? AND status != 'known'", (q["created_by_run"],))
        pid = best_match(q["title"] + " " + (q.get("body") or ""), problems) if problems else None
        if pid:
            store.update(conn, "questions", q["id"], problem_id=pid)
            out.append(q["id"])
    return out


TEXT_LEVELS = ("section", "unit", "follow")
HELD_PRIORITY = 0.55


def _hold_reason(conn: sqlite3.Connection, origin: str, origin_id: str | None, reads: list[dict[str, Any]] | None,
                 room: int | None, taken: int) -> str | None:
    """Why a proposed candidate is kept as a question instead of entering play, or None.

    Two rules. A candidate sparked by a reference counts only when this run opened that reference's text (a
    section or a statement): three surveys once proposed candidates having read summary cards alone. And
    outside a propose question, candidates enter play only while fewer than the charter's target are there:
    16 were once in play against a target of 5, and none was ever defined or attacked."""
    if reads is not None and origin == "reference" and origin_id:
        src = store.get(conn, "sources", origin_id)
        readable = src and (store.select(conn, "units", "source_id = ?", (origin_id,), limit=1) or conn.execute(
            "SELECT 1 FROM source_sections WHERE source_id = ? LIMIT 1", (origin_id,)).fetchone())
        if readable and not any(r.get("source_id") == origin_id and r.get("level") in TEXT_LEVELS for r in reads):
            return (f"it names {origin_id} as its spark, but this run opened only that source's card or outline: "
                    f"read the sections it rests on, then propose it")
    if room is not None and taken >= room:
        return "enough candidates are in play already; it waits for a propose question"
    return None


def _hold(conn: sqlite3.Connection, p: dict[str, Any], why: str, qid: str, run_id: str, question: dict[str, Any],
          origin: str, origin_id: str | None) -> str:
    fields = "\n".join(f"{k}: {p[k]}" for k in ("objects", "forward_map", "distribution", "hidden", "verification",
                                               "known_relatives") if p.get(k))
    return store.insert(
        conn, "questions", kind="explore", lane="deepen" if origin == "reference" else "open", status="open",
        title=f"Idea held back: {p['name']}"[:200],
        body=f"A run proposed this candidate; it was not put in play because {why}.\n{fields}",
        parent_id=qid, domains=question.get("domains"), priority=HELD_PRIORITY, created_by_run=run_id,
        origin=origin, origin_id=origin_id)


def apply(conn: sqlite3.Connection, run_id: str, question: dict[str, Any], data: dict[str, Any],
          reads: list[dict[str, Any]] | None = None, candidate_room: int | None = None) -> dict[str, Any]:
    """``reads`` is the run's tool-side read log and ``candidate_room`` how many more candidates may enter
    play from this run; without them (None) the two holding rules are off."""
    """Persist a validated write-back. Returns ids of what was created."""
    qid = question["id"]
    created: dict[str, list[str]] = {"findings": [], "questions": [], "problems": [], "notes": [], "cards": [],
                                     "warnings": []}

    for card in data.get("source_cards", []):
        sid = reading.apply_card(conn, card, run_id)
        if sid:
            created["cards"].append(sid)
        else:
            created["warnings"].append(f"paper card for unknown source {card.get('source_id')!r} ignored")

    for n in data.get("reading_notes", []):
        sid = (n.get("source_id") or "").strip()
        if not store.get(conn, "sources", sid):
            created["warnings"].append(f"reading note for unknown source {sid!r} ignored")
            continue
        created["notes"].append(store.insert(
            conn, "reading_notes", question_id=qid, run_id=run_id, source_id=sid,
            **{k: n.get(k) or None for k in ("prior", "decision", "confirmed", "surprises", "hardness",
                                               "structure_exploited", "limits")},
            ideas=[i for i in n.get("ideas") or [] if str(i).strip()],
            patterns=[p for p in n.get("weakness_patterns") or [] if str(p).strip()],
            unit_ids=[u for u in n.get("unit_ids") or [] if str(u).strip()]))

    for f in data["findings"]:
        fid = store.insert(
            conn, "findings", question_id=qid, problem_id=question.get("problem_id"), run_id=run_id,
            kind=f["kind"], claim=f["claim"], evidence_kind=f["evidence_kind"],
            evidence_id=f.get("evidence_id") or None, evidence_note=f.get("evidence_note") or None,
            confidence=_clamp(f.get("confidence", 0.5)),
        )
        created["findings"].append(fid)

    new_problem_ids: dict[str, str] = {}
    for p in data["candidate_problems"]:
        origin, origin_id = reading.resolve_origin(conn, p.get("sparked_by", "prior"), p.get("sparked_by_id", ""))
        why = _hold_reason(conn, origin, origin_id, reads, candidate_room, len(created["problems"]))
        if why:
            created["questions"].append(_hold(conn, p, why, qid, run_id, question, origin, origin_id))
            created["warnings"].append(f"candidate '{p['name'][:60]}' kept as a question: {why}")
            continue
        pid = store.insert(
            conn, "problems", name=p["name"], status="idea",
            domains=[d.strip() for d in p.get("domains", "").split(",") if d.strip()],
            objects=p.get("objects"), forward_map=p.get("forward_map"), distribution=p.get("distribution"),
            hidden=p.get("hidden"), verification=p.get("verification"),
            reductions=[{"note": p["known_relatives"]}] if p.get("known_relatives") else None,
            parent_id=question.get("problem_id"), created_by_run=run_id,
            refs=[origin_id] if origin == "reference" else None,
        )
        created["problems"].append(pid)
        new_problem_ids[p["name"].strip().lower()] = pid
        # Every new candidate gets its own well-definedness question in the deepen lane.
        nq = store.insert(
            conn, "questions", kind="investigate", lane="deepen", status="open",
            title=f"Make '{p['name']}' well defined: instance generator, forward map, verifier",
            body=(f"Candidate problem {pid} proposed from question {qid}. Write a Sage/Python instance generator "
                  f"and verifier, measure the forward-map cost, and decide whether it reaches ladder step 2."),
            problem_id=pid, domains=[d.strip() for d in p.get("domains", "").split(",") if d.strip()],
            priority=0.6, created_by_run=run_id, origin=origin, origin_id=origin_id,
        )
        created["questions"].append(nq)
        # ... and its own known-result check, run separately: is it a known problem in disguise?
        created["questions"].append(novelty.open_check(conn, pid, run_id=run_id,
                                                       relatives_note=p.get("known_relatives") or None))

    counted = False
    for c in data.get("novelty_checks", []):
        target = (c.get("problem_id") or "").strip()
        if target.lower().startswith("new:"):
            target = new_problem_ids.get(target[4:].strip().lower(), "")
        target = target or question.get("problem_id") or ""
        warning = novelty.record(conn, target, c.get("relation", ""), (c.get("known_problem_id") or "").strip() or None,
                                 c.get("argument", ""), run_id, question)
        if warning:
            created["warnings"].append(warning)
        elif target == question.get("problem_id"):
            counted = True

    audits: dict[str, list[dict[str, Any]]] = {}
    for a in data.get("weakness_audits", []):
        target = (a.get("problem_id") or "").strip()
        if target.lower().startswith("new:"):
            target = new_problem_ids.get(target[4:].strip().lower(), "")
        target = target or question.get("problem_id") or ""
        if not target:
            created["warnings"].append(f"weakness audit without a problem ignored: {a.get('pattern_id')}")
            continue
        audits.setdefault(target, []).append(a)
    for target, entries in audits.items():
        created["warnings"] += reading.apply_audit(conn, target, entries, run_id=run_id, question_id=qid)

    blocked = False
    for q in data["new_questions"]:
        if q["kind"] == "novelty_check":
            # the system opens exactly one known-result check per candidate; a second one opened by an agent
            # repeats it and cannot count (9 of 30 runs once went to such copies)
            created["warnings"].append(f"known-result check question not opened (each candidate already has "
                                       f"one): {q['title'][:80]}")
            continue
        origin, origin_id = reading.resolve_origin(conn, q.get("sparked_by", "prior"), q.get("sparked_by_id", ""))
        about = (q.get("problem_id") or "").strip()
        if about.lower().startswith("new:"):
            about = new_problem_ids.get(about[4:].strip().lower(), "")
        if about and store.get(conn, "problems", about) is None:
            about = ""
        if not about and not question.get("problem_id") and created["problems"]:
            # a run that proposes candidates and opens questions about them: tie each to the one it names
            about = best_match(q["title"] + " " + (q.get("body") or ""),
                               [store.get(conn, "problems", x) for x in created["problems"]]) or ""
        nq = store.insert(
            conn, "questions", kind=q["kind"], lane=q["lane"], status="open", title=q["title"],
            body=q.get("body"), parent_id=qid, problem_id=about or question.get("problem_id"),
            domains=question.get("domains"), priority=_clamp(q.get("priority", 0.5)), created_by_run=run_id,
            origin=origin, origin_id=origin_id,
        )
        created["questions"].append(nq)
        if q.get("blocks_parent"):
            blocked = True

    status = data["status"]
    if question.get("kind") == "novelty_check" and question.get("problem_id") and status not in ("closed", "abandoned"):
        # A check ends with its verdict. One that keeps reopening takes the lab's runs (one check ran twice to
        # the token cap and stayed open); after two runs without a verdict the operator's own check decides.
        if counted:
            status = "closed"
        elif (question.get("runs") or 0) + 1 >= 2:
            note = ("No verdict after two check runs; nothing equivalent was established. Last summary: "
                    + " ".join(data["summary"].split())[:1200])
            novelty.record(conn, question["problem_id"], "no_relative_found", None, note, run_id, question)
            status = "closed"
            created["warnings"].append("known-result check closed after two runs without a verdict: recorded as "
                                       "no relative found, for the operator to double-check")
    if question.get("kind") == "propose" and status == "closed" and not created["problems"]:
        negative = any(f["kind"] == "negative" for f in data["findings"])
        if not negative:
            status = "open"
            why_not = ("a propose question closes with at least one candidate_problems entry, or a negative "
                       "finding saying why none of the listed ideas gives a well-posed problem")
            data = {**data, "summary": f"{data['summary']}\n[Not closed: {why_not}]".strip()}
            created["warnings"].append(f"propose question kept open: {why_not}")
    if question.get("kind") == "reread" and status == "closed":
        sid = question.get("origin_id")
        negative = any(f["kind"] == "negative" for f in data["findings"])
        sparked = [x for x in created["questions"] + created["problems"]
                   if sid and sid in ((store.get(conn, "questions", x) or {}).get("origin_id"),
                                      *((store.get(conn, "problems", x) or {}).get("refs") or []))]
        if not sparked and not negative:
            status = "open"
            why_not = ("a re-read closes with a new question or candidate sparked by this source (sparked_by "
                       "'reference', sparked_by_id the source id), or a negative finding saying the second reading "
                       "gave nothing new")
            data = {**data, "summary": f"{data['summary']}\n[Not closed: {why_not}]".strip()}
            created["warnings"].append(f"re-read kept open: {why_not}")
    if question.get("kind") == "seed" and status == "closed":
        why_not = _seed_incomplete(conn, question, data)
        if why_not:
            status = "open"
            data = {**data, "summary": f"{data['summary']}\n[Not closed: {why_not}]".strip()}
            created["warnings"].append(f"seed read kept open: {why_not}")

    changes: dict[str, Any] = {"summary": data["summary"], "runs": (question.get("runs") or 0) + 1}
    if status in ("closed", "abandoned"):
        store.update(conn, "questions", qid, **changes)
        store.close_question(conn, qid, status=status)
    else:
        changes["status"] = "blocked" if blocked else "open"
        # A question that stays open yields its lane to others: its priority decays with every run, so one
        # question cannot take run after run (a later run can raise it again through its sub-questions).
        changes["priority"] = round((question.get("priority") or 0.5) * STAY_OPEN_DECAY, 4)
        store.update(conn, "questions", qid, **changes)
    return created


STAY_OPEN_DECAY = 0.7


def _seed_incomplete(conn: sqlite3.Connection, question: dict[str, Any], data: dict[str, Any]) -> str | None:
    """A seed read closes only with a paper card and at least one question or candidate sparked by the
    reference, or a negative finding saying why the paper suggests nothing actionable."""
    sid = question.get("origin_id")
    src = store.get(conn, "sources", sid) if sid else None
    if src is None:
        return None
    if not src.get("card"):
        return "write the paper card (source_cards)"
    sparked = conn.execute("SELECT COUNT(*) AS n FROM questions WHERE origin_id = ? AND kind != 'seed'",
                           (sid,)).fetchone()["n"]
    negative = any(f["kind"] == "negative" for f in data["findings"])
    if not sparked and not negative:
        return ("open at least one question or candidate sparked by this reference (sparked_by 'reference', "
                "sparked_by_id the source id), or record a negative finding saying why it suggests nothing")
    return None


def _clamp(x: Any) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.5
