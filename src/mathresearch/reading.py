"""Purpose-driven reading.

The workers already know the field; reading gives them exact statements, results newer than their
training, and a narrower focus. So reading is organised around questions, top-down:

    L0  the paper card: what a PQC designer needs to know about the paper (written once, by a worker)
    L1  the outline: sections with the units each contains
    L2  units: exact statements, opened only when a question needs them

Every reference the operator provides gets a **seed read**: a question whose job is to read the paper as a
PQC designer and open at least one concrete question from it. Every question records its **origin** (the
reference or experiment that sparked it, or the model's own prior) so the reflection step can keep the
agenda anchored in the operator's references and in experimental facts.

Weakness patterns are the structural reasons known schemes broke; every candidate is audited against the
ones that apply to its domains.
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from typing import Any

from . import store

ORIGINS = ("charter", "operator", "reference", "experiment", "prior", "system", "reflection")
GROUNDED_ORIGINS = ("operator", "reference", "experiment")   # sparked by something outside the model's prior

CARD_FIELDS = OrderedDict([
    ("problem", "The computational problem or scheme the paper is about, in one or two sentences."),
    ("hardness", "The hardness assumption at stake and what is claimed about it."),
    ("structure", "The structure that makes the scheme work (trapdoor, group action, central map), and what is secret."),
    ("main_results", "The main theorems or algorithms, with unit ids or labels where indexed."),
    ("attacks", "Attacks presented or cited, with complexities, and the structure each exploits."),
    ("parameters", "Concrete parameters and claimed security levels, if any."),
    ("open_problems", "Limits and open problems the authors admit."),
    ("pqc_relevance", "Why this matters for designing new post-quantum hardness assumptions."),
])

AUDIT_STATUSES = ("avoided", "vulnerable", "untested", "not_applicable")

# Domain words that select weakness patterns by their applies_to tags ('generic' applies to everything).
TAG_KEYWORDS = {
    "multivariate": ("multivariate", "polynomial system", "polynomial map", "quadratic", "mq", "hfe", "uov",
                     "oil and vinegar", "matsumoto", "groebner", "algebraic geometry"),
    "codes": ("code", "decoding", "syndrome", "goppa", "reed-solomon", "mceliece"),
    "lattices": ("lattice", "lwe", "sis", "ntru", "short vector"),
    "isogenies": ("isogen", "elliptic", "supersingular", "abelian variet"),
    "group_actions": ("group action", "isomorphism", "equivalence", "orbit", "tensor", "representation",
                      "classification"),
}

SEED_PRIORITY = 0.8
TEXTBOOK_SEED_PRIORITY = 0.9    # textbooks are the main resource: their seed reads go first
ABSTRACT_SEED_PRIORITY = 0.6    # an abstract-only paper gives a thin seed read; let full texts go first


def seed_priority(src: dict[str, Any]) -> float:
    if src.get("kind") in ("book", "notes"):
        return TEXTBOOK_SEED_PRIORITY
    if src.get("format") == "html":
        return ABSTRACT_SEED_PRIORITY
    return SEED_PRIORITY
MIN_PURPOSE_CHARS = 12
SECTION_CHARS = 9000


# -- sources and seed reads -------------------------------------------------------

def seed_reference(conn: sqlite3.Connection, source_id: str, priority: float | None = None) -> str:
    """Open (or return the existing) seed-read question for a reference the operator provided. Textbooks get
    the highest priority, abstract-only papers the lowest; an open seed read is raised to its source's level."""
    src = store.get(conn, "sources", source_id)
    if src is None:
        raise KeyError(source_id)
    priority = seed_priority(src) if priority is None else priority
    existing = store.select(conn, "questions", "kind = 'seed' AND origin_id = ?", (source_id,), limit=1)
    if existing:
        q = existing[0]
        if q["status"] == "open" and (q.get("priority") or 0) < priority and not q.get("runs"):
            store.update(conn, "questions", q["id"], priority=priority)
        return q["id"]
    store.update(conn, "sources", source_id, provided_by="operator")
    return store.insert(
        conn, "questions", kind="seed", lane="harvest", status="open", priority=priority,
        title=f"Seed read: {src['title'][:120]}",
        body=(f"The operator chose source {source_id} ('{src['title']}'"
              f"{', ' + str(src['year']) if src.get('year') else ''}) as a reference. Read it as a PQC designer: "
              f"what does it make you want to try? Write its paper card, a reading note, and open at least one "
              f"concrete question or candidate sparked by it."),
        domains=src.get("domains") or None, origin="operator", origin_id=source_id)


def outline(conn: sqlite3.Connection, source_id: str) -> list[dict[str, Any]]:
    """Sections of a source in reading order, each with its units (and, for PDFs, the size of its prose)."""
    sections: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for u in store.select(conn, "units", "source_id = ?", (source_id,), order="rowid"):
        name = (u.get("anchor") or {}).get("section") or "(no section)"
        sections.setdefault(name, []).append(u)
    prose = conn.execute("SELECT title, length(text) AS n FROM source_sections WHERE source_id = ? ORDER BY idx",
                         (source_id,)).fetchall()
    if not prose:
        return [{"section": name, "units": units} for name, units in sections.items()]
    out = [{"section": r["title"], "units": sections.pop(r["title"], []), "chars": r["n"]} for r in prose]
    out += [{"section": name, "units": units} for name, units in sections.items()]
    return out


def section_text(conn: sqlite3.Connection, source_id: str, title: str) -> str | None:
    row = conn.execute("SELECT text FROM source_sections WHERE source_id = ? AND title = ?",
                       (source_id, title)).fetchone()
    return row["text"] if row else None


def card_text(conn: sqlite3.Connection, src: dict[str, Any]) -> str:
    authors = ", ".join((src.get("authors") or [])[:6])
    ids = src.get("identifiers") or {}
    ref = " ".join(f"{k}:{v}" for k, v in ids.items())
    lines = [f"[{src['id']}] {src['title']} ({src.get('year') or '?'}) {authors} {ref}".strip()]
    if src.get("provided_by"):
        lines.append(f"provided by: {src['provided_by']}")
    card = src.get("card")
    if card:
        lines += [f"{k}: {card[k]}" for k in CARD_FIELDS if card.get(k)]
    else:
        lines.append("No paper card yet: if you read this paper, write one in source_cards.")
        if src.get("abstract"):
            lines.append("abstract: " + src["abstract"][:1500])
    notes = store.select(conn, "reading_notes", "source_id = ?", (src["id"],), order="created_at DESC", limit=3)
    for n in notes:
        lines.append(f"earlier reading note [{n['id']}] for {n.get('question_id')}: surprises: "
                     f"{(n.get('surprises') or '-')[:200]}; ideas: {'; '.join(n.get('ideas') or [])[:300]}")
    return "\n".join(lines)


def read_source(conn: sqlite3.Connection, source_id: str, level: str, section: str = "",
                purpose: str = "", part: int = 1) -> str:
    src = store.get(conn, "sources", source_id)
    if src is None:
        return f"No source with id {source_id}. Use list_sources or ingest_source."
    secs = outline(conn, source_id)
    twin = None
    if src.get("format") == "html":
        from .ingest import full_text_twin
        twin = full_text_twin(conn, src)
        if twin is not None and level != "card":
            return (f"{source_id} holds only the abstract; the full text of this paper is {twin['id']} "
                    f"({twin.get('format')}). Read that one.")
    if level == "card":
        text = card_text(conn, src)
        if twin is not None:
            text += f"\nfull text: {twin['id']} holds the full text of this paper; read its outline there."
        if secs:
            text += "\nsections: " + "; ".join(f"{s['section']} ({len(s['units'])})" for s in secs[:30])
        return text
    if level == "outline":
        if not secs:
            return (f"Source {source_id} has no indexed units (format {src.get('format')}). Cite it as "
                    f"evidence_kind 'source' with a page or section, or ingest the full text.")
        lines = []
        for s in secs:
            lines.append(f"## {s['section']}" + (f"  ({s['chars']:,} characters of text)" if s.get("chars") else ""))
            lines += [f"  [{u['id']}] {u['kind']} {u.get('label') or ''} {u.get('title') or ''}: "
                      f"{u['statement'][:100]}" for u in s["units"]]
        return "\n".join(lines)
    if level == "section":
        if len((purpose or "").strip()) < MIN_PURPOSE_CHARS:
            return ("State the reading brief first: pass 'purpose' naming the decision this section will change "
                    "(e.g. 'if the rank bound needs D < q, candidate X is dead; otherwise sweep n = 20'). If you "
                    "cannot name one, you do not need to read it.")
        wanted = (section or "").strip().lower()
        chosen = [s for s in secs if wanted and s["section"].lower().startswith(wanted)] or \
            [s for s in secs if wanted and wanted in s["section"].lower()]
        if not chosen:
            return "No section matches. Sections: " + "; ".join(s["section"] for s in secs)
        if chosen[0].get("chars"):
            return _read_prose(conn, source_id, chosen, part)
        out, used = [], 0
        for s in chosen:
            out.append(f"## {s['section']}")
            for u in s["units"]:
                block = (f"[{u['id']}] {u['kind']} {u.get('label') or ''} {u.get('title') or ''}\n"
                         f"{u['statement']}")
                if used + len(block) > SECTION_CHARS:
                    out.append("... section truncated; open the remaining units with open_unit.")
                    return "\n".join(out)
                out.append(block)
                used += len(block)
        return "\n".join(out)
    return f"Unknown level {level!r}; use card, outline or section."


def _read_prose(conn: sqlite3.Connection, source_id: str, chosen: list[dict[str, Any]], part: int) -> str:
    """One part of a section's text (SECTION_CHARS each), with the ids of the units stated in it."""
    sec = chosen[0]
    text = section_text(conn, source_id, sec["section"]) or ""
    parts = max(1, -(-len(text) // SECTION_CHARS))
    part = min(max(1, int(part or 1)), parts)
    chunk = text[(part - 1) * SECTION_CHARS: part * SECTION_CHARS]
    out = [f"## {sec['section']}" + (f"  (part {part} of {parts}; pass part={part + 1} for the next)"
                                     if parts > 1 and part < parts else (f"  (part {part} of {parts})" if parts > 1 else "")),
           chunk]
    if sec["units"]:
        out.append("units stated here (cite these ids): " + ", ".join(
            f"{u['id']} {u['kind']} {u.get('label') or ''}".strip() for u in sec["units"]))
    if len(chosen) > 1:
        out.append("other matching sections: " + "; ".join(s["section"] for s in chosen[1:8]))
    return "\n".join(out)


def apply_card(conn: sqlite3.Connection, card: dict[str, Any], run_id: str) -> str | None:
    """Store a paper card written by a worker. Returns the source id, or None when it names no known source."""
    sid = (card.get("source_id") or "").strip()
    if not sid or store.get(conn, "sources", sid) is None:
        return None
    store.update(conn, "sources", sid, card={k: str(card.get(k) or "").strip() for k in CARD_FIELDS},
                 card_run=run_id)
    return sid


# -- weakness patterns ------------------------------------------------------------------

def domain_tags(domains: list[str] | None) -> set[str]:
    text = " ".join(domains or []).lower()
    return {tag for tag, words in TAG_KEYWORDS.items() if any(w in text for w in words)}


def patterns_for(conn: sqlite3.Connection, domains: list[str] | None) -> list[dict[str, Any]]:
    tags = domain_tags(domains) | {"generic"}
    rows = store.select(conn, "weakness_patterns", order="id")
    if not (tags - {"generic"}):  # domain unknown: every pattern is a candidate check
        return rows
    return [p for p in rows if tags & set(p.get("applies_to") or [])]


def apply_audit(conn: sqlite3.Connection, problem_id: str, entries: list[dict[str, Any]], *, run_id: str,
                question_id: str | None) -> list[str]:
    """Merge audit entries into a problem's weakness audit. 'avoided' and 'vulnerable' need an experiment id as
    evidence; without one they are recorded as 'untested'. A verified vulnerability is a negative finding."""
    problem = store.get(conn, "problems", problem_id)
    if problem is None:
        return [f"audit for unknown problem {problem_id} ignored"]
    audit = dict(problem.get("weakness_audit") or {})
    notes = []
    for e in entries:
        pid = (e.get("pattern_id") or "").strip()
        if store.get(conn, "weakness_patterns", pid) is None:
            notes.append(f"unknown weakness pattern {pid!r} ignored")
            continue
        status = e.get("status") if e.get("status") in AUDIT_STATUSES else "untested"
        evidence = (e.get("evidence_id") or "").strip() or None
        note = (e.get("note") or "").strip()
        if status in ("avoided", "vulnerable") and (not evidence or store.get(conn, "experiments", evidence) is None):
            notes.append(f"{pid}: '{status}' claimed without an experiment id; recorded as untested")
            note = f"claimed {status} without experiment evidence. {note}".strip()
            status, evidence = "untested", None
        if status == "not_applicable" and not note:
            note = "no reason given"
        audit[pid] = {"status": status, "evidence_id": evidence, "note": note[:500], "run_id": run_id}
        if status == "vulnerable":
            store.insert(conn, "findings", problem_id=problem_id, question_id=question_id, run_id=run_id,
                         kind="negative", claim=f"{problem['name']} shows weakness pattern {pid}: {note[:300]}",
                         evidence_kind="experiment", evidence_id=evidence, confidence=0.8)
    store.update(conn, "problems", problem_id, weakness_audit=audit)
    return notes


def audit_summary(conn: sqlite3.Connection, problem: dict[str, Any]) -> dict[str, list[str]]:
    audit = problem.get("weakness_audit") or {}
    out: dict[str, list[str]] = {s: [] for s in AUDIT_STATUSES}
    for p in patterns_for(conn, problem.get("domains")):
        out[(audit.get(p["id"]) or {}).get("status", "untested")].append(p["id"])
    return out


# -- origins and usefulness -------------------------------------------------------------

def resolve_origin(conn: sqlite3.Connection, sparked_by: str, sparked_by_id: str) -> tuple[str, str | None]:
    """Turn a worker's claim about what sparked a question into a checked (origin, origin_id)."""
    sid = (sparked_by_id or "").strip()
    if sparked_by == "reference" and sid and store.get(conn, "sources", sid):
        return "reference", sid
    if sparked_by == "experiment" and sid and store.get(conn, "experiments", sid):
        return "experiment", sid
    return "prior", None


def origin_mix(conn: sqlite3.Connection, statuses: tuple[str, ...] = ("open", "blocked")) -> dict[str, int]:
    marks = ",".join("?" for _ in statuses)
    rows = conn.execute(f"SELECT COALESCE(origin, 'prior') AS o, COUNT(*) AS n FROM questions "
                        f"WHERE status IN ({marks}) GROUP BY o", statuses).fetchall()
    return {r["o"]: r["n"] for r in rows}


def source_usefulness(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """For each source that was read or provided: what it led to (questions, candidates, experiments)."""
    rows = []
    for src in store.select(conn, "sources", order="created_at"):
        sid = src["id"]
        sparked = store.select(conn, "questions", "origin_id = ? AND kind != 'seed'", (sid,))
        qids = [q["id"] for q in sparked]
        notes = conn.execute("SELECT COUNT(*) AS n FROM reading_notes WHERE source_id = ?", (sid,)).fetchone()["n"]
        if not (sparked or notes or src.get("provided_by") == "operator"):
            continue
        candidates = sum(1 for q in sparked if q.get("problem_id") and q["kind"] == "investigate")
        experiments = 0
        if qids:
            marks = ",".join("?" for _ in qids)
            experiments = conn.execute(f"SELECT COUNT(*) AS n FROM experiments WHERE question_id IN ({marks})",
                                       qids).fetchone()["n"]
        seed = store.select(conn, "questions", "kind = 'seed' AND origin_id = ?", (sid,), limit=1)
        rows.append({"id": sid, "title": src["title"][:80], "provided_by": src.get("provided_by"),
                     "notes": notes, "questions": len(sparked), "candidates": candidates,
                     "experiments": experiments, "has_card": bool(src.get("card")),
                     "seed_status": seed[0]["status"] if seed else None,
                     "score": len(sparked) + 2 * candidates + experiments})
    rows.sort(key=lambda r: (-r["score"], r["title"]))
    return rows


