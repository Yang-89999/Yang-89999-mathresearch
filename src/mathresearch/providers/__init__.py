"""Model providers behind one interface."""

from __future__ import annotations

import os

from .base import (ChatRequest, ChatResponse, FakeProvider, Message, Provider, TextBlock, ToolResultBlock,
                   ToolSpec, ToolUseBlock, Usage, strictify)

__all__ = [
    "ChatRequest", "ChatResponse", "FakeProvider", "Message", "Provider", "TextBlock", "ToolResultBlock",
    "ToolSpec", "ToolUseBlock", "Usage", "strictify", "make_provider",
]


def make_provider(name: str | None = None, model: str | None = None) -> Provider:
    """Instantiate a provider by name (``anthropic``, ``openai`` or ``fake``).

    Defaults come from ``MATHRESEARCH_PROVIDER`` and ``MATHRESEARCH_MODEL``;
    if neither is set, the provider is chosen from whichever API key exists.
    """
    name = name or os.environ.get("MATHRESEARCH_PROVIDER")
    model = model or os.environ.get("MATHRESEARCH_MODEL")
    if not name:
        if os.environ.get("ANTHROPIC_API_KEY"):
            name = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            name = "openai"
        else:
            raise RuntimeError("no provider configured: set MATHRESEARCH_PROVIDER or an API key")
    if name == "anthropic":
        from .anthropic_messages import AnthropicProvider

        return AnthropicProvider(model=model)
    if name == "openai":
        from .openai_responses import OpenAIProvider

        return OpenAIProvider(model=model)
    if name == "fake":
        return FakeProvider()
    raise ValueError(f"unknown provider {name!r}")
