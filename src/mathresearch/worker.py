"""One worker run: assemble context, loop over tools, write back.

A run exists for one question and ends with a structured write-back. The
conversation is discarded afterwards; only the write-back persists.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import pricing, store, writeback
from .charter import Charter
from .context import system_prompt, user_prompt
from .providers.base import ChatRequest, Message, Provider, TextBlock, ToolResultBlock, Usage
from .tools import registry

EFFORT_BY_LANE = {"harvest": "low", "deepen": "high", "open": "medium"}
MAX_TOKENS_BY_LANE = {"harvest": 8000, "deepen": 16000, "open": 12000}


WRAP_UP = ("The budget for this run is spent ({reason}). Do not call any more tools. Return the write-back JSON "
           "now: record what you established, and leave the question open with a summary that lets the next run "
           "continue where you stopped.")


@dataclass
class RunOutcome:
    run_id: str
    question_id: str
    status: str  # done | failed
    steps: int
    usage: Usage
    created: dict[str, list[str]]
    error: str | None = None
    cost_usd: float | None = None
    budget_note: str | None = None


def run_question(conn: sqlite3.Connection, charter: Charter, provider: Provider, question: dict[str, Any],
                 workspace: Path, max_steps: int = 12, model: str | None = None,
                 max_usd: float | None = None) -> RunOutcome:
    """Run one worker on ``question``. ``max_usd`` lowers the charter's per-run dollar cap for this run
    (the loop passes what is left under the daily and total caps)."""
    budget = charter.budget
    if max_usd is not None and (budget.max_usd_per_run is None or max_usd < budget.max_usd_per_run):
        budget = dataclasses.replace(budget, max_usd_per_run=max_usd)
    lane = question["lane"]
    model_id = model or getattr(provider, "model", None) or provider.default_model
    run_id = store.insert(conn, "runs", question_id=question["id"], lane=lane, provider=provider.name,
                          model=model_id, status="running")
    store.update(conn, "questions", question["id"], status="running")
    ctx = registry.ToolContext(conn=conn, workspace=workspace, question=question, run_id=run_id,
                               hidden_attacks=frozenset(charter.hidden_attacks))
    usage = Usage()
    messages = [Message(role="user", content=[TextBlock(user_prompt(conn, question))])]
    system = system_prompt(charter, lane)
    error: str | None = None
    data: dict[str, Any] | None = None
    steps = 0
    budget_note: str | None = None   # set once the run has hit its cap and been told to wrap up

    try:
        while steps < max_steps + (1 if budget_note else 0):
            steps += 1
            response = provider.complete(ChatRequest(
                system=system, messages=messages, tools=registry.specs(),
                output_schema=writeback.WRITEBACK_SCHEMA, model=model,
                max_tokens=MAX_TOKENS_BY_LANE.get(lane, 12000), effort=EFFORT_BY_LANE.get(lane),
            ))
            usage.add(response.usage)
            messages.append(response.as_message())
            over = None if budget_note else pricing.run_over_budget(
                budget, usage, pricing.cost_of(usage, model_id, charter.pricing))

            if response.stop_reason == "tool_use" and response.tool_calls:
                if budget_note:  # already told to stop: refuse further tool use and end
                    error = f"budget exhausted: {budget_note}; the model kept calling tools"
                    break
                results = []
                for call in response.tool_calls:
                    if over:
                        text, is_error = f"Not run: {over}.", True
                    else:
                        text, is_error = registry.execute(call.name, call.input, ctx)
                    results.append(ToolResultBlock(tool_use_id=call.id, content=text, is_error=is_error))
                content: list[Any] = list(results)
                if over:
                    budget_note = over
                    content.append(TextBlock(WRAP_UP.format(reason=over)))
                messages.append(Message(role="user", content=content))
                continue

            if response.stop_reason == "refusal":
                error = "model refused: " + response.text()[:500]
                break
            if response.stop_reason == "max_tokens":
                if budget_note:
                    error = f"budget exhausted: {budget_note}; no write-back"
                    break
                if over:
                    budget_note = over
                messages.append(Message(role="user", content=[TextBlock(
                    "Your reply was cut off. Return only the write-back JSON now, briefly.")]))
                continue

            data = _parse_writeback(response.text())
            if data is None:
                if budget_note:
                    error = f"budget exhausted: {budget_note}; no valid write-back"
                    break
                if over:
                    budget_note = over
                messages.append(Message(role="user", content=[TextBlock(
                    "That was not valid write-back JSON. Return only the JSON object matching the schema.")]))
                continue
            break
        else:
            if data is None:
                error = f"no write-back after {max_steps} steps"
    except Exception as exc:  # provider/network failures end the run but keep the record
        error = f"{type(exc).__name__}: {exc}"

    if data is not None and error is None:
        try:
            created = writeback.apply(conn, run_id, question, writeback.validate(data))
        except writeback.WritebackError as exc:
            error, created = f"invalid write-back: {exc}", {"findings": [], "questions": [], "problems": []}
    else:
        created = {"findings": [], "questions": [], "problems": []}

    status = "failed" if error else "done"
    if error:
        store.update(conn, "questions", question["id"], status="open")  # leave it on the agenda
    cost = pricing.cost_of(usage, model_id, charter.pricing)
    store.update(conn, "runs", run_id, status=status, input_tokens=usage.input_tokens,
                 output_tokens=usage.output_tokens, cost_usd=cost, result=data, error=error,
                 context_ids={"experiments": ctx.experiment_ids, "cached_input_tokens": usage.cached_input_tokens,
                              "cache_write_tokens": usage.cache_write_tokens, "budget_note": budget_note},
                 finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return RunOutcome(run_id=run_id, question_id=question["id"], status=status, steps=steps, usage=usage,
                      created=created, error=error, cost_usd=cost, budget_note=budget_note)


def _parse_writeback(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None
