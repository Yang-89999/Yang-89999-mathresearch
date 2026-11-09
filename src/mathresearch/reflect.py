"""Reflection: every N runs, tidy the agenda and regenerate the world digest.

Deterministic and cheap; no model call. The digest is the one piece of
cross-question context every worker sees, so it is kept short and factual:
what has been proposed, what broke and why, which domains are starved,
how effort compares with the charter's lane shares, what it has cost.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import pricing, reading, store
from .charter import Charter

STALE_RUNS = 6         # an open question with this many runs loses priority
ABANDON_RUNS = 12      # ... and with this many is abandoned
DECAY = 0.7
PRIOR_DECAY = 0.85     # priority factor for prior-only questions when they crowd out grounded ones
DIGEST_CHARS_IN_CONTEXT = 2200
DEAD_STATUSES = ("broken", "duplicate", "abandoned")
LADDER = ["idea", "well_defined", "not_trivially_broken", "not_known", "useful_structure", "human_review"]


@dataclass
class Reflection:
    digest: str
    actions: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    digest_id: str | None = None


def reflect(conn: sqlite3.Connection, charter: Charter, workspace: Path | None = None,
            trigger: str = "schedule") -> Reflection:
    actions: list[str] = []
    actions += _mark_duplicates(conn)
    actions += _abandon_questions_on_dead_problems(conn)
    actions += _decay_or_abandon_stale(conn)
    actions += _reseed_starved_domains(conn, charter)
    actions += _balance_origins(conn, charter)
    stats = collect_stats(conn, charter)
    digest = render_digest(stats, actions)
    digest_id = store.insert(conn, "digests", run_count=stats["runs"]["total"], trigger=trigger,
                             content=digest, stats=stats, actions=actions)
    if workspace is not None:
        (workspace / "digest.md").write_text(digest, encoding="utf-8")
    return Reflection(digest=digest, actions=actions, stats=stats, digest_id=digest_id)


def latest_digest(conn: sqlite3.Connection) -> dict[str, Any] | None:
    rows = store.select(conn, "digests", order="rowid DESC", limit=1)
    return rows[0] if rows else None


def digest_for_context(conn: sqlite3.Connection, limit: int = DIGEST_CHARS_IN_CONTEXT) -> str | None:
    d = latest_digest(conn)
    if not d:
        return None
    text = d["content"]
    # Drop the housekeeping section; workers need the state, not the log.
    text = text.split("\n## Reflection actions", 1)[0].strip()
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n..."
    return text


# -- agenda hygiene ---------------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _mark_duplicates(conn: sqlite3.Connection) -> list[str]:
    actions = []
    seen: dict[str, str] = {}
    for p in store.select(conn, "problems", order="created_at"):
        key = _norm(p["name"])
        if not key:
            continue
        if key in seen and p["status"] not in ("known", *DEAD_STATUSES):
            store.update(conn, "problems", p["id"], status="duplicate", parent_id=seen[key])
            actions.append(f"marked {p['id']} '{p['name']}' duplicate of {seen[key]}")
        else:
            seen.setdefault(key, p["id"])
    return actions


def _abandon_questions_on_dead_problems(conn: sqlite3.Connection) -> list[str]:
    actions = []
    rows = conn.execute(
        "SELECT q.id, q.title, p.status AS pstatus, p.name FROM questions q JOIN problems p ON p.id = q.problem_id "
        "WHERE q.status IN ('open','blocked') AND p.status IN ('broken','duplicate','abandoned') "
        "AND q.kind != 'learn'").fetchall()
    for r in rows:
        q = store.get(conn, "questions", r["id"])
        note = f"Abandoned by reflection: problem '{r['name']}' is {r['pstatus']}."
        store.update(conn, "questions", r["id"], summary=((q.get("summary") or "") + "\n" + note).strip())
        store.close_question(conn, r["id"], status="abandoned")
        actions.append(f"abandoned question {r['id']} ({r['pstatus']} problem): {r['title'][:60]}")
    return actions


def _decay_or_abandon_stale(conn: sqlite3.Connection) -> list[str]:
    actions = []
    for q in store.select(conn, "questions", "status = 'open' AND runs >= ?", (STALE_RUNS,), order="runs DESC"):
        if q["runs"] >= ABANDON_RUNS:
            note = f"Abandoned by reflection after {q['runs']} runs without closing."
            store.update(conn, "questions", q["id"], summary=((q.get("summary") or "") + "\n" + note).strip())
            store.close_question(conn, q["id"], status="abandoned")
            actions.append(f"abandoned stale question {q['id']} after {q['runs']} runs: {q['title'][:60]}")
        else:
            new_priority = max(0.05, round(q["priority"] * DECAY, 4))
            if new_priority < q["priority"]:
                store.update(conn, "questions", q["id"], priority=new_priority)
                actions.append(f"decayed priority of {q['id']} to {new_priority} ({q['runs']} runs)")
    return actions


def _domain_key(domain: str) -> str:
    return domain.split("(")[0].strip()


def _reseed_starved_domains(conn: sqlite3.Connection, charter: Charter) -> list[str]:
    actions = []
    live = store.select(conn, "questions", "status IN ('open','blocked','running')")
    if not live and not store.select(conn, "questions", limit=1):
        return actions  # never seeded; agenda.seed handles the first questions
    for domain in charter.domains:
        key = _domain_key(domain).lower()
        if any(key in " ".join(q.get("domains") or []).lower() for q in live):
            continue
        qid = store.insert(
            conn, "questions", kind="survey", lane="harvest", status="open",
            title=f"Re-survey {_domain_key(domain)}: what remains unexplored after the first pass",
            body=(f"Domain: {domain}. Earlier questions in this domain are closed. Read their summaries "
                  f"(read_question) and the candidates they produced (list_catalog candidates), then look for "
                  f"operations and structures that were not covered. Emit candidates or close with a finding "
                  f"saying the domain is exhausted at this depth."),
            domains=[domain], priority=0.4, origin="reflection")
        actions.append(f"reseeded starved domain '{_domain_key(domain)}' as {qid}")
    return actions


def _balance_origins(conn: sqlite3.Connection, charter: Charter) -> list[str]:
    """Keep the agenda anchored: when questions from the model's prior alone exceed the charter's share of the
    open agenda while questions sparked by references or experiments wait, lower the prior ones' priority."""
    open_qs = store.select(conn, "questions", "status = 'open'")
    if not open_qs:
        return []
    prior = [q for q in open_qs if (q.get("origin") or "prior") == "prior"]
    grounded = [q for q in open_qs if q.get("origin") in reading.GROUNDED_ORIGINS]
    if not grounded:
        return []
    share = len(prior) / (len(prior) + len(grounded))  # among ideas; charter and housekeeping questions excluded
    if share <= charter.reading.max_prior_share:
        return []
    top_grounded = max(q["priority"] for q in grounded)
    changed = 0
    for q in prior:
        if q["priority"] >= top_grounded * 0.5:  # leave the already-low ones alone
            store.update(conn, "questions", q["id"], priority=max(0.05, round(q["priority"] * PRIOR_DECAY, 4)))
            changed += 1
    if not changed:
        return []
    return [f"lowered priority of {changed} prior-only questions: they are {share:.0%} of open ideas "
            f"(charter max {charter.reading.max_prior_share:.0%}) while {len(grounded)} grounded ones wait"]


# -- statistics -----------------------------------------------------------------

def collect_stats(conn: sqlite3.Connection, charter: Charter) -> dict[str, Any]:
    runs = store.select(conn, "runs")
    by_lane = Counter(r.get("lane") for r in runs)
    total = len(runs)
    usd_by_lane: Counter = Counter()
    for r in runs:
        usd_by_lane[r.get("lane")] += r.get("cost_usd") or 0.0
    lane_rows = []
    for lane, share in charter.lanes.items():
        n = by_lane.get(lane, 0)
        lane_rows.append({"lane": lane, "runs": n, "actual": round(n / total, 3) if total else 0.0,
                          "target": round(share, 3), "usd": round(usd_by_lane.get(lane, 0.0), 4)})
    tokens_in = sum(r.get("input_tokens") or 0 for r in runs)
    tokens_out = sum(r.get("output_tokens") or 0 for r in runs)
    cost = sum(r.get("cost_usd") or 0.0 for r in runs)

    problems = store.select(conn, "problems", "status != 'known'", order="created_at")
    by_status = Counter(p["status"] for p in problems)
    candidates = sorted((p for p in problems if p["status"] in LADDER),
                        key=lambda p: (-LADDER.index(p["status"]), p["created_at"]))
    broken = []
    for p in problems:
        if p["status"] == "broken":
            why = conn.execute("SELECT claim FROM findings WHERE problem_id = ? AND kind = 'negative' "
                               "ORDER BY created_at DESC LIMIT 1", (p["id"],)).fetchone()
            broken.append({"id": p["id"], "name": p["name"], "why": (why["claim"] if why else "")[:200]})

    findings = store.select(conn, "findings")
    f_kinds = Counter(f["kind"] for f in findings)
    notable = [f for f in findings if f["kind"] in ("anomaly", "novelty")][-5:]

    questions = store.select(conn, "questions")
    q_status = Counter(q["status"] for q in questions)
    q_open_lane = Counter(q["lane"] for q in questions if q["status"] == "open")
    stale = [q for q in questions if q["status"] == "open" and q["runs"] >= STALE_RUNS]
    blocked = [q for q in questions if q["status"] == "blocked"]
    live = [q for q in questions if q["status"] in ("open", "blocked", "running")]
    starved = [_domain_key(d) for d in charter.domains
               if not any(_domain_key(d).lower() in " ".join(q.get("domains") or []).lower() for q in live)]

    counts = {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
              for t in ("sources", "units", "experiments", "reading_notes")}
    open_mix = reading.origin_mix(conn, ("open", "blocked"))
    all_mix = reading.origin_mix(conn, ("open", "blocked", "running", "closed", "abandoned"))
    n_ideas = open_mix.get("prior", 0) + sum(open_mix.get(o, 0) for o in reading.GROUNDED_ORIGINS)
    useful = reading.source_usefulness(conn)
    exp_kinds = Counter(e["kind"] for e in store.select(conn, "experiments"))

    return {
        "runs": {"total": total, "failed": sum(r.get("status") == "failed" for r in runs), "lanes": lane_rows,
                 "input_tokens": tokens_in, "output_tokens": tokens_out, "cost_usd": round(cost, 4),
                 "cost_today_usd": pricing.spend(conn)["usd_today"],
                 "budget": {k: v for k, v in vars(charter.budget).items() if v is not None}},
        "problems": {"by_status": dict(by_status),
                     "candidates": [{"id": p["id"], "name": p["name"], "status": p["status"]} for p in candidates[:12]],
                     "broken": broken[:8]},
        "findings": {"by_kind": dict(f_kinds),
                     "notable": [{"id": f["id"], "kind": f["kind"], "claim": f["claim"][:200]} for f in notable]},
        "questions": {"by_status": dict(q_status), "open_by_lane": dict(q_open_lane),
                      "stale": [{"id": q["id"], "runs": q["runs"], "title": q["title"][:80]} for q in stale[:8]],
                      "blocked": [{"id": q["id"], "title": q["title"][:80]} for q in blocked[:8]],
                      "starved_domains": starved},
        "knowledge": {"sources": counts["sources"], "units": counts["units"], "experiments": counts["experiments"],
                      "experiment_kinds": dict(exp_kinds), "reading_notes": counts["reading_notes"]},
        "origins": {"open": open_mix, "all": all_mix,
                    "prior_share_open": round(open_mix.get("prior", 0) / n_ideas, 3) if n_ideas else 0.0,
                    "max_prior_share": charter.reading.max_prior_share},
        "references": {
            "useful": [r for r in useful if r["score"] > 0][:8],
            "unread": [{"id": r["id"], "title": r["title"]} for r in useful
                       if r["provided_by"] == "operator" and r["seed_status"] in ("open", "blocked", "running")][:8],
            "barren": [{"id": r["id"], "title": r["title"]} for r in useful
                       if r["score"] == 0 and r["seed_status"] in ("closed", "abandoned")][:8],
        },
    }


def render_digest(stats: dict[str, Any], actions: list[str]) -> str:
    r, p, f, q, k = stats["runs"], stats["problems"], stats["findings"], stats["questions"], stats["knowledge"]
    o = stats.get("origins") or {"open": {}, "prior_share_open": 0.0, "max_prior_share": 0.5}
    refs = stats.get("references") or {"useful": [], "unread": [], "barren": []}
    lines = ["# World digest", "",
             f"Runs: {r['total']} ({r['failed']} failed); tokens {r['input_tokens']:,} in / {r['output_tokens']:,} out"
             + (f"; cost ${r['cost_usd']:.2f}" if r["cost_usd"] else ""),
             "Lane shares (actual vs target): " + ", ".join(
                 f"{l['lane']} {l['actual']:.2f}/{l['target']:.2f}" for l in r["lanes"]),
             *([("Spend by lane: " + ", ".join(f"{l['lane']} ${l['usd']:.2f}" for l in r["lanes"])
                 + f"; today ${r['cost_today_usd']:.2f}"
                 + (f" of ${r['budget']['max_usd_per_day']:.2f}" if r["budget"].get("max_usd_per_day") else ""))]
               if r["cost_usd"] else []),
             f"Knowledge: {k['sources']} sources, {k['units']} units, {k.get('reading_notes', 0)} reading notes, "
             f"{k['experiments']} experiments"
             + (f" ({', '.join(f'{n} {kind}' for kind, n in sorted(k['experiment_kinds'].items()))})"
                if k["experiment_kinds"] else ""),
             "", "## Candidate problems"]
    if p["candidates"]:
        lines += [f"- [{c['id']}] {c['name']} — {c['status']}" for c in p["candidates"]]
    else:
        lines.append("- none alive yet")
    if p["by_status"]:
        lines.append("Status counts: " + ", ".join(f"{s} {n}" for s, n in sorted(p["by_status"].items())))
    if p["broken"]:
        lines += ["", "## Broken (do not re-propose without a new idea)"]
        lines += [f"- [{b['id']}] {b['name']}: {b['why'] or 'see findings'}" for b in p["broken"]]
    lines += ["", "## Findings"]
    lines.append("By kind: " + (", ".join(f"{s} {n}" for s, n in sorted(f["by_kind"].items())) or "none"))
    if f["notable"]:
        lines += [f"- ({n['kind']}) {n['claim']} [{n['id']}]" for n in f["notable"]]
    lines += ["", "## Agenda"]
    lines.append("Questions: " + (", ".join(f"{s} {n}" for s, n in sorted(q["by_status"].items())) or "none")
                 + "; open by lane: " + (", ".join(f"{l} {n}" for l, n in sorted(q["open_by_lane"].items())) or "none"))
    if q["starved_domains"]:
        lines.append("Starved domains (no live question): " + ", ".join(q["starved_domains"]))
    if q["blocked"]:
        lines.append("Blocked: " + "; ".join(f"[{b['id']}] {b['title']}" for b in q["blocked"]))
    if q["stale"]:
        lines.append("Stale (many runs, still open): " + "; ".join(
            f"[{s['id']}] {s['title']} ({s['runs']} runs)" for s in q["stale"]))
    if o["open"]:
        lines.append("Open questions sparked by: " + ", ".join(f"{k} {n}" for k, n in sorted(o["open"].items()))
                     + f" (prior-only share {o['prior_share_open']:.2f}, charter max {o['max_prior_share']:.2f})")
    if refs["useful"] or refs["unread"] or refs["barren"]:
        lines += ["", "## References"]
        lines += [f"- [{x['id']}] {x['title']}: {x['questions']} questions, {x['candidates']} candidates, "
                  f"{x['experiments']} experiments" for x in refs["useful"]]
        if refs["unread"]:
            lines.append("Seed reads waiting: " + "; ".join(f"[{x['id']}] {x['title']}" for x in refs["unread"]))
        if refs["barren"]:
            lines.append("Read, nothing came of it yet: " + "; ".join(f"[{x['id']}] {x['title']}" for x in refs["barren"]))
    if actions:
        lines += ["", "## Reflection actions"] + [f"- {a}" for a in actions]
    return "\n".join(lines) + "\n"


def to_json(reflection: Reflection) -> str:
    return json.dumps({"digest_id": reflection.digest_id, "actions": reflection.actions, "stats": reflection.stats},
                      indent=1, default=str)
