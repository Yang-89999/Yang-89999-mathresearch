"""Assemble a worker's context from persistent state.

The system prompt is built stable-first so prompt caching works: charter and
methodology (identical for every run), then the lane instructions (identical
within a lane). Everything specific to this question goes in the user turn.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import store
from .charter import Charter

METHODOLOGY = """
# How you work

You are one research worker inside an autonomous mathematical research system. You receive one question,
tools, and the relevant state. You must end by returning the structured write-back (JSON matching the
schema you were given); it is the only thing that persists. Nothing you say outside it is kept.

Rules:
1. Learn before you claim. Search the knowledge base for exact statements before relying on memory. If the
   knowledge base lacks what you need, either ingest the paper now (search_literature, then ingest_source;
   cheap, do it when you know which paper) or open a 'learn' question (blocks_parent: true when you cannot
   continue without it) instead of guessing.
2. Attack before you believe. A hardness claim without an experiment is at most a hypothesis. Use run_script
   for every computational claim, run_attack to measure how standard attacks scale against a problem family,
   and ladder_evaluate to promote or demote a candidate; cite the experiment id as evidence.
3. Check the catalog. Before proposing a candidate problem, list the known problems and attacks and say which
   known problem it most resembles or reduces to. Disguised versions of MQ, MinRank, isomorphism of
   polynomials, syndrome decoding, LWE or tensor isomorphism are not discoveries.
4. For every mathematical operation you meet, ask: what objects, what forward computation and at what cost,
   what information is lost, is there a natural inverse, reconstruction or classification problem, does it
   live over finite fields, what is known about inverting it, does it hide an abelian group structure a
   quantum algorithm could exploit, and does it offer a trapdoor or a group action.
5. Preserve surprises. An unexpected connection or anomaly is a finding of kind 'anomaly' or 'novelty' even
   when its cryptographic use is unclear.
6. Every finding carries evidence: a unit id, a source id with page or label, or an experiment id.
   Reasoning-only findings must say so and carry lower confidence.
7. Be precise and brief. Exact statements, exact parameters, no filler.
"""

LANE_INSTRUCTIONS = {
    "harvest": """
# Lane: harvest
Read and propose. Survey the material relevant to the question, extract the computational operations it
offers, and emit candidate problems in the schema. Do not try to settle hardness here; a well-posed
candidate with its closest known relative named is the deliverable. Keep runs short.
""",
    "deepen": """
# Lane: deepen
Investigate one candidate rigorously. Make it well defined (generator, forward map, verifier as code), run
the cheap attacks first (exhaustive search, linearization, Groebner at small sizes), measure how cost grows
with size, and state honestly whether it survived. A broken candidate is a useful negative result: record
why it broke as a finding.
""",
    "open": """
# Lane: open exploration
Follow mathematical interest, not the cryptographic objective. Look for unexpected relations between
structures, invariants that are computable, and classification or reconstruction problems. Record what you
find as findings and questions; do not force a cryptographic reading.
""",
}


KIND_INSTRUCTIONS = {
    "learn": """
# This is a learn question
Its job is to bring material into the knowledge base, not to do research on it. Find the papers
(search_literature), ingest them (ingest_source), check that the units you needed are there (search_units),
and record a finding per ingested source (evidence_kind 'source' or 'unit') saying what it covers and what is
still missing. Close the question when the material the parent needs is indexed; open a further learn
question only for what you could not obtain.
""",
}


def system_prompt(charter: Charter, lane: str) -> str:
    return charter.to_prompt() + "\n" + METHODOLOGY + LANE_INSTRUCTIONS.get(lane, "")


def user_prompt(conn: sqlite3.Connection, question: dict[str, Any], max_units: int = 8) -> str:
    parts = ["# Your question", _question_block(question)]
    if question.get("kind") in KIND_INSTRUCTIONS:
        parts.append(KIND_INSTRUCTIONS[question["kind"]].strip())

    chain = _parent_chain(conn, question)
    if chain:
        parts.append("# Parent questions (context, newest first)")
        parts += [_question_block(p, short=True) for p in chain]

    children = store.select(conn, "questions", "parent_id = ?", (question["id"],), order="created_at")
    if children:
        parts.append("# Child questions you opened earlier")
        parts += [f"- [{c['id']}] ({c['kind']}, {c['status']}) {c['title']}" +
                  (f"\n  summary: {c['summary'][:500]}" if c.get("summary") else "") for c in children]

    if question.get("problem_id"):
        p = store.get(conn, "problems", question["problem_id"])
        if p:
            parts.append("# Problem under investigation")
            parts.append(json.dumps({k: v for k, v in p.items() if v not in (None, "", [])},
                                    ensure_ascii=False, indent=1, default=str))

    findings = store.select(conn, "findings", "question_id = ?", (question["id"],), order="created_at DESC", limit=15)
    if findings:
        parts.append("# Findings recorded on this question so far")
        parts += [f"- [{f['id']}] ({f['kind']}, conf {f.get('confidence')}) {f['claim']} "
                  f"[evidence: {f['evidence_kind']} {f.get('evidence_id') or ''}]" for f in findings]

    experiments = store.select(conn, "experiments", "question_id = ?", (question["id"],),
                               order="created_at DESC", limit=5)
    if experiments:
        parts.append("# Recent experiments on this question")
        for e in experiments:
            parts.append(f"- [{e['id']}] {e['engine']} exit {e['exit_code']} {e['wall_seconds']}s "
                         f"result={json.dumps(e.get('result'))[:300]}")

    units = _related_units(conn, question, max_units)
    if units:
        parts.append("# Knowledge units that may be relevant (open them for full statements)")
        parts += [f"- [{u['id']}] {u['kind']} {u.get('label') or ''}: {u['statement'][:200]}" for u in units]
    else:
        parts.append("# Knowledge base\nNo indexed units match this question yet. Search with other terms, "
                     "or open a 'learn' question naming the material to ingest.")

    known = conn.execute("SELECT COUNT(*) AS n FROM problems WHERE status='known'").fetchone()["n"]
    attacks = conn.execute("SELECT COUNT(*) AS n FROM attacks").fetchone()["n"]
    parts.append(f"# Catalog\n{known} known problems and {attacks} attacks are catalogued; use list_catalog.")
    parts.append("# Deliverable\nWork with the tools as needed, then return the write-back JSON.")
    return "\n\n".join(parts)


def _question_block(q: dict[str, Any], short: bool = False) -> str:
    head = f"[{q['id']}] kind={q['kind']} lane={q['lane']} status={q['status']} runs={q.get('runs', 0)}\n{q['title']}"
    if short:
        return head + (f"\nsummary: {q['summary'][:600]}" if q.get("summary") else "")
    body = f"\n\n{q['body']}" if q.get("body") else ""
    summary = f"\n\n## Your summary from previous runs\n{q['summary']}" if q.get("summary") else \
        "\n\n(No previous runs on this question.)"
    return head + body + summary


def _parent_chain(conn: sqlite3.Connection, question: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    chain = []
    pid = question.get("parent_id")
    while pid and len(chain) < limit:
        p = store.get(conn, "questions", pid)
        if not p:
            break
        chain.append(p)
        pid = p.get("parent_id")
    return chain


_STOP = {"the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "is", "are", "with", "over", "what",
         "which", "that", "this", "its", "it", "be", "can", "does", "do", "as", "by", "from", "into", "how"}


def _related_units(conn: sqlite3.Connection, question: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    text = f"{question['title']} {question.get('body') or ''}"
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", text) if w.lower() not in _STOP]
    if not words:
        return []
    # OR the distinctive words; FTS5 needs quoting for hyphens.
    query = " OR ".join(f'"{w}"' for w in dict.fromkeys(words[:12]))
    try:
        return store.search_units(conn, query, limit=limit)
    except Exception:
        return []
