"""OpenAI Responses API adapter.

Requests are stateless (``store: false``): the worker owns the history and
replays it, including encrypted reasoning items, on every call. Structured
output uses ``text.format`` with a strict JSON schema; functions are strict.
"""

from __future__ import annotations

import json
from typing import Any

from .base import (ChatRequest, ChatResponse, Message, TextBlock, ToolResultBlock, ToolSpec, ToolUseBlock,
                   Usage, strictify)

# Set MATHRESEARCH_MODEL or pass --model to override; OpenAI model ids change often.
DEFAULT_MODEL = "gpt-6-astra"


def to_wire(request: ChatRequest, model: str) -> dict[str, Any]:
    """Build the keyword arguments for ``client.responses.create``."""
    items: list[dict[str, Any]] = []
    for m in request.messages:
        items.extend(_items(m))
    kwargs: dict[str, Any] = {
        "model": model,
        "instructions": request.system,
        "input": items,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": request.max_tokens,
    }
    if request.tools:
        kwargs["tools"] = [_tool(t) for t in request.tools]
    if request.output_schema:
        kwargs["text"] = {"format": {"type": "json_schema", "name": "writeback",
                                      "schema": strictify(request.output_schema), "strict": True}}
    if request.effort:
        kwargs["reasoning"] = {"effort": request.effort}
    return kwargs


def from_wire(response: Any) -> ChatResponse:
    blocks = []
    raw_items = []
    for item in response.output:
        itype = getattr(item, "type", None)
        raw_items.append(item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else item)
        if itype == "message":
            for part in item.content:
                if getattr(part, "type", None) == "output_text":
                    blocks.append(TextBlock(part.text))
                elif getattr(part, "type", None) == "refusal":
                    return ChatResponse(blocks=[TextBlock(part.refusal)], stop_reason="refusal", raw=raw_items)
        elif itype == "function_call":
            try:
                args = json.loads(item.arguments) if item.arguments else {}
            except ValueError:
                args = {"_unparsed": item.arguments}
            blocks.append(ToolUseBlock(id=item.call_id, name=item.name, input=args))
    if any(isinstance(b, ToolUseBlock) for b in blocks):
        stop = "tool_use"
    elif getattr(response, "status", "completed") == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", "")
        stop = "max_tokens" if reason == "max_output_tokens" else "other"
    else:
        stop = "end_turn"
    u = getattr(response, "usage", None)
    cached = 0
    if u is not None and getattr(u, "input_tokens_details", None) is not None:
        cached = getattr(u.input_tokens_details, "cached_tokens", 0) or 0
    usage = Usage(input_tokens=getattr(u, "input_tokens", 0) or 0,
                  output_tokens=getattr(u, "output_tokens", 0) or 0, cached_input_tokens=cached)
    return ChatResponse(blocks=blocks, stop_reason=stop, usage=usage, raw=raw_items)


def _items(m: Message) -> list[dict[str, Any]]:
    if m.role == "assistant" and m.raw is not None:
        return list(m.raw)  # replay output items (reasoning, message, function_call) unchanged
    items: list[dict[str, Any]] = []
    for b in m.content:
        if isinstance(b, TextBlock):
            items.append({"role": m.role, "content": b.text})
        elif isinstance(b, ToolUseBlock):
            items.append({"type": "function_call", "call_id": b.id, "name": b.name,
                          "arguments": json.dumps(b.input)})
        elif isinstance(b, ToolResultBlock):
            items.append({"type": "function_call_output", "call_id": b.tool_use_id,
                          "output": ("ERROR: " if b.is_error else "") + b.content})
    return items


def _tool(t: ToolSpec) -> dict[str, Any]:
    return {"type": "function", "name": t.name, "description": t.description,
            "parameters": t.strict_schema(), "strict": True}


class OpenAIProvider:
    name = "openai"
    default_model = DEFAULT_MODEL

    def __init__(self, model: str | None = None, client: Any = None):
        self.model = model or DEFAULT_MODEL
        if client is None:
            import openai  # lazy: optional dependency

            client = openai.OpenAI()
        self.client = client

    def complete(self, request: ChatRequest) -> ChatResponse:
        kwargs = to_wire(request, request.model or self.model)
        response = self.client.responses.create(**kwargs)
        return from_wire(response)
