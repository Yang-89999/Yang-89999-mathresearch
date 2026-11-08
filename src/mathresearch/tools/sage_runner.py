"""Sandboxed execution of mathematical scripts.

``run_script`` executes a script in a subprocess with wall-clock and memory
limits, under SageMath when it is installed and plain Python otherwise, and
returns everything needed to record the execution as an experiment.

Scripts may print a final line of the form ``RESULT: {...json...}`` to hand a
structured result back; it is parsed into ``ScriptResult.result``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

RESULT_PREFIX = "RESULT:"


@dataclass
class ScriptResult:
    engine: str
    code: str
    code_sha256: str
    stdout: str
    stderr: str
    exit_code: int | None
    wall_seconds: float
    timed_out: bool
    result: dict | list | None = None
    tool_versions: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_experiment_row(self) -> dict:
        """Columns for ``store.insert(conn, 'experiments', **row)``."""
        return {
            "engine": self.engine,
            "code": self.code,
            "code_sha256": self.code_sha256,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "wall_seconds": self.wall_seconds,
            "timed_out": int(self.timed_out),
            "result": self.result,
            "tool_versions": self.tool_versions,
        }


def available_engines() -> dict[str, str | None]:
    """Which computational engines are on PATH."""
    return {name: shutil.which(name) for name in ("sage", "msolve", "magma", "Singular", "M2", "python3")}


def tool_versions(engine: str) -> dict[str, str]:
    versions = {"python": platform.python_version()}
    if engine == "sage":
        try:
            out = subprocess.run(["sage", "--version"], capture_output=True, text=True, timeout=60)
            versions["sage"] = out.stdout.strip().splitlines()[0] if out.stdout.strip() else "unknown"
        except (OSError, subprocess.TimeoutExpired):
            versions["sage"] = "unknown"
    return versions


def run_script(
    code: str,
    *,
    engine: str = "auto",
    timeout: float = 120.0,
    memory_mb: int = 2048,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> ScriptResult:
    """Run ``code`` and capture its output.

    engine: ``"sage"``, ``"python"`` or ``"auto"`` (Sage if installed).
    timeout: wall-clock seconds before the process is killed.
    memory_mb: address-space limit applied to the child (POSIX only).
    """
    engines = available_engines()
    if engine == "auto":
        engine = "sage" if engines["sage"] else "python"
    if engine == "sage" and not engines["sage"]:
        raise RuntimeError("SageMath requested but 'sage' is not on PATH")
    if engine not in ("sage", "python"):
        raise ValueError(f"unknown engine {engine!r}")

    sha = hashlib.sha256(code.encode("utf-8")).hexdigest()
    workdir = Path(cwd) if cwd else Path(tempfile.mkdtemp(prefix="mr_exp_"))
    workdir.mkdir(parents=True, exist_ok=True)
    script = workdir / ("script.sage" if engine == "sage" else "script.py")
    script.write_text(code, encoding="utf-8")

    cmd = ["sage", "-python", str(script)] if engine == "sage" else [sys.executable, "-I", str(script)]
    if engine == "sage":
        # Let Sage preparse the file so Sage syntax (R.<x> = ...) works.
        cmd = ["sage", str(script)]

    child_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(workdir), "LANG": "C.UTF-8",
                 "PYTHONDONTWRITEBYTECODE": "1", "MPLBACKEND": "Agg"}
    if env:
        child_env.update(env)

    def limit_resources() -> None:  # pragma: no cover - runs in the child
        if memory_mb and hasattr(resource, "RLIMIT_AS"):
            limit = memory_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            except (ValueError, OSError):
                pass
        os.setsid()

    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd, cwd=str(workdir), env=child_env, capture_output=True, text=True,
            timeout=timeout, preexec_fn=limit_resources if os.name == "posix" else None,
        )
        stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _text(exc.stdout)
        stderr = _text(exc.stderr) + f"\n[killed after {timeout:.0f}s]"
        exit_code = None
    wall = time.monotonic() - start

    return ScriptResult(
        engine=engine, code=code, code_sha256=sha,
        stdout=_cap(stdout), stderr=_cap(stderr), exit_code=exit_code,
        wall_seconds=round(wall, 3), timed_out=timed_out,
        result=parse_result(stdout), tool_versions=tool_versions(engine),
    )


def parse_result(stdout: str) -> dict | list | None:
    """Return the JSON after the last ``RESULT:`` line, if any."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                return json.loads(line[len(RESULT_PREFIX):].strip())
            except ValueError:
                return None
    return None


def _text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else data


def _cap(text: str, limit: int = 200_000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + f"\n... [{len(text) - limit} characters omitted] ...\n" + text[-limit // 2:]
