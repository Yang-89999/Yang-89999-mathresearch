"""Provider-neutral request and response types.

A worker speaks to a model through this interface only. Each adapter
translates these types to one vendor's wire format and back. The shape is the
common subset of the OpenAI Responses API and the Claude Messages API: a
system prompt, alternating messages holding text, tool calls and tool
results, a list of JSON-schema tools, and an optional JSON schema for the
final answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: str = "tool_result"


Block = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: list[Block]
    raw: Any = None  # provider-native items to replay verbatim (e.g. reasoning items)

    def text(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextBlock))


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]

    def strict_schema(self) -> dict[str, Any]:
        return strictify(self.input_schema)


@dataclass
class Usage:
    """Token counts, normalised across providers: ``input_tokens`` is the whole prompt including the
    cached part; ``cached_input_tokens`` were read from the prompt cache, ``cache_write_tokens`` written to it."""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.cache_write_tokens += other.cache_write_tokens

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ChatRequest:
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    model: str | None = None
    max_tokens: int = 16000
    effort: str | None = None  # low | medium | high


@dataclass
class ChatResponse:
    blocks: list[Block]
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens" | "refusal" | "other"
    usage: Usage = field(default_factory=Usage)
    raw: Any = None

    @property
    def tool_calls(self) -> list[ToolUseBlock]:
        return [b for b in self.blocks if isinstance(b, ToolUseBlock)]

    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    def as_message(self) -> Message:
        return Message(role="assistant", content=list(self.blocks), raw=self.raw)


class Provider(Protocol):
    name: str
    default_model: str

    def complete(self, request: ChatRequest) -> ChatResponse: ...


def strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``schema`` in the strict subset both vendors accept.

    Every object gets ``additionalProperties: false`` and all of its
    properties listed in ``required``. Keep schemas flat and null-free by
    construction; this function only enforces the two mechanical rules.
    """
    out = json.loads(json.dumps(schema))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(out)
    return out


class FakeProvider:
    """Scripted provider for tests and offline runs.

    ``responses`` is consumed in order; each entry is a ``ChatResponse``.
    Every request is recorded in ``requests`` for assertions.
    """

    name = "fake"
    default_model = "fake-1"

    def __init__(self, responses: list[ChatResponse] | None = None):
        import threading

        self.responses = list(responses or [])
        self.requests: list[ChatRequest] = []
        self._lock = threading.Lock()

    def complete(self, request: ChatRequest) -> ChatResponse:
        snapshot = ChatRequest(system=request.system, messages=list(request.messages), tools=list(request.tools),
                               output_schema=request.output_schema, model=request.model,
                               max_tokens=request.max_tokens, effort=request.effort)
        with self._lock:
            self.requests.append(snapshot)
            if not self.responses:
                raise RuntimeError("FakeProvider ran out of scripted responses")
            return self.responses.pop(0)

    @staticmethod
    def final(data: dict[str, Any]) -> ChatResponse:
        return ChatResponse(blocks=[TextBlock(json.dumps(data))], stop_reason="end_turn",
                            usage=Usage(input_tokens=100, output_tokens=50))

    @staticmethod
    def call(name: str, input: dict[str, Any], id_: str = "call_1") -> ChatResponse:
        return ChatResponse(blocks=[ToolUseBlock(id=id_, name=name, input=input)], stop_reason="tool_use",
                            usage=Usage(input_tokens=100, output_tokens=20))
