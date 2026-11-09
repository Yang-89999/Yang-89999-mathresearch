"""Lessons from the published work of PQC experts, and timely reviews of candidates through them.

The lessons (catalog/expert_lessons.yaml) are paraphrased principles with references. They reach the
workers in two ways:

* every worker prompt carries a short "lessons from the field" block, so the principles are always in mind;
* the reflection step opens a **review** question when a candidate reaches a new ladder status: one expert's
  lessons are checked against the candidate, reading their papers where needed. Experts rotate per
  candidate, preferring those whose lessons apply to its domains.

Reviews argue from what the papers say. Workers must not speak for the authors or invent their opinions.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

from . import reading, store

REVIEW_STATUSES = ("idea", "well_defined", "not_trivially_broken", "not_known", "useful_structure")
REVIEW_PRIORITY = 0.75
CHECK_CHARS_IN_PROMPT = 200


def all_lessons(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return store.select(conn, "lessons", order="rowid")


def lessons_for(conn: sqlite3.Connection, domains: list[str] | None,
                expert: str | None = None) -> list[dict[str, Any]]:
    tags = reading.domain_tags(domains) | {"generic"}
    out = []
    for les in all_lessons(conn):
        if expert and expert not in (les.get("experts") or []):
            continue
        if tags & set(les.get("applies_to") or []):
            out.append(les)
    return out


def experts(conn: sqlite3.Connection) -> list[str]:
    names: dict[str, None] = {}
    for les in all_lessons(conn):
        for e in les.get("experts") or []:
            names.setdefault(e, None)
    return list(names)


def prompt_block(conn: sqlite3.Connection) -> str:
    """The lessons as a stable block for the system prompt ('' when none are loaded)."""
    rows = all_lessons(conn)
    if not rows:
        return ""
    names = experts(conn)
    lines = ["# Lessons from the field",
             f"Principles drawn from the published work of {_join(names)} (our paraphrase; cite their papers, never "
             f"speak for the authors). Apply the ones that fit your question; list_lessons shows them in full."]
    for les in rows:
        check = " ".join((les.get("check_text") or "").split())
        if len(check) > CHECK_CHARS_IN_PROMPT:
            check = check[:CHECK_CHARS_IN_PROMPT].rsplit(" ", 1)[0] + " ..."
        lines.append(f"- [{les['id']}] {les['title']} ({', '.join(les.get('experts') or [])}): {check}")
    return "\n".join(lines) + "\n"


def render(les: dict[str, Any]) -> str:
    refs = "; ".join(les.get("refs") or [])
    src = ", ".join(les.get("sources") or [])
    return (f"[{les['id']}] {les['title']} ({', '.join(les.get('experts') or [])})\n"
            f"  lesson: {' '.join((les.get('lesson') or '').split())}\n"
            f"  check: {' '.join((les.get('check_text') or '').split())}\n"
            f"  refs: {refs}" + (f"\n  ingest: {src}" if src else ""))


# -- timely reviews ------------------------------------------------------------------

def _pick_expert(conn: sqlite3.Connection, problem: dict[str, Any], used: Counter) -> tuple[str, list[dict]] | None:
    best = None
    for name in experts(conn):
        applicable = lessons_for(conn, problem.get("domains"), expert=name)
        if not applicable:
            continue
        key = (used.get(name, 0), -len(applicable), name)
        if best is None or key < best[0]:
            best = (key, name, applicable)
    return (best[1], best[2]) if best else None


def review_body(problem: dict[str, Any], expert: str, applicable: list[dict[str, Any]]) -> str:
    lessons = "\n".join(render(les) for les in applicable)
    return (f"Review candidate {problem['id']} ('{problem['name']}', status {problem['status']}) through the "
            f"lessons drawn from {expert}'s published work:\n\n{lessons}\n\n"
            f"For each lesson, decide whether the candidate respects it, with evidence: run the check where it is "
            f"computational (run_script, run_attack, submit_attack), and read the cited papers where the lesson "
            f"needs their exact content (read_source if indexed; otherwise ingest_source with the id given). "
            f"Record a finding per lesson (respected, violated, or not applicable, and why), update the weakness "
            f"audit, and open questions for every gap. A violated lesson with experimental support is a negative "
            f"finding; say whether it is fatal or fixable. Argue from what the papers say; do not speak for "
            f"{expert} or guess their opinion.")


def schedule_reviews(conn: sqlite3.Connection, charter) -> list[str]:
    """Open review questions for candidates that reached a ladder status not yet reviewed."""
    policy = charter.reviews
    if not policy.enabled or not all_lessons(conn):
        return []
    start = REVIEW_STATUSES.index(policy.from_status) if policy.from_status in REVIEW_STATUSES else 1
    statuses = REVIEW_STATUSES[start:]
    actions = []
    marks = ",".join("?" for _ in statuses)
    candidates = store.select(conn, "problems", f"status IN ({marks})", statuses, order="updated_at DESC")
    for p in candidates:
        if len(actions) >= policy.max_per_reflection:
            break
        reviews = store.select(conn, "questions", "origin = 'review' AND problem_id = ?", (p["id"],))
        if any(r["status"] in ("open", "blocked", "running") for r in reviews):
            continue
        if any((r.get("origin_id") or "").endswith(f"|{p['status']}") for r in reviews):
            continue
        used = Counter((r.get("origin_id") or "|").split("|")[0] for r in reviews)
        pick = _pick_expert(conn, p, used)
        if pick is None:
            continue
        expert, applicable = pick
        qid = store.insert(
            conn, "questions", kind="investigate", lane="deepen", status="open", priority=REVIEW_PRIORITY,
            title=f"Review '{p['name'][:80]}' against lessons from {expert}'s work",
            body=review_body(p, expert, applicable), problem_id=p["id"], domains=p.get("domains") or None,
            origin="review", origin_id=f"{expert}|{p['status']}")
        actions.append(f"opened review {qid} of {p['id']} ({p['status']}) through {expert}'s lessons")
    return actions


def review_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = store.select(conn, "questions", "origin = 'review'")
    by_status = Counter(r["status"] for r in rows)
    by_expert = Counter((r.get("origin_id") or "|").split("|")[0] for r in rows)
    return {"total": len(rows), "by_status": dict(by_status), "by_expert": dict(by_expert)}


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]
