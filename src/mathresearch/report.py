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
        return "No candidate problems in play.\n" + _settled(conn, include_dead)
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
        nov = p.get("novelty") or {}
        if nov:
            last = (nov.get("checks") or [{}])[-1]
            line = f"known-result check: {nov.get('verdict', 'unchecked')}"
            if last.get("relation"):
                line += f" ({last['relation']}{' ' + last['known_id'] if last.get('known_id') else ''})"
            if nov.get("closest"):
                line += "; closest known: " + ", ".join(c["name"] for c in nov["closest"])
            out.append(_wrap(line, indent=""))
            if nov.get("verdict") == "related" and last.get("argument"):
                out.append(_wrap(f"to double-check by hand: {last['argument'][:600]}", indent=""))
        audit = p.get("weakness_audit") or {}
        if audit:
            from collections import Counter
            counts = Counter(v.get("status") if isinstance(v, dict) else v for v in audit.values())
            out.append("weakness audit: " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items(), key=str)))
        out.append("")
    return "\n".join(out).rstrip() + "\n" + _settled(conn, include_dead)


def _settled(conn: sqlite3.Connection, already_listed: bool) -> str:
    """One line per broken, duplicate or abandoned candidate with the reason, so a rejection can be judged."""
    if already_listed:
        return ""
    rows = store.select(conn, "problems", "status IN ('broken', 'duplicate', 'abandoned')", order="created_at")
    if not rows:
        return ""
    out = ["", f"# Settled candidates ({len(rows)})", ""]
    for p in rows:
        checks = (p.get("novelty") or {}).get("checks") or []
        why = ""
        if p["status"] == "duplicate" and checks:
            c = ([x for x in checks if x.get("relation") == "equivalent"] or checks)[-1]   # the check that rejected it
            why = f"{c['relation']}{' ' + c['known_id'] if c.get('known_id') else ''}: {c.get('argument', '')[:240]}"
        elif p["status"] == "duplicate" and p.get("parent_id"):
            why = f"same as {p['parent_id']}"
        out.append(_wrap(f"- [{p['status']}] {p['name']} ({p['id']}){': ' + why if why else ''}", indent=""))
    return "\n".join(out) + "\n"


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


# -- trend ------------------------------------------------------------------------------------------------------

SURVIVED = ("not_known", "useful_structure", "human_review")          # not trivially broken and not known
NOT_TRIVIALLY_BROKEN = ("not_trivially_broken",) + SURVIVED
WELL_DEFINED = ("well_defined",) + NOT_TRIVIALLY_BROKEN


def trend_rows(conn: sqlite3.Connection, by: str = "day", batch: int = 20) -> list[dict[str, Any]]:
    """Discovery metrics per batch of runs (a day, or every ``batch`` runs). A candidate is credited to the batch
    whose run proposed it, with the status it has now."""
    runs_ = store.select(conn, "runs", order="started_at")
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for i, r in enumerate(runs_):
        key = r["started_at"][:10] if by == "day" else f"runs {i // batch * batch + 1}-{min(len(runs_), (i // batch + 1) * batch)}"
        if not groups or groups[-1][0] != key:
            groups.append((key, []))
        groups[-1][1].append(r)
    problems_by_run: dict[str, list[dict[str, Any]]] = {}
    checks_by_run: dict[str, list[dict[str, Any]]] = {}
    for p in store.select(conn, "problems", "status != 'known'"):
        problems_by_run.setdefault(p.get("created_by_run") or "", []).append(p)
        for c in (p.get("novelty") or {}).get("checks") or []:
            checks_by_run.setdefault(c.get("run_id") or "", []).append(c)
    out = []
    for key, rs in groups:
        ids = {r["id"] for r in rs}
        cands = [p for rid in ids for p in problems_by_run.get(rid, [])]
        checks = [c for rid in ids for c in checks_by_run.get(rid, [])]
        submitted = [e for e in store.select(conn, "experiments", "kind = 'attack_sweep'")
                     if e.get("run_id") in ids and (e.get("params") or {}).get("submitted")]
        solved_all = sum(1 for e in submitted if (e.get("result") or {}).get("largest_broken") is not None
                         and (e.get("result") or {}).get("largest_broken") == max((e.get("params") or {}).get("sizes") or [0]))
        findings = [f for f in store.select(conn, "findings", "kind IN ('anomaly', 'novelty')") if f.get("run_id") in ids]
        out.append({
            "batch": key, "runs": len(rs), "failed": sum(r["status"] == "failed" for r in rs),
            "cost": sum(r.get("cost_usd") or 0 for r in rs),
            "deepen_share": sum((r.get("lane") == "deepen") for r in rs) / len(rs),
            "candidates": len(cands),
            "well_defined": sum(p["status"] in WELL_DEFINED for p in cands),
            "not_trivially_broken": sum(p["status"] in NOT_TRIVIALLY_BROKEN for p in cands),
            "known_in_disguise": sum((p.get("novelty") or {}).get("verdict") == "known" for p in cands),
            "broken": sum(p["status"] == "broken" for p in cands),
            "survivors": sum(p["status"] in SURVIVED for p in cands),
            "novelty_checks": len(checks),
            "attacks_submitted": len(submitted), "attacks_solving_all_sizes": solved_all,
            "anomalies": len(findings),
        })
    return out


def trend(conn: sqlite3.Connection, by: str = "day", batch: int = 20) -> str:
    rows = trend_rows(conn, by=by, batch=batch)
    if not rows:
        return "No runs yet."
    head = ("| batch | runs (failed) | cost | deepen | candidates | well defined | not trivially broken | "
            "known in disguise | broken | survivors | novelty checks | attacks submitted (solved all sizes) | "
            "anomalies |")
    out = ["# Discovery trend", "",
           "A survivor is a candidate that is well defined, not trivially broken by the lab's attacks, and not a "
           "known problem according to a separate known-result check. Candidates count in the batch that proposed "
           "them, with their status today.", "", head, "|" + "---|" * 13]
    tot: dict[str, float] = {}
    for r in rows:
        out.append(f"| {r['batch']} | {r['runs']} ({r['failed']}) | {_money(r['cost'])} | {r['deepen_share']:.0%} | "
                   f"{r['candidates']} | {r['well_defined']} | {r['not_trivially_broken']} | {r['known_in_disguise']} | "
                   f"{r['broken']} | {r['survivors']} | {r['novelty_checks']} | {r['attacks_submitted']} "
                   f"({r['attacks_solving_all_sizes']}) | {r['anomalies']} |")
        for k, v in r.items():
            if k not in ("batch", "deepen_share"):
                tot[k] = tot.get(k, 0) + v
    per = f"{_money(tot['cost'] / tot['survivors'])} per survivor" if tot.get("survivors") else "no survivor yet"
    out += ["", f"Total: {int(tot['runs'])} runs, {_money(tot['cost'])}, {int(tot['candidates'])} candidates, "
                f"{int(tot['known_in_disguise'])} known in disguise, {int(tot['survivors'])} survivors ({per})."]
    return "\n".join(out) + "\n"
