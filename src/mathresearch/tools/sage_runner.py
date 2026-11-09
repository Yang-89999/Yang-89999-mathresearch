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


# -- write guard ------------------------------------------------------------------------------------------------
# A script may write only inside its own experiment directory and the system temp directory. On the first live
# runs a worker's script rewrote the system's own source files (the package is installed in editable mode), so
# this is enforced in two layers: a Python audit hook prepended to every script (all platforms; it also blocks
# starting other programs from plain Python scripts), and on macOS the operating system's sandbox (sandbox-exec),
# which also covers programs Sage starts. Neither is meant to stop a determined attacker; they stop a research
# worker from changing anything outside its experiment.

GUARD_MARKER = "# --- mathresearch write guard (prepended; your script starts below) ---"

GUARD_CODE = """
def __mr_guard(allowed, block_spawn, programs, guard_dir):
    import os, sys
    roots = [os.path.realpath(a) for a in allowed]
    guard = os.path.realpath(guard_dir) if guard_dir else None

    def under(p, r):
        return p == r or p.startswith(r + os.sep)

    def inside(path):
        try:
            p = os.path.realpath(os.fsdecode(os.fspath(path)))
        except TypeError:
            return True  # a file descriptor: opened earlier, already checked
        if guard and under(p, guard):
            return False  # the guard's own files are never writable
        return any(under(p, r) for r in roots)

    def deny(what, path):
        raise PermissionError(f"write guard: {what} {path!r} is outside the experiment directory; "
                              "scripts may write only in their working directory or the temp directory")

    def python_child(argv, env):
        # Another Python process is allowed when it inherits this guard: the guard directory first on
        # PYTHONPATH (its sitecustomize installs the guard) and no flag that skips it (-I, -E, -S).
        env = os.environ if env is None else env
        path = env.get("PYTHONPATH") or env.get(b"PYTHONPATH") or ""
        first = os.fsdecode(path).split(os.pathsep)[0]
        if not guard or not first or os.path.realpath(first) != guard:
            return "the child must keep PYTHONPATH from os.environ"
        args = [os.fsdecode(a) for a in argv[1:]]
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--" or a == "-" or not a.startswith("-"):
                break
            if a.startswith("--"):
                i += 1
                continue
            for j, ch in enumerate(a[1:]):
                if ch in "IES":
                    return f"flag -{ch} would skip the guard"
                if ch == "m":
                    mod = a[j + 2:] or (args[i + 1] if i + 1 < len(args) else "")
                    if mod.split(".")[0] in ("pip", "ensurepip", "venv"):
                        return "installing packages is not allowed (numpy and sympy are installed)"
                    return None
                if ch == "c":
                    return None
                if ch in "XW":
                    if j == len(a) - 2:
                        i += 1
                    break
            i += 1
        return None

    writes = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    two_paths = {"os.rename", "os.link", "os.symlink", "shutil.copyfile", "shutil.copytree", "shutil.move"}
    one_path = {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.utime",
                "shutil.rmtree", "os.chflags", "os.lchflags", "os.setxattr", "os.removexattr"}
    spawn = {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
             "pty.spawn"}
    pythons = {os.path.basename(sys.executable)}

    def hook(event, args):
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if path is None or isinstance(path, int):
                return
            wants = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \\
                (isinstance(flags, int) and flags & writes)
            if wants and not inside(path):
                deny("writing", path)
        elif event in one_path:
            if args and not isinstance(args[0], int) and not inside(args[0]):
                deny(event, args[0])
        elif event in two_paths:
            target = args[1] if len(args) > 1 else None
            if target is not None and not isinstance(target, int) and not inside(target):
                deny(event, target)
            if event in ("os.rename", "shutil.move") and args and not inside(args[0]):
                deny(event, args[0])
        elif block_spawn and event in spawn:
            why = "only Python (sys.executable) and these math programs may be started"
            if event == "subprocess.Popen" and args:
                argv = list(args[1]) if args[1] else []
                exe = args[0] if args[0] is not None else (argv[0] if argv else "")
                name = os.path.basename(os.fsdecode(exe if isinstance(exe, (str, bytes)) else str(exe)))
                if name in programs:
                    return
                if name in pythons or (name.startswith("python") and name[6:].replace(".", "").isdigit()):
                    why = python_child(argv, args[3] if len(args) > 3 else None)
                    if why is None:
                        return
            raise PermissionError(f"write guard: starting other programs ({event}) is not allowed in scripts: "
                                  f"{why}; allowed: Python via subprocess.run([sys.executable, 'helper.py']) "
                                  f"(the guard applies there too) and {sorted(programs)}")

    sys.addaudithook(hook)
"""

MATH_PROGRAMS = ("msolve", "Singular", "M2", "magma", "gp", "gap", "maxima", "sage")


def guard_prefix(workdir: Path, block_spawn: bool, guard_dir: Path | None = None) -> str:
    allowed = [str(Path(workdir).resolve()), tempfile.gettempdir(), "/dev/null", "/dev/tty"]
    gd = str(Path(guard_dir).resolve()) if guard_dir else None
    return (f"{GUARD_MARKER}\n{GUARD_CODE}\n"
            f"__mr_guard({allowed!r}, {block_spawn!r}, {set(MATH_PROGRAMS)!r}, {gd!r})\ndel __mr_guard\n"
            f"# --- end of guard ---\n")


SANDBOX_PROFILE = """(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath "{workdir}") (subpath "/private/tmp") (subpath "/private/var/folders") (subpath "/dev"))
"""

_SANDBOX_OK: bool | None = None


def sandbox_command(cmd: list[str], workdir: Path) -> list[str]:
    """Wrap ``cmd`` in macOS's sandbox-exec so it can write only to ``workdir`` and temp directories. Returns
    ``cmd`` unchanged where sandbox-exec is unavailable or fails its self-test."""
    global _SANDBOX_OK
    exe = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
    if not exe:
        return cmd
    profile = SANDBOX_PROFILE.format(workdir=str(Path(workdir).resolve()).replace('"', ""))
    if _SANDBOX_OK is None:
        try:
            _SANDBOX_OK = subprocess.run([exe, "-p", profile, "/usr/bin/true"], capture_output=True,
                                         timeout=20).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _SANDBOX_OK = False
    return [exe, "-p", profile, *cmd] if _SANDBOX_OK else cmd


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
    # Sage may start helper programs (Singular, Maxima); the OS sandbox covers those on macOS. A Python script
    # may start further Python processes: the guard reaches them through a sitecustomize module on PYTHONPATH,
    # kept in its own directory, which scripts cannot write to.
    guard_dir = Path(tempfile.mkdtemp(prefix="mr_guard_")) if engine == "python" else None
    if guard_dir:
        (guard_dir / "sitecustomize.py").write_text(guard_prefix(workdir, True, guard_dir), encoding="utf-8")
    script.write_text(guard_prefix(workdir, block_spawn=engine == "python", guard_dir=guard_dir) + code,
                      encoding="utf-8")

    cmd = [sys.executable, "-I", str(script)]
    if engine == "sage":
        # Let Sage preparse the file so Sage syntax (R.<x> = ...) works.
        cmd = ["sage", str(script)]
    cmd = sandbox_command(cmd, workdir)

    child_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(workdir), "LANG": "C.UTF-8",
                 "PYTHONDONTWRITEBYTECODE": "1", "MPLBACKEND": "Agg"}
    if env:
        child_env.update(env)
    if guard_dir:
        child_env["PYTHONPATH"] = str(guard_dir.resolve())

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
    finally:
        if guard_dir:
            shutil.rmtree(guard_dir, ignore_errors=True)
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
