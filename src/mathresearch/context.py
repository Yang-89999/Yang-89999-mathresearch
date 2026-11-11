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

from . import novelty, reading, store
from .charter import Charter
from .reflect import digest_for_context

METHODOLOGY = """
# How you work

You are one research worker inside an autonomous mathematical research system. You receive one question,
tools, and the relevant state. You must end by returning the structured write-back (JSON matching the
schema you were given); it is the only thing that persists. Nothing you say outside it is kept.

Rules:
1. Read with a purpose. You already know this field; read for exact statements, results newer than your
   training, and focus, not to learn it from scratch. Orient first, cheaply: read_source 'card' (or the
   metadata and abstract when there is no card) and 'outline' (sections and the statements in each). Then,
   before opening any section or statement, state the brief: the question you serve, what you expect the
   paper to show (your prior, made specific by the outline), and the decision the reading will change. If
   you cannot name the decision, do not read further. Then open only what you need: 'section' with the
   purpose, open_unit for one statement (it brings the definitions and lemmas it uses), and follow to trace
   what builds on it, the papers it cites and its proof. If the knowledge
   base lacks a paper, find and ingest it (search_literature, ingest_source). Return a reading note for
   every source you read: where it differed from your prior, what structure its attacks exploit, and the
   ideas it gives you. Every idea becomes a new question or candidate, or you say why it is not actionable.
2. Attack before you believe. A hardness claim without an experiment is at most a hypothesis. Use run_script
   for every computational claim, run_attack to measure how standard attacks scale against a problem family,
   and ladder_evaluate to promote or demote a candidate; cite the experiment id as evidence. Scripts run in
   their own experiment directory and may write only there: never try to change the system's own code, the
   lab or any file elsewhere (it is blocked). A script may start helper Python scripts with
   subprocess.run([sys.executable, "helper.py"]) (the same limit applies to them) and the math programs, but
   no other programs, no shell and no pip install: numpy and sympy are installed. A new problem family or attack belongs in the write-back (a
   candidate problem, submit_attack), and the operator decides what goes into the lab.
3. Check the catalog. Before proposing a candidate problem, list the known problems and attacks and say which
   known problem it most resembles or reduces to. Disguised versions of MQ, MinRank, isomorphism of
   polynomials, syndrome decoding, LWE or tensor isomorphism are not discoveries. Every new candidate gets a
   separate known-result check (a novelty_check question, opened by the system: do not open another): there
   your job is to show the candidate is known, and to record the outcome in novelty_checks. Only an
   equivalence (instance maps both ways that keep the distribution) rejects it. A reduction to a known problem,
   being a special case with planted structure, or a citation is a relative to record and an attack to run in
   the lab; the operator double-checks those by hand.
4. For every mathematical operation you meet, ask: what objects, what forward computation and at what cost,
   what information is lost, is there a natural inverse, reconstruction or classification problem, does it
   live over finite fields, what is known about inverting it, does it hide an abelian group structure a
   quantum algorithm could exploit, and does it offer a trapdoor or a group action.
5. Say what sparked each new question and candidate: a reference you read (sparked_by 'reference' with the
   source id), an experiment's result ('experiment' with its id), or your own prior ('prior'). Ideas grounded
   in the references and in experiments are worth more than ideas from memory alone.
6. Audit every candidate against the weakness patterns (list_weakness_patterns): the structural reasons
   known schemes broke. Run the pattern's test and record 'avoided' or 'vulnerable' with the experiment id;
   'untested' is honest, a claim without an experiment is not accepted.
7. Preserve surprises. An unexpected connection or anomaly is a finding of kind 'anomaly' or 'novelty' even
   when its cryptographic use is unclear.
8. Every finding carries evidence: a unit id, a source id with page or label, or an experiment id.
   Reasoning-only findings must say so and carry lower confidence.
9. Be precise and brief. Exact statements, exact parameters, no filler.
10. Build on the memory. Other runs have worked on other branches of the agenda; what you see above is only the
   part linked to your question. Use search_findings before opening a question or proposing a candidate, and
   cite or contradict what is already recorded rather than rediscovering it.
11. Textbooks first. For definitions, standard theorems and classical algorithms, the classical textbooks
   in the knowledge base (search_units shows their hits first; in 'books' restricts to them) are the better
   source: settled, exact and self-contained. Go to papers for recent results, attacks and parameters.
12. A missing text never stops you. Papers on IACR ePrint arrive as abstract only, and textbooks and journal
   versions may be absent. Think anyway: you know this literature. State the result you rely on as you
   remember it, mark it 'from memory, not checked against the text', give it lower confidence, and keep
   going. When the full text would change a decision, call request_paper (citation, id if known, why); the
   operator downloads requested texts between runs. Open a 'learn' question only for material you cannot
   reconstruct at all.
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
    "novelty_check": """
# This is a known-result check
""" + novelty.SCOPE + """
""",
    "attack": """
# This is an attack question
The deliverable is a verified attack or a verified negative result, not an argument. If the target is a
candidate without a family yet (sample_instance refuses it), first give it one with define_family: without a
stored generator and verifier no attack on it can be registered and the candidate can never be marked
broken. Look before you compute: find the closest published attack on the nearest known problem
(search_units, then search_literature) and open the section that states its algorithm, once, early; say in a
reading note what you took from it or why it does not transfer. Then work in this order:
sample_instance to see the public data; run_script to compute with small instances and test hypotheses about
structure (what survives the secret affine maps, what relations hold between inputs and outputs, what
linear-algebra quantities have unexpected rank); when an algorithm recovers preimages, submit_attack with
a self-contained script on several sizes. Record each tested hypothesis as a finding with its experiment id,
including the ones that failed. Known attacks from the catalog are available through run_attack; a new
attack counts only once submit_attack has verified it.
""",
    "learn": """
# This is a learn question (pulled reading)
A parent question needs material it cannot state exactly from memory. Start from the brief: what the parent
needs, what you expect the material to say, and which decision of the parent it will change. Look in the
textbooks first (search_units in 'books'), then find papers (search_literature), ingest them (ingest_source),
and read top-down (read_source card, outline, then only the
needed sections). Return a reading note per source and a finding per fact the parent needs, with unit or source
ids. Close the question with a summary that says what changed for the parent; open a further learn question
only for what you could not obtain.
""",
    "seed": """
# This is a seed read
The operator chose this paper as a reference: it is a seed for research, not a course. Orient first: its
metadata and abstract (read_source 'card') and its outline. Then write down what you expect it to show and
what you already know of its topic (the reading note's prior), before opening any section. Then read the
sections and statements that matter for PQC design and ask: as a designer of post-quantum hardness
assumptions, what does this make me want to try? A variant that removes the structure an attack exploited, a
transfer of the hard core to another setting, an attack idea on a related scheme, a classification or
reconstruction problem hiding in a theorem. Deliver: the paper card (source_cards), a reading note, and at
least one new question or candidate with sparked_by 'reference' and this source id. If the paper suggests
nothing actionable, record a negative finding saying why. The question cannot close otherwise. If only the
abstract is stored (an ePrint paper), do the same from the abstract and what you know of the paper, mark what
comes from memory, and call request_paper when the full text would change what you conclude.
""",
    "propose": """
# This is a propose question
Too few candidate problems are in play, and everything downstream (the known-result check, the lab's attacks,
the ladder) has nothing to work on. The deliverable is one to three candidate_problems, not findings about
known problems. Work from the listed ideas: pick the most promising, read what sparked it (search_findings,
read_source), and turn it into a problem with explicit objects, forward map, instance distribution, hidden
data and verification. For each, name the closest known problem in the catalog (list the catalog first) and
state the exact structural difference: which structure that the known attacks exploit is absent or changed.
Prefer problems whose instances the lab can generate (list the lab families; a variant of a family can be
tested with run_script from the first deepen run). Audit each against the weakness patterns. Do not argue
hardness here and do not certify novelty: a separate known-result check does that. If none of the ideas
gives a well-posed problem, say why in a negative finding. Keep the run short.
""",
    "reread": """
# This is a re-read
You read this source before; your notes from then and what has happened since are in the question. The point
is not to summarise it again. Start from the results: for each failed attack, broken or surviving candidate
and negative finding listed, ask what in this source explains it, predicts it, or contradicts it, and go to
those sections (read_source outline, then the sections; they may not be the ones you read the first time).
Statements you passed over as routine may now be the ones that matter: a hypothesis a broken candidate
violated, a remark on why a method stops working, a parameter range the authors avoided. Deliver a reading
note (prior: what you expect now; surprises: what reads differently) and at least one new question or
candidate, sparked_by 'reference' with this source id, that says which result made it visible. If the second
reading gives nothing the first did not, say so in a negative finding. Keep the run short.
""",
    "survey": """
# This is a survey (browsing)
Browse widely but shallowly: paper cards and abstracts (read_source 'card', search_literature), not full
sections. Read through one lens: what is hard here, what broke and why, and which nearby problem nobody has
tried. A survey is useful only if it yields candidates or attack ideas; emit them with what sparked them.
""",
}


def system_prompt(charter: Charter, lane: str, lessons: str = "") -> str:
    """Stable-first: charter, methodology and lessons are identical for every run; lane text per lane."""
    return (charter.to_prompt() + "\n" + METHODOLOGY + ("\n" + lessons if lessons else "")
            + LANE_INSTRUCTIONS.get(lane, ""))


def user_prompt(conn: sqlite3.Connection, question: dict[str, Any], max_units: int = 8) -> str:
    parts = ["# Your question", _question_block(question)]
    if question.get("kind") in KIND_INSTRUCTIONS:
        parts.append(KIND_INSTRUCTIONS[question["kind"]].strip())

    origin_src = None
    if question.get("origin") in ("operator", "reference") and question.get("origin_id"):
        origin_src = store.get(conn, "sources", question["origin_id"])
        if origin_src:
            parts.append("# The reference behind this question (read_source for more)\n"
                         + reading.card_text(conn, origin_src))
    elif question.get("origin") == "experiment" and question.get("origin_id"):
        e = store.get(conn, "experiments", question["origin_id"])
        if e:
            parts.append(f"# The experiment that sparked this question\n[{e['id']}] {e['engine']} "
                         f"params={json.dumps(e.get('params'), default=str)[:300]} "
                         f"result={json.dumps(e.get('result'), default=str)[:600]}")

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
            parts.append(json.dumps({k: v for k, v in p.items() if v not in (None, "", [], {})
                                     and k != "weakness_audit"}, ensure_ascii=False, indent=1, default=str))
            if p["status"] != "known":
                audit = reading.audit_summary(conn, p)
                parts.append("# Weakness audit of this problem\n" + "\n".join(
                    f"- {status}: {', '.join(ids)}" for status, ids in audit.items() if ids)
                    + "\nTest the untested patterns that matter (list_weakness_patterns) and report weakness_audits.")

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

    notes = store.select(conn, "reading_notes", "question_id = ?", (question["id"],), order="created_at DESC",
                         limit=6)
    if notes:
        parts.append("# Your reading notes on this question")
        parts += [f"- [{n['id']}] source {n['source_id']}: decision: {(n.get('decision') or '-')[:160]}; "
                  f"surprises: {(n.get('surprises') or '-')[:200]}; ideas: {'; '.join(n.get('ideas') or [])[:300]}"
                  for n in notes]

    units = _related_units(conn, question, max_units)
    source_ids = [sid for sid in dict.fromkeys(u.get("source_id") for u in units)
                  if sid and (origin_src is None or sid != origin_src["id"])][:3]
    if source_ids:
        parts.append("# Papers that may be relevant (cards; read_source to go deeper)")
        for sid in source_ids:
            src = store.get(conn, "sources", sid)
            if src:
                parts.append(reading.card_text(conn, src)[:900])
    if units:
        parts.append("# Knowledge units that may be relevant (open them for full statements)")
        parts += [f"- [{u['id']}] {u['kind']} {u.get('label') or ''}: {u['statement'][:200]}" for u in units]
    else:
        parts.append("# Knowledge base\nNo indexed units match this question yet. Search with other terms, "
                     "or open a 'learn' question naming the material to ingest.")

    digest = digest_for_context(conn)
    if digest:
        parts.append("# State of the research (latest digest)\n" + digest)

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
    runs = q.get("runs") or 0
    if runs >= MANY_RUNS:
        summary += (f"\n\nThis question has had {runs} runs. Do not repeat earlier work: either settle it now "
                    "(close it with what is established, and what is not and why), or abandon it with a summary "
                    "saying what would unblock it (a paper to request_paper, an experiment, a sub-question).")
    return head + body + summary


MANY_RUNS = 3


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
