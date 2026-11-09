"""Scripts may write only in their experiment directory: a worker's script once rewrote the system's own source."""

import sys
from pathlib import Path

from mathresearch.tools import sage_runner
from mathresearch.tools.sage_runner import run_script


def test_scripts_cannot_write_outside_their_directory(tmp_path):
    outside = Path(__file__).resolve().parent / "_guard_should_not_exist.txt"   # in the repo, not in temp
    code = f"""
import os, subprocess
open('inside.txt', 'w').write('fine')
for name, f in [('open', lambda: open({str(outside)!r}, 'w')), ('append', lambda: open({str(outside)!r}, 'a')),
                ('os.open', lambda: os.open({str(outside)!r}, os.O_WRONLY | os.O_CREAT)),
                ('rename', lambda: os.rename('inside.txt', {str(outside)!r})),
                ('remove', lambda: os.remove({str(Path(__file__).resolve())!r})),
                ('system', lambda: os.system('touch x')), ('popen', lambda: subprocess.run(['touch', 'x']))]:
    try:
        f()
        print('ESCAPED', name)
    except PermissionError as e:
        print('blocked', name)
print(open({str(Path(__file__).resolve())!r}).read()[:6])   # reading is fine
"""
    try:
        res = run_script(code, engine="python", cwd=tmp_path / "exp")
        assert res.ok, res.stderr
        assert "ESCAPED" not in res.stdout and res.stdout.count("blocked") == 7
        assert (tmp_path / "exp" / "inside.txt").read_text() == "fine" and not outside.exists()
        assert res.code == code                                   # the experiment records the worker's code
        assert Path(__file__).exists()
    finally:
        outside.unlink(missing_ok=True)


def test_math_programs_may_still_be_started(tmp_path, monkeypatch):
    prefix = sage_runner.guard_prefix(tmp_path, block_spawn=True)
    assert "'msolve'" in prefix and "'Singular'" in prefix


def test_macos_sandbox_wraps_the_command(tmp_path, monkeypatch):
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(sage_runner.sys, "platform", "darwin")
    monkeypatch.setattr(sage_runner.shutil, "which", lambda name: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(sage_runner.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Done())
    monkeypatch.setattr(sage_runner, "_SANDBOX_OK", None)
    cmd = sage_runner.sandbox_command([sys.executable, "-I", "script.py"], tmp_path)
    assert cmd[:2] == ["/usr/bin/sandbox-exec", "-p"] and cmd[3:] == [sys.executable, "-I", "script.py"]
    assert f'(subpath "{tmp_path.resolve()}")' in cmd[2] and "(deny file-write*)" in cmd[2]
    assert calls and calls[0][-1] == "/usr/bin/true"                      # self-tested once
    Done.returncode = 1
    monkeypatch.setattr(sage_runner, "_SANDBOX_OK", None)
    assert sage_runner.sandbox_command(["x"], tmp_path) == ["x"]          # falls back when the self-test fails
    monkeypatch.setattr(sage_runner.sys, "platform", "linux")
    monkeypatch.setattr(sage_runner, "_SANDBOX_OK", None)
    assert sage_runner.sandbox_command(["x"], tmp_path) == ["x"]


def test_python_helpers_run_under_the_same_guard(tmp_path):
    outside = Path(__file__).resolve().parent / "_guard_child_should_not_exist.txt"
    helper = f"""
import sys
try:
    open({str(outside)!r}, 'w')
    print('CHILD ESCAPED')
except PermissionError:
    print('child blocked')
open('from_child.txt', 'w').write('ok')
print('child ran', sys.argv[1:])
"""
    code = f"""
import os, subprocess, sys
open('helper.py', 'w').write({helper!r})
out = subprocess.run([sys.executable, '-u', 'helper.py', '7'], capture_output=True, text=True)
print(out.stdout.strip(), out.stderr.strip()[-200:])
def attempt(name, f):
    try:
        f()
        print('ESCAPED', name)
    except PermissionError:
        print('blocked', name)
attempt('isolated', lambda: subprocess.run([sys.executable, '-I', 'helper.py']))
attempt('no-site', lambda: subprocess.run([sys.executable, '-u', '-S', 'helper.py']))
attempt('clean env', lambda: subprocess.run([sys.executable, 'helper.py'], env={{'PATH': os.environ['PATH']}}))
attempt('own path first', lambda: subprocess.run([sys.executable, 'helper.py'],
        env={{**os.environ, 'PYTHONPATH': '.' + os.pathsep + os.environ['PYTHONPATH']}}))
attempt('pip', lambda: subprocess.run([sys.executable, '-m', 'pip', 'install', 'sympy']))
attempt('shell', lambda: subprocess.run('python3 helper.py', shell=True))
guard = os.environ['PYTHONPATH']
attempt('edit guard', lambda: open(os.path.join(guard, 'sitecustomize.py'), 'w'))
attempt('drop guard', lambda: os.remove(os.path.join(guard, 'sitecustomize.py')))
print('GUARD', guard)
"""
    try:
        res = run_script(code, engine="python", cwd=tmp_path / "exp")
        assert res.ok, res.stderr
        assert "child blocked" in res.stdout and "child ran ['7']" in res.stdout, res.stdout
        assert "ESCAPED" not in res.stdout and res.stdout.count("blocked") == 9, res.stdout
        assert (tmp_path / "exp" / "from_child.txt").read_text() == "ok" and not outside.exists()
        guard_dir = res.stdout.split("GUARD ")[1].split()[0]
        assert not Path(guard_dir).exists()                                  # removed after the run
    finally:
        outside.unlink(missing_ok=True)
