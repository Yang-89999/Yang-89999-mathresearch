import re
import threading
import time

import pytest
import yaml

from mathresearch import db, pricing, store
from mathresearch.charter import DEFAULT_CHARTER, Charter, CharterError
from mathresearch.cli import main
from mathresearch.loop import UnpricedModelError, run_cycles
from mathresearch.providers.base import ChatResponse, FakeProvider, TextBlock, ToolUseBlock, Usage
from mathresearch.reflect import reflect
from mathresearch.worker import run_question

WB = {"summary": "ok", "status": "open", "findings": [], "new_questions": [], "candidate_problems": []}


def _charter(**over):
    data = yaml.safe_load(DEFAULT_CHARTER)
    data.update(over)
    return Charter.from_dict(data)


def _final(tokens=(100, 50)):
    r = FakeProvider.final(WB)
    r.usage = Usage(input_tokens=tokens[0], output_tokens=tokens[1])
    return r


def _call(tokens=(100, 20), id_="c1"):
    r = FakeProvider.call("list_catalog", {"kind": "problems"}, id_=id_)
    r.usage = Usage(input_tokens=tokens[0], output_tokens=tokens[1])
    return r


class PricedFake(FakeProvider):
    """A fake that reports a real model id, so prices apply."""
    def __init__(self, responses, model="claude-opus-5-5"):
        super().__init__(responses)
        self.model = model


# -- prices --------------------------------------------------------------------------

def test_price_arithmetic_with_cache():
    p = pricing.BUILTIN_PRICES["claude-opus-5-5"]
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cached_input_tokens=600_000, cache_write_tokens=100_000)
    # 300k fresh * 4 + 600k * 0.2 + 100k * 5 + 100k out * 20, per million
    assert p.cost(u) == pytest.approx(1.2 + 0.12 + 0.5 + 2.0)
    assert pricing.cost_of(u, "unknown-model") is None
    over = {"my-openai-model": pricing.Price.from_dict({"input": 1, "output": 8, "cached_input": 0.1})}
    assert pricing.cost_of(Usage(input_tokens=2_000_000, output_tokens=0, cached_input_tokens=1_000_000),
                           "my-openai-model", over) == pytest.approx(1.1)
    with pytest.raises(ValueError):
        pricing.Price.from_dict({"input": 1})


def test_openai_default_model_is_priced_and_charter_overrides():
    from mathresearch.providers import openai_responses
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cached_input_tokens=500_000)
    # 500k fresh * 10 + 500k cached * 1 + 100k out * 50, per million
    assert pricing.cost_of(u, openai_responses.DEFAULT_MODEL) == pytest.approx(5.0 + 0.5 + 5.0)
    over = {openai_responses.DEFAULT_MODEL: pricing.Price.from_dict({"input": 4, "output": 20})}
    assert pricing.cost_of(u, openai_responses.DEFAULT_MODEL, over) == pytest.approx(4.0 + 2.0)


def test_charter_budget_and_pricing_sections():
    c = _charter(budget={"max_usd_per_day": 5, "max_usd_total": None},
                 pricing={"gpt-x": {"input": 1.25, "output": 10, "cached_input": 0.125}})
    assert c.budget.max_usd_per_day == 5.0 and c.budget.max_usd_total is None and c.budget.max_usd_per_run == 2.0
    assert c.pricing["gpt-x"].cached_input == 0.125
    with pytest.raises(CharterError):
        _charter(budget={"max_dollars": 3})
    with pytest.raises(CharterError):
        _charter(pricing={"gpt-x": {"input": 1}})


# -- per-run caps ----------------------------------------------------------------------------

def test_run_records_cost(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    qid = store.insert(conn, "questions", kind="survey", lane="harvest", status="open", title="Q")
    prov = PricedFake([_call((10_000, 1_000)), _final((20_000, 2_000))])
    out = run_question(conn, _charter(), prov, store.get(conn, "questions", qid), tmp_path)
    assert out.status == "done" and out.cost_usd == pytest.approx((30_000 * 4 + 3_000 * 20) / 1e6)
    run = store.get(conn, "runs", out.run_id)
    assert run["cost_usd"] == pytest.approx(out.cost_usd) and run["model"] == "claude-opus-5-5"


def test_token_cap_stops_tools_and_asks_for_writeback(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    qid = store.insert(conn, "questions", kind="survey", lane="harvest", status="open", title="Q")
    charter = _charter(budget={"max_tokens_per_run": 1000, "max_usd_per_run": None})
    prov = PricedFake([_call((900, 200)), _final((1200, 50))])
    out = run_question(conn, charter, prov, store.get(conn, "questions", qid), tmp_path)
    assert out.status == "done" and "token cap" in out.budget_note
    last = prov.requests[-1].messages[-1].content
    assert last[0].is_error and last[0].content.startswith("Not run: token cap")  # the tool did not run
    assert "budget for this run is spent" in last[-1].text
    assert store.get(conn, "runs", out.run_id)["context_ids"]["budget_note"] == out.budget_note


def test_dollar_cap_and_a_model_that_keeps_calling_tools(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    qid = store.insert(conn, "questions", kind="survey", lane="harvest", status="open", title="Q")
    charter = _charter(budget={"max_usd_per_run": 0.01, "max_tokens_per_run": None})
    prov = PricedFake([_call((5_000, 0)), _call((5_000, 0), id_="c2")])  # $0.02 then still calling
    out = run_question(conn, charter, prov, store.get(conn, "questions", qid), tmp_path)
    assert out.status == "failed" and "budget exhausted" in out.error and "dollar cap" in out.error
    assert store.get(conn, "questions", qid)["status"] == "open"


# -- loop-level caps --------------------------------------------------------------------------

def test_daily_cap_stops_new_runs(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    store.insert(conn, "runs", lane="harvest", provider="x", model="claude-opus-5-5", status="done", cost_usd=4.995)
    charter = _charter(budget={"max_usd_per_day": 5.0, "max_usd_per_run": 1.0})
    res = run_cycles(conn, charter, PricedFake([_final()] * 3), tmp_path, cycles=3, seed=1)
    assert len(res) == 0 and "daily cap" in res.stopped_because
    s = pricing.spend(conn)
    assert s["usd_today"] == 4.995 and s["by_model"]["claude-opus-5-5"]["runs"] == 1


def test_unpriced_model_refused_unless_allowed(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    prov = PricedFake([_final()], model="some-openai-model")
    with pytest.raises(UnpricedModelError):
        run_cycles(conn, _charter(), prov, tmp_path, cycles=1)
    res = run_cycles(conn, _charter(), prov, tmp_path, cycles=1, allow_unpriced=True)
    assert len(res) == 1 and res[0].cost_usd is None and "no price" in res.warnings[0]
    # with the price in the charter it is accepted and costed
    priced = _charter(pricing={"some-openai-model": {"input": 1, "output": 4}})
    res = run_cycles(conn, priced, PricedFake([_final()], model="some-openai-model"), tmp_path, cycles=1)
    assert res[0].cost_usd == pytest.approx((100 * 1 + 50 * 4) / 1e6)


# -- parallel workers ---------------------------------------------------------------------------

class SlowFake(PricedFake):
    def __init__(self, n, tokens=(100, 50)):
        super().__init__([_final(tokens) for _ in range(n)])
        self.active: list[str] = []
        self.peak = 0
        self.overlap = False
        self.lock = threading.Lock()

    def complete(self, request):
        qid = re.search(r"\[(q_[0-9a-z_]+)\]", request.messages[0].content[0].text).group(1)
        with self.lock:
            self.overlap |= qid in self.active  # the same question in two workers at once
            self.active.append(qid)
            self.peak = max(self.peak, len(self.active))
        time.sleep(0.15)
        try:
            return super().complete(request)
        finally:
            with self.lock:
                self.active.remove(qid)


def test_parallel_workers_run_distinct_questions(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    charter = _charter(reflect_every_runs=3)
    prov = SlowFake(6)
    reflections = []
    res = run_cycles(conn, charter, prov, tmp_path, cycles=6, seed=2, workers=3, on_reflection=reflections.append)
    assert len(res) == 6 and all(o.status == "done" for o in res)
    assert prov.peak == 3  # three provider calls in flight at once: really concurrent (no wall-clock bound)
    assert not prov.overlap  # no question handed to two workers at once
    assert db.table_counts(conn)["runs"] == 6 and len(reflections) >= 1  # batches can finish together
    assert not store.select(conn, "questions", "status = 'running'")


def test_parallel_reserves_budget_for_runs_in_flight(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    store.insert(conn, "runs", lane="harvest", provider="x", model="claude-opus-5-5", status="done", cost_usd=2.0)
    charter = _charter(budget={"max_usd_per_day": 5.0, "max_usd_per_run": 1.0})
    prov = SlowFake(6, tokens=(250_000, 0))  # $1.00 per run at claude-opus-5-5 input prices
    res = run_cycles(conn, charter, prov, tmp_path, cycles=6, seed=2, workers=4)
    # $2 spent; each run holds $1 while in flight, so three start, then the day is spent
    assert len(res) == 3 and prov.peak == 3
    assert "daily cap" in (res.stopped_because or "")
    assert pricing.spend(conn)["usd_today"] == pytest.approx(5.0)


def test_last_run_gets_only_what_is_left(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    charter = _charter(budget={"max_usd_total": 0.5, "max_usd_per_run": 2.0, "max_usd_per_day": None})
    allowance, why = pricing.run_allowance(charter.budget, conn)
    assert why is None and allowance == 0.5
    store.insert(conn, "runs", lane="harvest", provider="x", model="m", status="done", cost_usd=0.495)
    allowance, why = pricing.run_allowance(charter.budget, conn)
    assert allowance is None and "total cap" in why


# -- reporting --------------------------------------------------------------------------------

def test_status_and_digest_show_spend(tmp_path, capsys):
    main(["--dir", str(tmp_path), "init"])
    conn = db.connect(tmp_path / "research.db")
    store.insert(conn, "runs", lane="harvest", provider="openai", model="m1", status="done", cost_usd=0.75,
                 input_tokens=1000, output_tokens=100)
    store.insert(conn, "runs", lane="deepen", provider="openai", model="m2", status="done", cost_usd=None,
                 input_tokens=10, output_tokens=1)
    capsys.readouterr()
    assert main(["--dir", str(tmp_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "spend: $0.75 today" in out and "m1" in out and "1 runs have no price" in out
    assert "max_usd_per_day 20.0" in out
    d = reflect(conn, _charter()).digest
    assert "Spend by lane: harvest $0.75" in d and "of $20.00" in d


def test_cli_run_refuses_unpriced_and_accepts_budget_flag(tmp_path, capsys, monkeypatch):
    main(["--dir", str(tmp_path), "init"])
    import mathresearch.providers as providers
    monkeypatch.setattr(providers, "make_provider", lambda name, model: PricedFake([_final()], model=model or "x"))
    assert main(["--dir", str(tmp_path), "run", "--cycles", "1", "--provider", "fake", "--model", "gpt-unknown"]) == 2
    assert "no price for model 'gpt-unknown'" in capsys.readouterr().err
    assert main(["--dir", str(tmp_path), "run", "--cycles", "1", "--provider", "fake", "--model", "claude-opus-5-5",
                 "--workers", "1", "--budget-usd", "0.5"]) == 0
    out = capsys.readouterr().out
    assert "1 cycles, 0 failed" in out and "this invocation" in out


# -- stopping with Ctrl+C -------------------------------------------------------------

class InterruptAfter(PricedFake):
    """Answers its scripted responses, then behaves as if the operator pressed Ctrl+C."""
    def complete(self, request):
        if not self.responses:
            raise KeyboardInterrupt
        return super().complete(request)


def test_ctrl_c_records_spend_and_reopens_the_question(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    qid = store.insert(conn, "questions", kind="survey", lane="harvest", status="open", title="Q")
    with pytest.raises(KeyboardInterrupt):
        run_question(conn, _charter(), InterruptAfter([_call((10_000, 1_000))]), store.get(conn, "questions", qid),
                     tmp_path)
    assert store.get(conn, "questions", qid)["status"] == "open"
    run = store.select(conn, "runs")[0]
    assert run["status"] == "cancelled" and "interrupted" in run["error"]
    assert run["input_tokens"] == 10_000 and run["cost_usd"] == pytest.approx((10_000 * 4 + 1_000 * 20) / 1e6)
    assert pricing.spend(conn)["usd_total"] == pytest.approx(0.06)


def test_next_run_recovers_what_a_killed_process_left_running(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    stuck = store.insert(conn, "questions", kind="survey", lane="harvest", status="running", title="Stuck")
    store.insert(conn, "runs", question_id=stuck, lane="harvest", provider="x", model="claude-opus-5-5",
                 status="running")
    res = run_cycles(conn, _charter(), PricedFake([_final()]), tmp_path, cycles=1, seed=1)
    assert any("1 unfinished runs marked cancelled, 1 questions reopened" in w for w in res.warnings)
    statuses = {r["status"] for r in store.select(conn, "runs")}
    assert "running" not in statuses and "cancelled" in statuses
    assert store.get(conn, "questions", stuck)["status"] != "running"
    s = pricing.spend(conn)
    assert s["unrecorded_runs"] == 1 and s["unpriced_runs"] == 0


def test_cli_ctrl_c_exits_cleanly(tmp_path, capsys, monkeypatch):
    main(["--dir", str(tmp_path), "init"])
    capsys.readouterr()
    import mathresearch.loop as loop_mod

    def interrupted(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(loop_mod, "run_cycles", interrupted)
    assert main(["--dir", str(tmp_path), "run", "--provider", "fake", "--allow-unpriced"]) == 130
    assert "recorded as cancelled" in capsys.readouterr().err
