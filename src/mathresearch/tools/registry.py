"""Tools available to a worker, as JSON-schema specs plus executors.

Executors receive the tool input and a ``ToolContext`` and return a string
the model reads. They never raise on expected failures; errors are returned
as text so the model can react.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import store
from ..providers.base import ToolSpec
from .sage_runner import run_script


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    workspace: Path
    question: dict[str, Any]
    run_id: str
    experiment_ids: list[str] = field(default_factory=list)
    script_timeout: float = 300.0
    script_memory_mb: int = 4096
    fetch: Callable[[str], bytes] | None = None   # network access for ingestion; None = real HTTP
    fetch_delay: float = 3.0
    hidden_attacks: frozenset[str] = frozenset()  # lab attacks withheld from this run (retrodiction tests)
    reads: list[dict[str, Any]] = field(default_factory=list)  # read_source calls: what was read and why


Executor = Callable[[dict[str, Any], ToolContext], str]


def _search_units(args: dict[str, Any], ctx: ToolContext) -> str:
    hits = store.search_units(ctx.conn, args["query"], limit=int(args.get("limit", 10)))
    if not hits:
        return "No knowledge units match. The knowledge base may not cover this yet; consider a 'learn' question."
    lines = []
    for u in hits:
        lines.append(f"[{u['id']}] {u['kind']} {u.get('label') or ''} {u.get('title') or ''}".strip())
        lines.append("  " + (u["statement"][:400] + ("..." if len(u["statement"]) > 400 else "")))
    return "\n".join(lines)


def _open_unit(args: dict[str, Any], ctx: ToolContext) -> str:
    u = store.get(ctx.conn, "units", args["unit_id"])
    if not u:
        return f"No unit with id {args['unit_id']}"
    src = store.get(ctx.conn, "sources", u["source_id"]) if u.get("source_id") else None
    out = {k: u.get(k) for k in ("id", "kind", "label", "title", "statement", "hypotheses", "notation",
                                  "proof_sketch", "depends_on", "anchor", "computational_notes")}
    out["source"] = {"id": src["id"], "title": src["title"], "year": src.get("year")} if src else None
    return json.dumps(out, ensure_ascii=False, indent=1, default=str)


def _run_script(args: dict[str, Any], ctx: ToolContext) -> str:
    res = run_script(args["code"], engine=args.get("engine", "auto"), timeout=ctx.script_timeout,
                     memory_mb=ctx.script_memory_mb, cwd=ctx.workspace / "experiments" / ctx.run_id)
    exp_id = store.insert(ctx.conn, "experiments", kind="script", question_id=ctx.question["id"],
                          problem_id=ctx.question.get("problem_id"), run_id=ctx.run_id,
                          params={"purpose": args.get("purpose", "")}, **res.to_experiment_row())
    ctx.experiment_ids.append(exp_id)
    status = "TIMED OUT" if res.timed_out else f"exit code {res.exit_code}"
    body = res.stdout[-6000:] if res.stdout else "(no stdout)"
    err = ("\n--- stderr ---\n" + res.stderr[-3000:]) if res.stderr.strip() else ""
    return f"experiment {exp_id} [{res.engine}] {status} in {res.wall_seconds}s\n--- stdout ---\n{body}{err}"


def _list_catalog(args: dict[str, Any], ctx: ToolContext) -> str:
    kind = args.get("kind", "problems")
    if kind == "attacks":
        rows = [r for r in store.select(ctx.conn, "attacks", order="name")
                if (r.get("implementation") or "") not in ctx.hidden_attacks]
        return "\n".join(f"[{r['id']}] {r['name']} ({r.get('family')}): exploits {r.get('exploits')}"
                         + (f" [runnable: {r['implementation']}]" if r.get("implementation") else "")
                         for r in rows) or "Attack catalog is empty."
    where = "status = 'known'" if kind == "problems" else "status != 'known'"
    rows = store.select(ctx.conn, "problems", where, order="name")
    return "\n".join(f"[{r['id']}] {r['name']} [{r['status']}]: forward {r.get('forward_map') or '?'}; "
                     f"hidden {r.get('hidden') or '?'}" for r in rows) or f"No {kind} in the catalog."


def _read_problem(args: dict[str, Any], ctx: ToolContext) -> str:
    p = store.get(ctx.conn, "problems", args["problem_id"])
    if not p:
        return f"No problem with id {args['problem_id']}"
    findings = store.select(ctx.conn, "findings", "problem_id = ?", (p["id"],), order="created_at DESC", limit=20)
    experiments = store.select(ctx.conn, "experiments", "problem_id = ?", (p["id"],), order="created_at DESC", limit=10)
    out = {k: v for k, v in p.items() if v not in (None, "", [])}
    out["recent_findings"] = [{"id": f["id"], "kind": f["kind"], "claim": f["claim"]} for f in findings]
    out["recent_experiments"] = [{"id": e["id"], "engine": e["engine"], "exit_code": e["exit_code"],
                                  "result": e.get("result")} for e in experiments]
    if p["status"] != "known":
        from ..reading import audit_summary
        out["weakness_audit_by_status"] = audit_summary(ctx.conn, p)
    return json.dumps(out, ensure_ascii=False, indent=1, default=str)


def _read_question(args: dict[str, Any], ctx: ToolContext) -> str:
    q = store.get(ctx.conn, "questions", args["question_id"])
    if not q:
        return f"No question with id {args['question_id']}"
    findings = store.select(ctx.conn, "findings", "question_id = ?", (q["id"],), order="created_at DESC", limit=20)
    out = {k: q.get(k) for k in ("id", "kind", "lane", "status", "title", "body", "summary", "parent_id", "problem_id")}
    out["findings"] = [{"id": f["id"], "kind": f["kind"], "claim": f["claim"], "evidence": f.get("evidence_id")}
                       for f in findings]
    return json.dumps(out, ensure_ascii=False, indent=1, default=str)


def _parse_sizes(text: str, default: list[int]) -> list[int]:
    try:
        sizes = [int(s) for s in str(text).replace(";", ",").split(",") if s.strip()]
    except ValueError:
        sizes = []
    return sizes or default


def _run_attack(args: dict[str, Any], ctx: ToolContext) -> str:
    from ..lab import get_attack, get_family, sweep
    from ..lab.attacks import applicable

    family_kwargs = {}
    if args.get("p"):
        family_kwargs["p"] = int(args["p"])
    if args.get("ratio"):
        family_kwargs["ratio"] = float(args["ratio"])
    try:
        family = get_family(args["family"], **family_kwargs)
    except (KeyError, TypeError, ValueError) as exc:
        return f"cannot build family: {exc}"
    sizes = _parse_sizes(args.get("sizes", ""), [4, 6, 8])
    seconds = float(args.get("seconds_per_size") or 20.0)
    seconds = min(seconds, ctx.script_timeout)
    attack_name = args.get("attack") or "all"
    if attack_name in ctx.hidden_attacks:
        return f"unknown attack {attack_name!r}"
    attacks = [a for a in applicable(family) if a.name not in ctx.hidden_attacks] if attack_name == "all" \
        else [get_attack(attack_name)]
    out = []
    for attack in attacks:
        if not attack.applies(family):
            out.append(f"{attack.name}: not applicable to {family.name} (requires {attack.requires})")
            continue
        curve = sweep(family, attack, sizes, time_limit=seconds)
        exp_id = store.insert(ctx.conn, "experiments", kind="attack_sweep", engine="python",
                              question_id=ctx.question["id"], problem_id=ctx.question.get("problem_id"),
                              run_id=ctx.run_id, code=f"lab.sweep({family.name}, {attack.name}, {sizes})",
                              code_sha256="", params={"family": family.name, "family_kwargs": family_kwargs,
                                                      "attack": attack.name, "sizes": sizes, "seconds": seconds},
                              stdout="", exit_code=0,
                              wall_seconds=sum(p["seconds"] for p in curve.points), result=curve.to_dict())
        ctx.experiment_ids.append(exp_id)
        pts = "; ".join(f"n={p['size']}: {'solved' if p['success'] else ('timeout' if p['timed_out'] else 'failed')} "
                        f"{p['seconds']:.3g}s work={p['work']}" for p in curve.points)
        out.append(f"experiment {exp_id}: {attack.name} on {family.name} -> growth {curve.growth} "
                   f"(exp_slope={curve.exp_slope}, poly_degree={curve.poly_degree}); {pts}")
    return "\n".join(out)


def _ladder_evaluate(args: dict[str, Any], ctx: ToolContext) -> str:
    from ..lab import get_family
    from ..lab.ladder import evaluate

    problem = store.get(ctx.conn, "problems", args["problem_id"])
    if not problem:
        return f"No problem with id {args['problem_id']}"
    family_name = args.get("family") or problem.get("generator_path")
    if not family_name:
        return ("This problem has no generator yet. Implement it as a family (a class with generate/forward/verify) "
                "or run experiments with run_script; the ladder needs code, not prose.")
    try:
        family = get_family(family_name)
    except KeyError as exc:
        return str(exc)
    sizes = _parse_sizes(args.get("sizes", ""), [4, 6, 8])
    result = evaluate(ctx.conn, problem["id"], family, sizes, time_limit=min(float(args.get("seconds_per_size") or 20.0),
                      ctx.script_timeout), question_id=ctx.question["id"], run_id=ctx.run_id,
                      exclude=set(ctx.hidden_attacks))
    verdicts = result.get("attacks", {}).get("verdicts", [])
    from ..reading import audit_summary
    audit = audit_summary(ctx.conn, store.get(ctx.conn, "problems", problem["id"]))
    return json.dumps({"status": result["status"], "well_defined": result["well_defined"], "verdicts": verdicts,
                       "weakness_patterns_untested": audit["untested"],
                       "weakness_patterns_vulnerable": audit["vulnerable"]}, indent=1, default=str)


def _domains_arg(args: dict[str, Any], ctx: ToolContext) -> list[str] | None:
    raw = args.get("domains") or ""
    domains = [d.strip() for d in str(raw).split(",") if d.strip()]
    return domains or (ctx.question.get("domains") or None)


def _search_literature(args: dict[str, Any], ctx: ToolContext) -> str:
    from .. import ingest
    from ..ingest import arxiv as ax

    fetch = ctx.fetch or ax.http_fetch
    hits = ingest.search(ctx.conn, args["query"], max_results=int(args.get("limit", 8)), fetch=fetch)
    if not hits:
        return "arXiv returned no results. Try other terms, the API syntax (ti:, abs:, au:, cat:cs.CR), or an ePrint id."
    lines = []
    for h in hits:
        state = f"already ingested as {h['ingested_source_id']}" if h["ingested_source_id"] else "not ingested"
        lines.append(f"arXiv:{h['arxiv_id']} ({h['year']}) {h['title']} -- {', '.join(h['authors'][:3])} "
                     f"[{', '.join(h['categories'])}] ({state})\n  {h['summary'][:300]}")
    return "\n".join(lines)


def _ingest_source(args: dict[str, Any], ctx: ToolContext) -> str:
    from .. import ingest
    from ..ingest import arxiv as ax

    fetch = ctx.fetch or ax.http_fetch
    try:
        result = ingest.ingest_reference(ctx.conn, args["reference"], ctx.workspace,
                                         domains=_domains_arg(args, ctx), fetch=fetch, delay=ctx.fetch_delay)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:  # network / parsing failures are information for the model
        return f"ingestion failed: {type(exc).__name__}: {exc}"
    src = store.get(ctx.conn, "sources", result.source_id)
    if src and not src.get("provided_by"):
        store.update(ctx.conn, "sources", result.source_id, provided_by="agent")
    text = result.summary() + "\nNext: read_source with level 'card' or 'outline', then only the sections you need."
    if result.unit_ids:
        units = [store.get(ctx.conn, "units", uid) for uid in result.unit_ids[:12]]
        text += "\n" + "\n".join(f"  [{u['id']}] {u['kind']} {u.get('label') or ''} {u.get('title') or ''}: "
                                 f"{u['statement'][:160]}" for u in units if u)
        if result.units > 12:
            text += f"\n  ... {result.units - 12} more; use search_units to find them."
    if result.units == 0 and not result.already_present:
        text += ("\nNo theorem-like environments were found. The source is stored; cite it as evidence_kind "
                 "'source' with the source id and page or section.")
    return text


def _list_sources(args: dict[str, Any], ctx: ToolContext) -> str:
    rows = store.select(ctx.conn, "sources", order="created_at DESC", limit=int(args.get("limit", 30)))
    if not rows:
        return "No sources ingested yet."
    out = []
    for r in rows:
        n = ctx.conn.execute("SELECT COUNT(*) AS n FROM units WHERE source_id = ?", (r["id"],)).fetchone()["n"]
        ids = r.get("identifiers") or {}
        ref = ids.get("arxiv") and f"arXiv:{ids['arxiv']}" or ids.get("eprint") and f"ePrint {ids['eprint']}" or ""
        card = "card" if r.get("card") else "no card"
        by = f", {r['provided_by']}" if r.get("provided_by") else ""
        out.append(f"[{r['id']}] {r['title']} ({r.get('year') or '?'}) {ref} [{r.get('format')}] {n} units, "
                   f"{card}{by}")
    return "\n".join(out)


def _read_source(args: dict[str, Any], ctx: ToolContext) -> str:
    from ..reading import read_source
    level = args.get("level") or "card"
    text = read_source(ctx.conn, args["source_id"], level, section=args.get("section") or "",
                       purpose=args.get("purpose") or "")
    ctx.reads.append({"source_id": args["source_id"], "level": level, "section": args.get("section") or "",
                      "purpose": (args.get("purpose") or "")[:300]})
    return text


def _list_weakness_patterns(args: dict[str, Any], ctx: ToolContext) -> str:
    from ..reading import patterns_for
    domains = None
    if args.get("problem_id"):
        p = store.get(ctx.conn, "problems", args["problem_id"])
        domains = p.get("domains") if p else None
    rows = patterns_for(ctx.conn, domains) if domains else store.select(ctx.conn, "weakness_patterns", order="id")
    if not rows:
        return "No weakness patterns loaded (mathresearch catalog load)."
    out = []
    for r in rows:
        out.append(f"[{r['id']}] {r['name']} (applies to {', '.join(r.get('applies_to') or [])})\n"
                   f"  {r['description'].strip()}\n  signature: {r.get('signature') or '-'}\n"
                   f"  test: {(r.get('test') or '-').strip()}\n"
                   f"  exploited by: {', '.join(r.get('exploited_by') or []) or '-'}; "
                   f"examples: {', '.join(r.get('examples') or []) or '-'}")
    return "\n".join(out)


def _list_lessons(args: dict[str, Any], ctx: ToolContext) -> str:
    from ..lessons import all_lessons, lessons_for, render
    expert = (args.get("expert") or "").strip() or None
    domains = None
    if args.get("problem_id"):
        p = store.get(ctx.conn, "problems", args["problem_id"])
        domains = p.get("domains") if p else None
    if domains:
        rows = lessons_for(ctx.conn, domains, expert)
    else:
        rows = [r for r in all_lessons(ctx.conn) if not expert or expert in (r.get("experts") or [])]
    if not rows:
        return "No lessons match (mathresearch catalog load loads them)."
    return "\n".join(render(r) for r in rows)


def _family_kwargs(args: dict[str, Any]) -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if args.get("p"):
        kw["p"] = int(args["p"])
    if args.get("ratio"):
        kw["ratio"] = float(args["ratio"])
    if args.get("theta"):
        kw["theta"] = int(args["theta"])
    return kw


def _sample_instance(args: dict[str, Any], ctx: ToolContext) -> str:
    from ..lab import get_family
    from ..lab.script_attack import INSTANCE_FORMAT, instance_to_json

    try:
        family = get_family(args["family"], **_family_kwargs(args))
        inst = family.generate(int(args.get("size") or 6), seed=int(args.get("seed") or 0))
    except (KeyError, TypeError, ValueError) as exc:
        return f"cannot sample: {exc}"
    data = instance_to_json(inst)
    text = json.dumps(data)
    if len(text) > 12000:
        text = text[:12000] + " ..."
    return f"{INSTANCE_FORMAT}\nSample (public part only; the planted solution is withheld):\n{text}"


def _submit_attack(args: dict[str, Any], ctx: ToolContext) -> str:
    from ..lab.script_attack import evaluate_script_attack

    sizes = _parse_sizes(args.get("sizes", ""), [7, 9, 11, 13])
    seconds = min(float(args.get("seconds_per_size") or 60.0), ctx.script_timeout)
    try:
        result = evaluate_script_attack(
            ctx.conn, name=args["name"], code=args["code"], family_name=args["family"], sizes=sizes,
            time_limit=seconds, engine=args.get("engine") or "python", family_kwargs=_family_kwargs(args),
            problem_id=args.get("problem_id") or ctx.question.get("problem_id"), question_id=ctx.question["id"],
            run_id=ctx.run_id, description=args.get("description") or "", workspace=ctx.workspace)
    except (KeyError, TypeError, ValueError) as exc:
        return f"cannot evaluate: {exc}"
    ctx.experiment_ids.append(result["experiment_id"])
    pts = "\n".join(f"  n={p['size']}: {'solved' if p['success'] else 'failed'} in {p['seconds']:.3g}s"
                    f"{' work=' + str(p['work']) if p.get('work') else ''}"
                    f"{' (' + p['notes'] + ')' if p.get('notes') else ''}" for p in result["curve"]["points"])
    tail = ""
    failed = [p for p in result["curve"]["points"] if not p["success"]]
    if failed:
        tail = "\nlast failure stderr: " + (failed[-1].get("stderr_tail") or "(empty)")[-400:]
    return (f"experiment {result['experiment_id']}, finding {result['finding_id']}"
            + (f", registered attack {result['attack_id']}" if result["attack_id"] else "")
            + f"\nverdict: {result['verdict']}\n{result['summary']}\n{pts}{tail}")


TOOLS: dict[str, tuple[ToolSpec, Executor]] = {
    "sample_instance": (
        ToolSpec(
            name="sample_instance",
            description="Generate one public instance of a problem family (mq or matsumoto_imai) in the exact JSON "
                        "format a submitted attack script receives, with the format documented. Use it to study the "
                        "structure of the public map in run_script before writing an attack.",
            input_schema={"type": "object", "properties": {
                "family": {"type": "string"},
                "size": {"type": "integer", "description": "n (number of variables)"},
                "seed": {"type": "integer"},
                "p": {"type": "integer"}, "ratio": {"type": "number"}, "theta": {"type": "integer"}}},
        ), _sample_instance),
    "submit_attack": (
        ToolSpec(
            name="submit_attack",
            description="Submit an attack as a Python (or Sage) script. The lab runs it on fresh instances it "
                        "generates (the script only sees instance.json: the public polynomials), verifies every "
                        "claimed solution, sweeps the sizes for a cost curve, records an experiment and a finding, "
                        "and registers the attack when it works. Verdict 'broken' needs success at every size with "
                        "polynomial-like growth; report a deterministic 'work' count for a reliable fit. This is "
                        "the only way to establish that you have broken something.",
            input_schema={"type": "object", "properties": {
                "name": {"type": "string", "description": "identifier, e.g. bilinear_relations_v1"},
                "code": {"type": "string", "description": "full script; reads instance.json, prints RESULT: {...}"},
                "family": {"type": "string"},
                "sizes": {"type": "string", "description": "comma-separated n values, e.g. '7,9,11,13'"},
                "seconds_per_size": {"type": "number"},
                "engine": {"type": "string", "enum": ["python", "sage"]},
                "problem_id": {"type": "string", "description": "defaults to the question's problem"},
                "description": {"type": "string", "description": "what structure the attack exploits"},
                "p": {"type": "integer"}, "ratio": {"type": "number"}, "theta": {"type": "integer"}}},
        ), _submit_attack),
    "search_literature": (
        ToolSpec(
            name="search_literature",
            description="Search arXiv for papers (title, abstract, authors, categories) and see which are already in "
                        "the knowledge base. Use it on a 'learn' question before ingesting. Supports the arXiv API "
                        "syntax: ti:, abs:, au:, cat:cs.CR, AND/OR.",
            input_schema={"type": "object", "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "1-20 results"}}},
        ), _search_literature),
    "ingest_source": (
        ToolSpec(
            name="ingest_source",
            description="Download a paper (arXiv id or URL, or IACR ePrint id like 2024/123) and split it into "
                        "knowledge units (definitions, theorems, lemmas, algorithms with exact statements and "
                        "anchors). Returns the source id and the first unit ids. LaTeX sources give exact units; "
                        "PDFs give heuristic ones.",
            input_schema={"type": "object", "properties": {
                "reference": {"type": "string", "description": "e.g. '2301.00001', 'arXiv:math/0601001', "
                                                              "'https://eprint.iacr.org/2024/123'"},
                "domains": {"type": "string", "description": "comma-separated domain tags for the units"}}},
        ), _ingest_source),
    "read_source": (
        ToolSpec(
            name="read_source",
            description="Read an ingested paper top-down. level 'card': the paper card (problem, hardness, structure, "
                        "results, attacks, open problems) or the abstract, plus earlier reading notes and the section "
                        "list. level 'outline': sections with their units. level 'section': full statements of the "
                        "units in the named section; needs 'purpose', the decision the section will change. Read "
                        "down only as far as the question needs.",
            input_schema={"type": "object", "properties": {
                "source_id": {"type": "string"},
                "level": {"type": "string", "enum": ["card", "outline", "section"]},
                "section": {"type": "string", "description": "part of a section name (level 'section'); else empty"},
                "purpose": {"type": "string",
                            "description": "The reading brief: which decision this changes (required for 'section')."}}},
        ), _read_source),
    "list_weakness_patterns": (
        ToolSpec(
            name="list_weakness_patterns",
            description="List the weakness patterns (the structural reasons known schemes broke), each with its "
                        "signature in public data and a computational test. Give a problem id to get only the "
                        "patterns that apply to its domains. Audit every candidate against them (weakness_audits).",
            input_schema={"type": "object", "properties": {
                "problem_id": {"type": "string", "description": "optional; empty for all patterns"}}},
        ), _list_weakness_patterns),
    "list_lessons": (
        ToolSpec(
            name="list_lessons",
            description="List the lessons drawn from the published work of PQC experts (Regev, Peikert, Ducas, "
                        "Lyubashevsky, Bai): each principle, the check to run, the references and the ePrint/arXiv "
                        "ids to ingest. Filter by expert name and/or by a problem's domains.",
            input_schema={"type": "object", "properties": {
                "expert": {"type": "string", "description": "e.g. 'Peikert'; empty for all"},
                "problem_id": {"type": "string", "description": "only lessons for this problem's domains; or empty"}}},
        ), _list_lessons),
    "list_sources": (
        ToolSpec(
            name="list_sources",
            description="List the papers already ingested into the knowledge base with their unit counts.",
            input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
        ), _list_sources),
    "run_attack": (
        ToolSpec(
            name="run_attack",
            description="Run one attack (or all applicable attacks) from the attack lab against a problem family "
                        "over a range of sizes and get the cost curve. Families: mq (params p, ratio = m/n), "
                        "matsumoto_imai. Attacks: exhaustive_search, linearization, patarin_linearization_equations, "
                        "groebner_sage, or all. Recorded as an experiment.",
            input_schema={"type": "object", "properties": {
                "family": {"type": "string"},
                "attack": {"type": "string", "description": "attack name or 'all'"},
                "sizes": {"type": "string", "description": "comma-separated sizes, e.g. '4,6,8,10'"},
                "seconds_per_size": {"type": "number"},
                "p": {"type": "integer", "description": "field size for mq (prime); 2 for matsumoto_imai"},
                "ratio": {"type": "number", "description": "equations per variable for mq"}}},
        ), _run_attack),
    "ladder_evaluate": (
        ToolSpec(
            name="ladder_evaluate",
            description="Run the promotion-ladder checks (well defined; not trivially broken) for a problem that has "
                        "a generator family, record findings, and update its status.",
            input_schema={"type": "object", "properties": {
                "problem_id": {"type": "string"},
                "family": {"type": "string", "description": "family name; defaults to the problem's generator"},
                "sizes": {"type": "string"},
                "seconds_per_size": {"type": "number"}}},
        ), _ladder_evaluate),
    "search_units": (
        ToolSpec(
            name="search_units",
            description="Full-text search (BM25) over the knowledge base of definitions, theorems, algorithms and "
                        "examples. Use exact mathematical terms (e.g. 'Groebner basis', 'syzygy', 'MinRank').",
            input_schema={"type": "object", "properties": {
                "query": {"type": "string", "description": "FTS5 query; words are ANDed, quote phrases."},
                "limit": {"type": "integer", "description": "Max hits, 1-30."}}},
        ), _search_units),
    "open_unit": (
        ToolSpec(
            name="open_unit",
            description="Open one knowledge unit in full: exact statement, hypotheses, notation, proof sketch, "
                        "dependencies and source anchor.",
            input_schema={"type": "object", "properties": {"unit_id": {"type": "string"}}},
        ), _open_unit),
    "run_script": (
        ToolSpec(
            name="run_script",
            description="Run a SageMath or Python script in a sandbox (time and memory limited) and record it as "
                        "an experiment. Print a final line 'RESULT: {json}' to return structured data. Use it for "
                        "every computational claim: counting, sampling instances, timing forward maps, running attacks.",
            input_schema={"type": "object", "properties": {
                "code": {"type": "string"},
                "engine": {"type": "string", "enum": ["auto", "sage", "python"],
                           "description": "auto uses Sage when installed."},
                "purpose": {"type": "string", "description": "One line on what this experiment tests."}}},
        ), _run_script),
    "list_catalog": (
        ToolSpec(
            name="list_catalog",
            description="List the catalog of known hard problems, known attacks, or the system's own candidate "
                        "problems. Always check it before calling anything new.",
            input_schema={"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["problems", "attacks", "candidates"]}}},
        ), _list_catalog),
    "read_problem": (
        ToolSpec(
            name="read_problem",
            description="Read a problem record in full with its recent findings and experiments.",
            input_schema={"type": "object", "properties": {"problem_id": {"type": "string"}}},
        ), _read_problem),
    "read_question": (
        ToolSpec(
            name="read_question",
            description="Read another question's current summary and findings (e.g. a child learn-question).",
            input_schema={"type": "object", "properties": {"question_id": {"type": "string"}}},
        ), _read_question),
}


def specs(names: list[str] | None = None) -> list[ToolSpec]:
    names = names or list(TOOLS)
    return [TOOLS[n][0] for n in names]


def execute(name: str, args: dict[str, Any], ctx: ToolContext) -> tuple[str, bool]:
    """Run a tool. Returns (text, is_error)."""
    if name not in TOOLS:
        return f"Unknown tool {name!r}. Available: {', '.join(TOOLS)}", True
    try:
        return TOOLS[name][1](args, ctx), False
    except Exception as exc:  # the model should see tool failures, not crash the run
        return f"{type(exc).__name__}: {exc}", True
