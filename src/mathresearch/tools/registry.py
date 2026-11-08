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
        rows = store.select(ctx.conn, "attacks", order="name")
        return "\n".join(f"[{r['id']}] {r['name']} ({r.get('family')}): exploits {r.get('exploits')}" for r in rows) \
            or "Attack catalog is empty."
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
    attacks = applicable(family) if attack_name == "all" else [get_attack(attack_name)]
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
                      ctx.script_timeout), question_id=ctx.question["id"], run_id=ctx.run_id)
    verdicts = result.get("attacks", {}).get("verdicts", [])
    return json.dumps({"status": result["status"], "well_defined": result["well_defined"], "verdicts": verdicts},
                      indent=1, default=str)


TOOLS: dict[str, tuple[ToolSpec, Executor]] = {
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
