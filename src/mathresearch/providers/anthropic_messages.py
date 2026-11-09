"""Claude Messages API adapter.

Structured output uses ``output_config.format`` with a JSON schema; tools are
declared ``strict``. Thinking stays at the model's default (adaptive on
current models); depth is controlled through ``output_config.effort``.
"""

from __future__ import annotations

import json
from typing import Any

from .base import (ChatRequest, ChatResponse, Message, TextBlock, ToolResultBlock, ToolSpec, ToolUseBlock,
                   Usage, strictify)

DEFAULT_MODEL = "claude-opus-5-5"


def to_wire(request: ChatRequest, model: str) -> dict[str, Any]:
    """Build the keyword arguments for ``client.messages.create``."""
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": request.max_tokens,
        "system": [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
        "messages": [_message(m) for m in request.messages],
    }
    if request.tools:
        kwargs["tools"] = [_tool(t) for t in request.tools]
    output_config: dict[str, Any] = {}
    if request.effort:
        output_config["effort"] = request.effort
    if request.output_schema:
        output_config["format"] = {"type": "json_schema", "schema": strictify(request.output_schema)}
    if output_config:
        kwargs["output_config"] = output_config
    return kwargs


def from_wire(response: Any) -> ChatResponse:
    blocks = []
    for b in response.content:
        btype = getattr(b, "type", None)
        if btype == "text":
            blocks.append(TextBlock(b.text))
        elif btype == "tool_use":
            blocks.append(ToolUseBlock(id=b.id, name=b.name, input=dict(b.input)))
        # thinking blocks are kept only through ``raw`` and replayed verbatim
    stop = response.stop_reason or "other"
    if stop not in ("end_turn", "tool_use", "max_tokens", "refusal"):
        stop = "other"
    u = response.usage
    # Anthropic reports uncached input, cache reads and cache writes separately; normalise to a total.
    read = getattr(u, "cache_read_input_tokens", 0) or 0
    written = getattr(u, "cache_creation_input_tokens", 0) or 0
    usage = Usage(input_tokens=(getattr(u, "input_tokens", 0) or 0) + read + written,
                  output_tokens=getattr(u, "output_tokens", 0) or 0,
                  cached_input_tokens=read, cache_write_tokens=written)
    return ChatResponse(blocks=blocks, stop_reason=stop, usage=usage, raw=list(response.content))


def _message(m: Message) -> dict[str, Any]:
    if m.role == "assistant" and m.raw is not None:
        return {"role": "assistant", "content": m.raw}  # replay thinking/tool_use blocks unchanged
    content = []
    for b in m.content:
        if isinstance(b, TextBlock):
            content.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolUseBlock):
            content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, ToolResultBlock):
            item: dict[str, Any] = {"type": "tool_result", "tool_use_id": b.tool_use_id, "content": b.content}
            if b.is_error:
                item["is_error"] = True
            content.append(item)
    return {"role": m.role, "content": content}


def _tool(t: ToolSpec) -> dict[str, Any]:
    return {"name": t.name, "description": t.description, "strict": True, "input_schema": t.strict_schema()}


class AnthropicProvider:
    name = "anthropic"
    default_model = DEFAULT_MODEL

    def __init__(self, model: str | None = None, client: Any = None):
        self.model = model or DEFAULT_MODEL
        if client is None:
            import anthropic  # lazy: optional dependency

            client = anthropic.Anthropic()
        self.client = client

    def complete(self, request: ChatRequest) -> ChatResponse:
        kwargs = to_wire(request, request.model or self.model)
        response = self.client.messages.create(**kwargs)
        return from_wire(response)


def describe(request: ChatRequest) -> str:  # pragma: no cover - debugging aid
    return json.dumps(to_wire(request, DEFAULT_MODEL), indent=2, default=str)
