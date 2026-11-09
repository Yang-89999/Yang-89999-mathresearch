"""Command-line entry point.

    mathresearch init [--dir DIR]      create a workspace: charter.yaml + research.db
    mathresearch status [--dir DIR]    table counts, open questions, engines on PATH
    mathresearch charter show          print the charter as workers see it
    mathresearch run-script FILE       execute a script through the sandbox and record it
    mathresearch run --cycles N        run N research cycles with the configured model provider
    mathresearch catalog load          load the seed catalogs of known problems and attacks
    mathresearch lab run --family mq   run attack sweeps from the command line
    mathresearch ingest fetch 2301.00001   download papers and split them into knowledge units
    mathresearch ingest file paper.tex     ingest a local LaTeX/PDF/text file
    mathresearch ingest seed SOURCE_ID     queue a seed read for a source already ingested
    mathresearch units search 'Groebner'   search the knowledge base
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, db, store
from .charter import DEFAULT_CHARTER, Charter, CharterError
from .tools.sage_runner import available_engines, run_script

CHARTER_FILE = "charter.yaml"
DB_FILE = "research.db"


def _workspace(args: argparse.Namespace) -> Path:
    return Path(args.dir).expanduser().resolve()


def cmd_init(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    ws.mkdir(parents=True, exist_ok=True)
    charter_path = ws / CHARTER_FILE
    if charter_path.exists() and not args.force:
        print(f"{charter_path} exists; use --force to overwrite the charter", file=sys.stderr)
    else:
        charter_path.write_text(DEFAULT_CHARTER, encoding="utf-8")
        print(f"wrote {charter_path}")
    conn = db.connect(ws / DB_FILE)
    print(f"database ready at {ws / DB_FILE} (schema v{db.current_version(conn)})")
    (ws / "experiments").mkdir(exist_ok=True)
    (ws / "sources").mkdir(exist_ok=True)
    print("edit charter.yaml to steer the research, then run: mathresearch status")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    db_path = ws / DB_FILE
    if not db_path.exists():
        print(f"no database at {db_path}; run 'mathresearch init' first", file=sys.stderr)
        return 1
    conn = db.connect(db_path)
    print(f"mathresearch {__version__}  workspace {ws}")
    print("\ntables:")
    for table, n in db.table_counts(conn).items():
        print(f"  {table:12s} {n}")
    print("\nopen questions by lane:")
    for lane in ("harvest", "deepen", "open"):
        n = conn.execute("SELECT COUNT(*) AS n FROM questions WHERE status='open' AND lane=?", (lane,)).fetchone()["n"]
        print(f"  {lane:8s} {n}")
    blocked = conn.execute("SELECT COUNT(*) AS n FROM questions WHERE status='blocked'").fetchone()["n"]
    print(f"  blocked  {blocked}")
    from . import reading
    mix = reading.origin_mix(conn)
    if mix:
        print("  sparked by: " + ", ".join(f"{k} {v}" for k, v in sorted(mix.items())))
    refs = conn.execute("SELECT COUNT(*) AS n, SUM(card IS NOT NULL) AS cards FROM sources "
                        "WHERE provided_by = 'operator'").fetchone()
    if refs["n"]:
        print(f"  your references: {refs['n']} ({refs['cards'] or 0} with a paper card)")
    from . import pricing
    charter_path = ws / CHARTER_FILE
    sp = pricing.spend(conn)
    print(f"\nspend: ${sp['usd_today']:.2f} today ({sp['today']} UTC), ${sp['usd_total']:.2f} in total; "
          f"{sp['tokens_total']:,} tokens")
    for m, v in sorted(sp["by_model"].items()):
        print(f"  {m:24s} {v['runs']:4d} runs  ${v['usd']:.2f}  {v['input_tokens']:,} in / {v['output_tokens']:,} out")
    if sp["unpriced_runs"]:
        print(f"  {sp['unpriced_runs']} runs have no price (add the model to the charter's pricing section)")
    if charter_path.exists():
        try:
            b = Charter.load(charter_path).budget
            caps = ", ".join(f"{k} {v}" for k, v in vars(b).items() if v is not None) or "none"
            print(f"  caps: {caps}")
        except CharterError as exc:
            print(f"  charter error: {exc}")
    print("\nengines on PATH:")
    for name, path in available_engines().items():
        print(f"  {name:8s} {path or '-'}")
    return 0


def cmd_charter_show(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    try:
        charter = Charter.load(ws / CHARTER_FILE)
    except FileNotFoundError:
        print(f"no charter at {ws / CHARTER_FILE}; run 'mathresearch init' first", file=sys.stderr)
        return 1
    except CharterError as exc:
        print(f"invalid charter: {exc}", file=sys.stderr)
        return 2
    print(charter.to_prompt())
    print()
    print(f"lanes: {json.dumps(charter.lanes)}  concurrency: {charter.max_concurrent_runs}")
    return 0


def cmd_run_script(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    code = Path(args.file).read_text(encoding="utf-8")
    res = run_script(code, engine=args.engine, timeout=args.timeout, memory_mb=args.memory_mb)
    db_path = ws / DB_FILE
    exp_id = None
    if db_path.exists() and not args.no_record:
        conn = db.connect(db_path)
        exp_id = store.insert(conn, "experiments", kind="script", **res.to_experiment_row())
    sys.stdout.write(res.stdout)
    if res.stderr:
        sys.stderr.write(res.stderr)
    status = "timed out" if res.timed_out else f"exit {res.exit_code}"
    print(f"\n[{res.engine}] {status} in {res.wall_seconds}s" + (f"  recorded as {exp_id}" if exp_id else ""), file=sys.stderr)
    return 0 if res.ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    from . import pricing
    from .loop import UnpricedModelError, run_cycles
    from .providers import make_provider

    ws = _workspace(args)
    try:
        charter = Charter.load(ws / CHARTER_FILE)
    except FileNotFoundError:
        print(f"no charter at {ws / CHARTER_FILE}; run 'mathresearch init' first", file=sys.stderr)
        return 1
    conn = db.connect(ws / DB_FILE)
    try:
        provider = make_provider(args.provider, args.model)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.budget_usd is not None:  # a cap for this invocation, on top of the charter's
        spent = pricing.spend(conn)["usd_total"]
        cap = spent + args.budget_usd
        current = charter.budget.max_usd_total
        charter.budget.max_usd_total = cap if current is None else min(current, cap)
    workers = args.workers if args.workers is not None else charter.max_concurrent_runs

    def report(outcome):
        q = store.get(conn, "questions", outcome.question_id)
        created = ", ".join(f"{len(v)} {k}" for k, v in outcome.created.items() if v) or "nothing new"
        cost = f" ${outcome.cost_usd:.3f}" if outcome.cost_usd is not None else " (unpriced)"
        print(f"[{outcome.status}] {q['lane']:7s} {q['kind']:12s} {q['title'][:70]}")
        print(f"         steps={outcome.steps} tokens={outcome.usage.input_tokens}+{outcome.usage.output_tokens}"
              f"{cost} -> {created}" + (f"  error: {outcome.error}" if outcome.error else "")
              + (f"  [stopped: {outcome.budget_note}]" if outcome.budget_note else ""))

    try:
        outcomes = run_cycles(conn, charter, provider, ws, cycles=args.cycles, seed=args.seed, model=args.model,
                              max_steps=args.max_steps, on_outcome=report, workers=workers,
                              allow_unpriced=args.allow_unpriced)
    except UnpricedModelError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for w in outcomes.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if outcomes.stopped_because:
        print(f"stopped early: {outcomes.stopped_because}", file=sys.stderr)
    elif not outcomes:
        print("nothing to run: the agenda has no open questions", file=sys.stderr)
    failed = sum(o.status == "failed" for o in outcomes)
    cost = sum(o.cost_usd or 0.0 for o in outcomes)
    s = pricing.spend(conn)
    print(f"\n{len(outcomes)} cycles, {failed} failed, ${cost:.3f} this invocation "
          f"(${s['usd_today']:.2f} today, ${s['usd_total']:.2f} in total)")
    return 1 if failed and failed == len(outcomes) else 0


def cmd_catalog_load(args: argparse.Namespace) -> int:
    from . import catalog

    conn = db.connect(_workspace(args) / DB_FILE)
    counts = catalog.load(conn)
    totals = db.table_counts(conn)
    print(f"inserted {counts['problems']} problems and {counts['attacks']} attacks "
          f"(catalog now {totals['problems']} problems, {totals['attacks']} attacks)")
    return 0


def _domains(text: str | None) -> list[str] | None:
    return [d.strip() for d in text.split(",") if d.strip()] if text else None


def _seed_policy(ws: Path) -> bool:
    try:
        return Charter.load(ws / CHARTER_FILE).reading.seed_operator_references
    except (FileNotFoundError, CharterError):
        return True


def _seed(conn, ws: Path, source_id: str, args: argparse.Namespace) -> None:
    """The operator provided this reference: mark it, and queue a seed read unless told not to."""
    from . import reading, store
    store.update(conn, "sources", source_id, provided_by="operator")
    if getattr(args, "no_seed", False) or not _seed_policy(ws):
        return
    qid = reading.seed_reference(conn, source_id)
    print(f"  seed read queued: {qid}")


def cmd_ingest_seed(args: argparse.Namespace) -> int:
    from . import reading, store
    ws = _workspace(args)
    conn = db.connect(ws / DB_FILE)
    status = 0
    for sid in args.source_ids:
        if store.get(conn, "sources", sid) is None:
            print(f"{sid}: no such source", file=sys.stderr)
            status = 1
            continue
        print(f"{sid}: seed read {reading.seed_reference(conn, sid)}")
    return status


def cmd_ingest_fetch(args: argparse.Namespace) -> int:
    from . import ingest

    ws = _workspace(args)
    conn = db.connect(ws / DB_FILE)
    failures = 0
    for i, ref in enumerate(args.refs):
        if i:
            ingest.ax.polite_sleep(args.delay)
        try:
            res = ingest.ingest_reference(conn, ref, ws, domains=_domains(args.domains), delay=args.delay,
                                          force=args.force)
        except Exception as exc:  # report and continue with the next reference
            print(f"{ref}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(res.summary())
        _seed(conn, ws, res.source_id, args)
    return 1 if failures and failures == len(args.refs) else 0


def cmd_ingest_file(args: argparse.Namespace) -> int:
    from . import ingest

    ws = _workspace(args)
    conn = db.connect(ws / DB_FILE)
    try:
        res = ingest.ingest_file(conn, Path(args.path).expanduser(), title=args.title, kind=args.kind,
                                 year=args.year, domains=_domains(args.domains))
    except (ValueError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(res.summary())
    _seed(conn, ws, res.source_id, args)
    return 0


def cmd_ingest_search(args: argparse.Namespace) -> int:
    from . import ingest

    conn = db.connect(_workspace(args) / DB_FILE)
    try:
        hits = ingest.search(conn, args.query, max_results=args.max)
    except OSError as exc:  # URLError is an OSError: no network, DNS, proxy
        print(f"arXiv search failed: {exc}", file=sys.stderr)
        return 1
    for h in hits:
        mark = "*" if h["ingested_source_id"] else " "
        print(f"{mark} arXiv:{h['arxiv_id']:14s} {h['year']}  {h['title'][:90]}")
        print(f"                   {', '.join(h['authors'][:3])}  [{', '.join(h['categories'])}]")
    if not hits:
        print("no results")
    return 0


def cmd_units_search(args: argparse.Namespace) -> int:
    conn = db.connect(_workspace(args) / DB_FILE)
    hits = store.search_units(conn, args.query, limit=args.limit)
    for u in hits:
        print(f"[{u['id']}] {u['kind']} {u.get('label') or ''} {u.get('title') or ''}".rstrip())
        print("    " + u["statement"][:300].replace("\n", " "))
    if not hits:
        print("no units match")
    return 0


def cmd_reflect(args: argparse.Namespace) -> int:
    from .reflect import reflect

    ws = _workspace(args)
    try:
        charter = Charter.load(ws / CHARTER_FILE)
    except FileNotFoundError:
        print(f"no charter at {ws / CHARTER_FILE}; run 'mathresearch init' first", file=sys.stderr)
        return 1
    conn = db.connect(ws / DB_FILE)
    reflection = reflect(conn, charter, ws, trigger="manual")
    print(reflection.digest, end="")
    print(f"digest {reflection.digest_id} written to {ws / 'digest.md'}", file=sys.stderr)
    return 0


def cmd_retro_list(args: argparse.Namespace) -> int:
    from .retro import SCENARIOS

    for s in SCENARIOS.values():
        print(f"{s.name:16s} knowledge up to {s.year}; target {s.target_family} sizes {s.target_sizes}\n"
              f"                 {s.description}")
    return 0


def cmd_retro_init(args: argparse.Namespace) -> int:
    from . import retro

    ws = _workspace(args)
    try:
        scenario = retro.get_scenario(args.scenario)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    ws.mkdir(parents=True, exist_ok=True)
    conn = db.connect(ws / DB_FILE)
    try:
        info = retro.setup(conn, ws, scenario, charter_file=CHARTER_FILE)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    (ws / "experiments").mkdir(exist_ok=True)
    (ws / "sources").mkdir(exist_ok=True)
    print(f"scenario {scenario.name}: catalog {info['catalog']}, {info['sources']} sources, {info['units']} units, "
          f"opening question {info['question_id']}")
    if info["leaks"]:
        print("WARNING forbidden terms present in: " + ", ".join(info["leaks"]), file=sys.stderr)
    print(f"now run: mathresearch --dir {ws} run --cycles N   then: mathresearch --dir {ws} retro check")
    return 0


def cmd_retro_check(args: argparse.Namespace) -> int:
    from . import retro

    ws = _workspace(args)
    conn = db.connect(ws / DB_FILE)
    try:
        scenario = retro.get_scenario(args.scenario)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    result = retro.check(conn, scenario)
    print(retro.render_check(result), end="")
    if args.json:
        print(json.dumps(result, indent=1, default=str))
    return 0 if result["passed"] else 1


def cmd_lab_run(args: argparse.Namespace) -> int:
    from .lab import get_family, sweep
    from .lab.attacks import applicable, get_attack

    kwargs = {}
    if args.p:
        kwargs["p"] = args.p
    if args.ratio:
        kwargs["ratio"] = args.ratio
    family = get_family(args.family, **kwargs)
    sizes = [int(s) for s in args.sizes.split(",")]
    attacks = applicable(family) if args.attack == "all" else [get_attack(args.attack)]
    for attack in attacks:
        curve = sweep(family, attack, sizes, time_limit=args.seconds)
        print(f"{attack.name} on {family.name}: growth {curve.growth} exp_slope={curve.exp_slope} "
              f"poly_degree={curve.poly_degree}")
        for pt in curve.points:
            state = "solved" if pt["success"] else ("timeout" if pt["timed_out"] else "failed")
            print(f"  n={pt['size']:3d} {state:8s} {pt['seconds']:9.4f}s work={pt['work']} {pt.get('notes', '')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mathresearch", description="Autonomous mathematical research environment")
    p.add_argument("--version", action="version", version=f"mathresearch {__version__}")
    p.add_argument("--dir", default=".", help="workspace directory (default: current directory)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create a workspace with a default charter and empty database")
    s.add_argument("--force", action="store_true", help="overwrite an existing charter.yaml")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("status", help="show state of the workspace")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("charter", help="charter commands")
    cs = s.add_subparsers(dest="charter_command", required=True)
    c = cs.add_parser("show", help="print the charter as workers see it")
    c.set_defaults(func=cmd_charter_show)

    s = sub.add_parser("run-script", help="run a Sage/Python script in the sandbox and record it")
    s.add_argument("file")
    s.add_argument("--engine", default="auto", choices=["auto", "sage", "python"])
    s.add_argument("--timeout", type=float, default=120.0)
    s.add_argument("--memory-mb", type=int, default=2048)
    s.add_argument("--no-record", action="store_true", help="do not store the run in the database")
    s.set_defaults(func=cmd_run_script)

    s = sub.add_parser("run", help="run research cycles: pick a question, run a worker, write back")
    s.add_argument("--cycles", type=int, default=1)
    s.add_argument("--provider", choices=["anthropic", "openai", "fake"],
                   help="default: MATHRESEARCH_PROVIDER, else whichever API key is set")
    s.add_argument("--model", help="model id (default: MATHRESEARCH_MODEL or the provider's default)")
    s.add_argument("--max-steps", type=int, default=12, help="max model calls per worker run")
    s.add_argument("--seed", type=int, help="random seed for lane selection")
    s.add_argument("--workers", type=int, help="parallel worker runs (default: charter max_concurrent_runs)")
    s.add_argument("--budget-usd", type=float, help="stop starting new runs once this invocation has spent this much")
    s.add_argument("--allow-unpriced", action="store_true",
                   help="run a model with no price in the charter, under the token cap only")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("catalog", help="catalog commands")
    cs = s.add_subparsers(dest="catalog_command", required=True)
    c = cs.add_parser("load", help="load the seed catalogs of known problems and attacks")
    c.set_defaults(func=cmd_catalog_load)

    s = sub.add_parser("retro", help="retrodiction tests: rediscover a known result from the knowledge of its time")
    rs = s.add_subparsers(dest="retro_command", required=True)
    c = rs.add_parser("list", help="list scenarios")
    c.set_defaults(func=cmd_retro_list)
    c = rs.add_parser("init", help="seed an empty workspace with a scenario")
    c.add_argument("--scenario", default="patarin_1995")
    c.set_defaults(func=cmd_retro_init)
    c = rs.add_parser("check", help="report whether the scenario's answer has been found (exit 0 when passed)")
    c.add_argument("--scenario", default="patarin_1995")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_retro_check)

    s = sub.add_parser("reflect", help="tidy the agenda and regenerate the world digest now")
    s.set_defaults(func=cmd_reflect)

    s = sub.add_parser("ingest", help="bring literature into the knowledge base")
    isub = s.add_subparsers(dest="ingest_command", required=True)
    c = isub.add_parser("fetch", help="download arXiv / IACR ePrint papers and split them into units")
    c.add_argument("refs", nargs="+", help="arXiv ids or URLs, ePrint ids like 2024/123 or URLs")
    c.add_argument("--domains", help="comma-separated domain tags for the units")
    c.add_argument("--delay", type=float, default=3.0, help="seconds between requests (arXiv asks for 3)")
    c.add_argument("--force", action="store_true", help="re-ingest even if the source is already present")
    c.add_argument("--no-seed", action="store_true", help="index only; do not queue a seed read")
    c.set_defaults(func=cmd_ingest_fetch)
    c = isub.add_parser("file", help="ingest a local .tex file, LaTeX directory, .pdf or text file")
    c.add_argument("path")
    c.add_argument("--title")
    c.add_argument("--kind", default="paper", choices=["paper", "book", "notes", "web", "other"])
    c.add_argument("--year", type=int)
    c.add_argument("--domains")
    c.add_argument("--no-seed", action="store_true", help="index only; do not queue a seed read")
    c.set_defaults(func=cmd_ingest_file)
    c = isub.add_parser("seed", help="queue a seed read for sources already ingested")
    c.add_argument("source_ids", nargs="+")
    c.set_defaults(func=cmd_ingest_seed)
    c = isub.add_parser("search", help="search arXiv; * marks papers already ingested")
    c.add_argument("query")
    c.add_argument("--max", type=int, default=10)
    c.set_defaults(func=cmd_ingest_search)

    s = sub.add_parser("units", help="knowledge-unit commands")
    us = s.add_subparsers(dest="units_command", required=True)
    c = us.add_parser("search", help="full-text search over knowledge units")
    c.add_argument("query")
    c.add_argument("--limit", type=int, default=10)
    c.set_defaults(func=cmd_units_search)

    s = sub.add_parser("lab", help="attack lab commands")
    ls = s.add_subparsers(dest="lab_command", required=True)
    c = ls.add_parser("run", help="run an attack sweep against a problem family")
    c.add_argument("--family", required=True)
    c.add_argument("--attack", default="all")
    c.add_argument("--sizes", default="4,6,8")
    c.add_argument("--seconds", type=float, default=20.0, help="time limit per size")
    c.add_argument("--p", type=int)
    c.add_argument("--ratio", type=float)
    c.set_defaults(func=cmd_lab_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
