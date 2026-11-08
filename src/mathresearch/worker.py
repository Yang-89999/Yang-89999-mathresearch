"""One worker run: assemble context, loop over tools, write back.

A run exists for one question and ends with a structured write-back. The
conversation is discarded afterwards; only the write-back persists.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import store, writeback
from .charter import Charter
from .context import system_prompt, user_prompt
from .providers.base import ChatRequest, Message, Provider, TextBlock, ToolResultBlock, Usage
from .tools import registry

EFFORT_BY_LANE = {"harvest": "low", "deepen": "high", "open": "medium"}
MAX_TOKENS_BY_LANE = {"harvest": 8000, "deepen": 16000, "open": 12000}


@dataclass
class RunOutcome:
    run_id: str
    question_id: str
    status: str  # done | failed
    steps: int
    usage: Usage
    created: dict[str, list[str]]
    error: str | None = None


def run_question(conn: sqlite3.Connection, charter: Charter, provider: Provider, question: dict[str, Any],
                 workspace: Path, max_steps: int = 12, model: str | None = None) -> RunOutcome:
    lane = question["lane"]
    run_id = store.insert(conn, "runs", question_id=question["id"], lane=lane, provider=provider.name,
                          model=model or provider.default_model, status="running")
    store.update(conn, "questions", question["id"], status="running")
    ctx = registry.ToolContext(conn=conn, workspace=workspace, question=question, run_id=run_id)
    usage = Usage()
    messages = [Message(role="user", content=[TextBlock(user_prompt(conn, question))])]
    system = system_prompt(charter, lane)
    error: str | None = None
    data: dict[str, Any] | None = None
    steps = 0

    try:
        while steps < max_steps:
            steps += 1
            response = provider.complete(ChatRequest(
                system=system, messages=messages, tools=registry.specs(),
                output_schema=writeback.WRITEBACK_SCHEMA, model=model,
                max_tokens=MAX_TOKENS_BY_LANE.get(lane, 12000), effort=EFFORT_BY_LANE.get(lane),
            ))
            usage.add(response.usage)
            messages.append(response.as_message())

            if response.stop_reason == "tool_use" and response.tool_calls:
                results = []
                for call in response.tool_calls:
                    text, is_error = registry.execute(call.name, call.input, ctx)
                    results.append(ToolResultBlock(tool_use_id=call.id, content=text, is_error=is_error))
                messages.append(Message(role="user", content=results))
                continue

            if response.stop_reason == "refusal":
                error = "model refused: " + response.text()[:500]
                break
            if response.stop_reason == "max_tokens":
                messages.append(Message(role="user", content=[TextBlock(
                    "Your reply was cut off. Return only the write-back JSON now, briefly.")]))
                continue

            data = _parse_writeback(response.text())
            if data is None:
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
    store.update(conn, "runs", run_id, status=status, input_tokens=usage.input_tokens,
                 output_tokens=usage.output_tokens, result=data, error=error,
                 context_ids={"experiments": ctx.experiment_ids},
                 finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return RunOutcome(run_id=run_id, question_id=question["id"], status=status, steps=steps, usage=usage,
                      created=created, error=error)


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
