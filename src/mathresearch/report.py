"""Readable reports of what the system has done: recent runs, candidate problems, the agenda.

Everything here only reads the database. The output is Markdown, so it reads well in a terminal and can be
saved with ``> report.md`` and opened in any Markdown viewer.
"""

from __future__ import annotations

import sqlite3
import textwrap
from typing import Any

from . import store

CANDIDATE_ORDER = ("human_review", "useful_structure", "not_known", "not_trivially_broken", "well_defined", "idea",
                   "broken", "duplicate", "abandoned")


def _wrap(text: Any, indent: str = "  ", width: int = 100) -> str:
    text = " ".join(str(text or "").split())
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent) if text else ""


def _money(v: float | None) -> str:
    return f"${v:.3f}" if v is not None else "unpriced"


def runs(conn: sqlite3.Connection, last: int = 5) -> str:
    """The last ``last`` runs, newest first: summary, findings, questions, candidates and reading notes."""
    rows = store.select(conn, "runs", order="started_at DESC", limit=last)
    if not rows:
        return "No runs yet. Start one with `mathresearch run`."
    out: list[str] = [f"# The last {len(rows)} runs", ""]
    for r in rows:
        q = store.get(conn, "questions", r["question_id"]) if r.get("question_id") else None
        title = q["title"] if q else "(question deleted)"
        kind = f"{q['lane']} {q['kind']}" if q else r.get("lane") or ""
        out.append(f"## [{r['status']}] {title}")
        out.append(f"{kind} | {r['id']} | {r['started_at'][:16].replace('T', ' ')} UTC | "
                   f"{(r.get('input_tokens') or 0):,}+{(r.get('output_tokens') or 0):,} tokens | "
                   f"{_money(r.get('cost_usd'))}")
        if r.get("error"):
            out.append(f"Error: {r['error']}")
        result = r.get("result") or {}
        if result.get("summary"):
            out += ["", "**Summary**", _wrap(result["summary"])]
        findings = store.select(conn, "findings", "run_id = ?", (r["id"],))
        if findings:
            out += ["", f"**Findings ({len(findings)})**"]
            for f in findings:
                conf = f" ({f['confidence']:.0%})" if f.get("confidence") is not None else ""
                ev = f"{f['evidence_kind']}" + (f" {f['evidence_id']}" if f.get("evidence_id") else "")
                out.append(f"- {f['kind']}{conf}, evidence: {ev}")
                out.append(_wrap(f["claim"], indent="  "))
        notes = store.select(conn, "reading_notes", "run_id = ?", (r["id"],))
        if notes:
            out += ["", f"**Reading notes ({len(notes)})**"]
            for n in notes:
                src = store.get(conn, "sources", n["source_id"]) if n.get("source_id") else None
                out.append(f"- {src['title'] if src else n.get('source_id') or 'source'}")
                for label, key in (("read to decide", "decision"), ("surprises", "surprises"),
                                   ("structure an attack exploits", "structure_exploited")):
                    if n.get(key):
                        out.append(_wrap(f"{label}: {n[key]}", indent="  "))
        questions = store.select(conn, "questions", "created_by_run = ?", (r["id"],))
        if questions:
            out += ["", f"**New questions ({len(questions)})**"]
            out += [f"- [{x['id']}] {x['lane']} {x['kind']}: {x['title']}" for x in questions]
        problems = store.select(conn, "problems", "created_by_run = ?", (r["id"],))
        if problems:
            out += ["", f"**New candidate problems ({len(problems)})**"]
            out += [f"- [{p['id']}] {p['name']} ({p['status']})" for p in problems]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def problems(conn: sqlite3.Connection, include_dead: bool = False) -> str:
    """Candidate problems (not the catalog of known ones), best ladder status first."""
    statuses = CANDIDATE_ORDER if include_dead else CANDIDATE_ORDER[:6]
    marks = ",".join("?" for _ in statuses)
    rows = store.select(conn, "problems", f"status IN ({marks})", statuses)
    if not rows:
        return "No candidate problems yet."
    rows.sort(key=lambda p: (CANDIDATE_ORDER.index(p["status"]), p["created_at"]))
    out = [f"# Candidate problems ({len(rows)})", ""]
    for p in rows:
        out.append(f"## [{p['status']}] {p['name']}  ({p['id']})")
        for label, key in (("objects", "objects"), ("forward map", "forward_map"), ("distribution", "distribution"),
                           ("hidden", "hidden"), ("verification", "verification"), ("quantum", "quantum_notes")):
            if p.get(key):
                out.append(_wrap(f"{label}: {p[key]}", indent=""))
        attacks = p.get("best_attacks") or []
        if attacks:
            out.append("best attacks: " + "; ".join(
                f"{a.get('attack_id', '?')} {a.get('complexity', '')}".strip() for a in attacks if isinstance(a, dict)))
        audit = p.get("weakness_audit") or {}
        if audit:
            from collections import Counter
            counts = Counter(v.get("status") if isinstance(v, dict) else v for v in audit.values())
            out.append("weakness audit: " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items(), key=str)))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def questions(conn: sqlite3.Connection, status: str = "open", limit: int = 30) -> str:
    """The agenda: questions with ``status``, highest priority first."""
    rows = store.select(conn, "questions", "status = ?", (status,), order="priority DESC, created_at", limit=limit)
    if not rows:
        return f"No {status} questions."
    out = [f"# {status.capitalize()} questions (top {len(rows)} by priority)", ""]
    for q in rows:
        out.append(f"- [{q['id']}] {q['priority']:.2f} {q['lane']} {q['kind']} (from {q.get('origin') or '?'}, "
                   f"{q['runs']} runs): {q['title']}")
    return "\n".join(out) + "\n"
