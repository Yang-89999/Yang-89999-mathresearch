"""Command-line entry point.

    mathresearch init [--dir DIR]      create a workspace: charter.yaml + research.db
    mathresearch status [--dir DIR]    table counts, open questions, engines on PATH
    mathresearch charter show          print the charter as workers see it
    mathresearch run-script FILE       execute a script through the sandbox and record it
    mathresearch run --cycles N        run N research cycles with the configured model provider
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
    from .loop import run_cycles
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

    def report(outcome):
        q = store.get(conn, "questions", outcome.question_id)
        created = ", ".join(f"{len(v)} {k}" for k, v in outcome.created.items() if v) or "nothing new"
        print(f"[{outcome.status}] {q['lane']:7s} {q['kind']:12s} {q['title'][:70]}")
        print(f"         steps={outcome.steps} tokens={outcome.usage.input_tokens}+{outcome.usage.output_tokens} "
              f"-> {created}" + (f"  error: {outcome.error}" if outcome.error else ""))

    outcomes = run_cycles(conn, charter, provider, ws, cycles=args.cycles, seed=args.seed, model=args.model,
                          max_steps=args.max_steps, on_outcome=report)
    if not outcomes:
        print("nothing to run: the agenda has no open questions", file=sys.stderr)
    failed = sum(o.status == "failed" for o in outcomes)
    print(f"\n{len(outcomes)} cycles, {failed} failed")
    return 1 if failed and failed == len(outcomes) else 0


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
    s.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
