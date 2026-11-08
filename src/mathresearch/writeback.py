"""The structured result every worker run must return, and how it is applied.

The schema is deliberately flat and null-free so that one definition serves
the strict modes of both providers. Every claim carries its evidence; every
knowledge gap becomes a question; every candidate becomes a problem row.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import store

FINDING_KINDS = ["fact", "derivation", "experimental", "negative", "gap", "anomaly", "novelty"]
EVIDENCE_KINDS = ["unit", "source", "experiment", "reasoning"]
QUESTION_KINDS = ["learn", "explore", "investigate", "attack", "novelty_check", "survey"]
LANES = ["harvest", "deepen", "open"]

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
                },
            },
        },
    },
}


class WritebackError(ValueError):
    pass


def validate(data: Any) -> dict[str, Any]:
    """Light validation; the strict schema has already done the heavy lifting."""
    if not isinstance(data, dict):
        raise WritebackError("write-back must be an object")
    for key in ("summary", "status", "findings", "new_questions", "candidate_problems"):
        if key not in data:
            raise WritebackError(f"write-back missing key {key!r}")
    if data["status"] not in ("open", "closed", "abandoned"):
        raise WritebackError(f"bad status {data['status']!r}")
    for f in data["findings"]:
        if f["kind"] not in FINDING_KINDS or f["evidence_kind"] not in EVIDENCE_KINDS:
            raise WritebackError(f"bad finding {f}")
    for q in data["new_questions"]:
        if q["kind"] not in QUESTION_KINDS or q["lane"] not in LANES:
            raise WritebackError(f"bad question {q}")
    return data


def apply(conn: sqlite3.Connection, run_id: str, question: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Persist a validated write-back. Returns ids of what was created."""
    qid = question["id"]
    created: dict[str, list[str]] = {"findings": [], "questions": [], "problems": []}

    for f in data["findings"]:
        fid = store.insert(
            conn, "findings", question_id=qid, problem_id=question.get("problem_id"), run_id=run_id,
            kind=f["kind"], claim=f["claim"], evidence_kind=f["evidence_kind"],
            evidence_id=f.get("evidence_id") or None, evidence_note=f.get("evidence_note") or None,
            confidence=_clamp(f.get("confidence", 0.5)),
        )
        created["findings"].append(fid)

    for p in data["candidate_problems"]:
        pid = store.insert(
            conn, "problems", name=p["name"], status="idea",
            domains=[d.strip() for d in p.get("domains", "").split(",") if d.strip()],
            objects=p.get("objects"), forward_map=p.get("forward_map"), distribution=p.get("distribution"),
            hidden=p.get("hidden"), verification=p.get("verification"),
            reductions=[{"note": p["known_relatives"]}] if p.get("known_relatives") else None,
            parent_id=question.get("problem_id"), created_by_run=run_id,
        )
        created["problems"].append(pid)
        # Every new candidate gets its own well-definedness question in the deepen lane.
        nq = store.insert(
            conn, "questions", kind="investigate", lane="deepen", status="open",
            title=f"Make '{p['name']}' well defined: instance generator, forward map, verifier",
            body=(f"Candidate problem {pid} proposed from question {qid}. Write a Sage/Python instance generator "
                  f"and verifier, measure the forward-map cost, and decide whether it reaches ladder step 2."),
            problem_id=pid, domains=[d.strip() for d in p.get("domains", "").split(",") if d.strip()],
            priority=0.6, created_by_run=run_id,
        )
        created["questions"].append(nq)

    blocked = False
    for q in data["new_questions"]:
        nq = store.insert(
            conn, "questions", kind=q["kind"], lane=q["lane"], status="open", title=q["title"],
            body=q.get("body"), parent_id=qid, problem_id=question.get("problem_id"),
            domains=question.get("domains"), priority=_clamp(q.get("priority", 0.5)), created_by_run=run_id,
        )
        created["questions"].append(nq)
        if q.get("blocks_parent"):
            blocked = True

    changes: dict[str, Any] = {"summary": data["summary"], "runs": (question.get("runs") or 0) + 1}
    if data["status"] in ("closed", "abandoned"):
        store.update(conn, "questions", qid, **changes)
        store.close_question(conn, qid, status=data["status"])
    else:
        changes["status"] = "blocked" if blocked else "open"
        store.update(conn, "questions", qid, **changes)
    return created


def _clamp(x: Any) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.5
