"""The structured result every worker run must return, and how it is applied.

The schema is deliberately flat and null-free so that one definition serves
the strict modes of both providers. Every claim carries its evidence; every
knowledge gap becomes a question; every candidate becomes a problem row.
"""

from __future__ import annotations

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
                    "kind": {"type": "string", "enum": QUESTION_KINDS},
                    "lane": {"type": "string", "enum": LANES},
                    "title": {"type": "string"},
                    "body": {"type": "string", "description": "The question in full, including why it matters here."},
                    "blocks_parent": {"type": "boolean",
                                      "description": "true if the current question cannot proceed until this one is answered (a knowledge gap)."},
                    "priority": {"type": "number", "description": "0 to 1."},
                    **_SPARK,
                },
            },
        },
        "candidate_problems": {
            "type": "array",
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
                           "question this is the result. 'no_relative_found' and 'generalizes' count only there; "
                           "showing a candidate is known counts from any run.",
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


def apply(conn: sqlite3.Connection, run_id: str, question: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
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

    for c in data.get("novelty_checks", []):
        target = (c.get("problem_id") or "").strip()
        if target.lower().startswith("new:"):
            target = new_problem_ids.get(target[4:].strip().lower(), "")
        target = target or question.get("problem_id") or ""
        warning = novelty.record(conn, target, c.get("relation", ""), (c.get("known_problem_id") or "").strip() or None,
                                 c.get("argument", ""), run_id, question)
        if warning:
            created["warnings"].append(warning)

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
        origin, origin_id = reading.resolve_origin(conn, q.get("sparked_by", "prior"), q.get("sparked_by_id", ""))
        nq = store.insert(
            conn, "questions", kind=q["kind"], lane=q["lane"], status="open", title=q["title"],
            body=q.get("body"), parent_id=qid, problem_id=question.get("problem_id"),
            domains=question.get("domains"), priority=_clamp(q.get("priority", 0.5)), created_by_run=run_id,
            origin=origin, origin_id=origin_id,
        )
        created["questions"].append(nq)
        if q.get("blocks_parent"):
            blocked = True

    status = data["status"]
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
