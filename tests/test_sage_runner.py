import shutil

import pytest

from mathresearch import db, store
from mathresearch.tools.sage_runner import available_engines, parse_result, run_script


def test_python_script_runs_and_returns_result():
    res = run_script(
        "import json\n"
        "roots = [x for x in range(7) if (x*x - 2) % 7 == 0]\n"
        "print('roots', roots)\n"
        "print('RESULT:', json.dumps({'roots': roots}))\n",
        engine="python", timeout=30,
    )
    assert res.ok
    assert res.result == {"roots": [3, 4]}
    assert "roots [3, 4]" in res.stdout
    assert res.engine == "python"
    assert len(res.code_sha256) == 64
    assert res.tool_versions["python"]


def test_timeout_is_reported_not_raised():
    res = run_script("import time\nwhile True: time.sleep(0.1)\n", engine="python", timeout=1.0)
    assert res.timed_out and not res.ok
    assert res.exit_code is None
    assert "killed" in res.stderr


def test_failing_script_captures_stderr():
    res = run_script("raise ValueError('boom')\n", engine="python", timeout=30)
    assert not res.ok and res.exit_code == 1
    assert "boom" in res.stderr


def test_memory_limit_is_enforced():
    res = run_script("x = bytearray(900 * 1024 * 1024)\nprint('allocated')\n", engine="python",
                     timeout=30, memory_mb=256)
    assert not res.ok
    assert "allocated" not in res.stdout


def test_result_is_recorded_as_experiment():
    conn = db.connect(":memory:")
    res = run_script("print('RESULT: [1, 2, 3]')\n", engine="python", timeout=30)
    exp_id = store.insert(conn, "experiments", kind="check", **res.to_experiment_row())
    row = store.get(conn, "experiments", exp_id)
    assert row["result"] == [1, 2, 3]
    assert row["engine"] == "python" and row["exit_code"] == 0


def test_parse_result_takes_last_valid_line():
    assert parse_result("RESULT: {\"a\": 1}\nnoise\nRESULT: {\"a\": 2}\n") == {"a": 2}
    assert parse_result("RESULT: not json\n") is None
    assert parse_result("nothing\n") is None


def test_sage_requested_but_missing_raises():
    if available_engines()["sage"]:
        pytest.skip("sage is installed here")
    with pytest.raises(RuntimeError):
        run_script("print(1)", engine="sage")


@pytest.mark.skipif(shutil.which("sage") is None, reason="SageMath not installed")
def test_sage_script_runs():
    res = run_script("R.<x> = GF(7)[]\nprint('RESULT:', str((x^2 - 2).roots(multiplicities=False)))\n",
                     engine="sage", timeout=300)
    assert res.ok
