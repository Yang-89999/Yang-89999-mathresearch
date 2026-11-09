import json
from types import SimpleNamespace as NS

from mathresearch.providers import anthropic_messages as am
from mathresearch.providers import openai_responses as oa
from mathresearch.providers.base import (ChatRequest, Message, TextBlock, ToolResultBlock, ToolSpec, ToolUseBlock,
                                         strictify)

TOOL = ToolSpec(name="search_units", description="search",
                input_schema={"type": "object", "properties": {"query": {"type": "string"},
                                                               "limit": {"type": "integer"}}})
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"},
                                           "items": {"type": "array", "items": {
                                               "type": "object", "properties": {"a": {"type": "string"}}}}}}


def _request():
    return ChatRequest(
        system="SYSTEM", tools=[TOOL], output_schema=SCHEMA, effort="high", max_tokens=5000,
        messages=[
            Message("user", [TextBlock("hello")]),
            Message("assistant", [ToolUseBlock(id="c1", name="search_units", input={"query": "Groebner"})]),
            Message("user", [ToolResultBlock(tool_use_id="c1", content="hits", is_error=False)]),
        ],
    )


def test_strictify_marks_every_object():
    s = strictify(SCHEMA)
    assert s["additionalProperties"] is False and s["required"] == ["summary", "items"]
    inner = s["properties"]["items"]["items"]
    assert inner["additionalProperties"] is False and inner["required"] == ["a"]
    assert "additionalProperties" not in SCHEMA  # original untouched


def test_anthropic_wire_shape():
    kw = am.to_wire(_request(), "claude-opus-5-5")
    assert kw["model"] == "claude-opus-5-5"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["tools"][0]["strict"] is True and kw["tools"][0]["input_schema"]["required"] == ["query", "limit"]
    assert kw["output_config"]["effort"] == "high"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in kw  # adaptive by default on current models
    msgs = kw["messages"]
    assert msgs[1]["content"][0] == {"type": "tool_use", "id": "c1", "name": "search_units", "input": {"query": "Groebner"}}
    assert msgs[2]["content"][0]["type"] == "tool_result" and msgs[2]["content"][0]["tool_use_id"] == "c1"


def test_anthropic_from_wire_tool_use_and_usage():
    resp = NS(content=[NS(type="thinking", thinking=""), NS(type="text", text="Let me search."),
                       NS(type="tool_use", id="t1", name="search_units", input={"query": "MinRank"})],
              stop_reason="tool_use", usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=7,
                                                cache_creation_input_tokens=3))
    out = am.from_wire(resp)
    assert out.stop_reason == "tool_use" and out.tool_calls[0].name == "search_units"
    assert out.usage.cached_input_tokens == 7 and out.usage.cache_write_tokens == 3
    assert out.usage.input_tokens == 20  # normalised to the whole prompt
    # raw content is replayed verbatim on the next turn, thinking included
    replayed = am.to_wire(ChatRequest(system="s", messages=[out.as_message()]), "m")["messages"][0]
    assert replayed["content"] == resp.content


def test_openai_wire_shape():
    kw = oa.to_wire(_request(), "gpt-5")
    assert kw["store"] is False and kw["instructions"] == "SYSTEM"
    assert kw["include"] == ["reasoning.encrypted_content"]
    assert kw["reasoning"] == {"effort": "high"}
    fmt = kw["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and fmt["schema"]["additionalProperties"] is False
    assert kw["tools"][0] == {"type": "function", "name": "search_units", "description": "search",
                              "parameters": strictify(TOOL.input_schema), "strict": True}
    items = kw["input"]
    assert items[0] == {"role": "user", "content": "hello"}
    assert items[1]["type"] == "function_call" and json.loads(items[1]["arguments"]) == {"query": "Groebner"}
    assert items[2] == {"type": "function_call_output", "call_id": "c1", "output": "hits"}


def test_openai_from_wire_function_call_and_replay():
    item1 = NS(type="reasoning", encrypted_content="xxx", model_dump=lambda exclude_none: {"type": "reasoning", "encrypted_content": "xxx"})
    item2 = NS(type="function_call", call_id="fc1", name="run_script", arguments='{"code": "print(1)"}',
               model_dump=lambda exclude_none: {"type": "function_call", "call_id": "fc1", "name": "run_script",
                                                "arguments": '{"code": "print(1)"}'})
    resp = NS(output=[item1, item2], status="completed",
              usage=NS(input_tokens=3, output_tokens=4, input_tokens_details=NS(cached_tokens=2)))
    out = oa.from_wire(resp)
    assert out.stop_reason == "tool_use" and out.tool_calls[0].input == {"code": "print(1)"}
    assert out.usage.cached_input_tokens == 2
    items = oa.to_wire(ChatRequest(system="s", messages=[out.as_message()]), "m")["input"]
    assert items[0]["type"] == "reasoning" and items[1]["type"] == "function_call"


def test_openai_from_wire_final_text_and_truncation():
    msg = NS(type="message", content=[NS(type="output_text", text='{"summary": "x"}')],
             model_dump=lambda exclude_none: {})
    out = oa.from_wire(NS(output=[msg], status="completed", usage=None))
    assert out.stop_reason == "end_turn" and out.text() == '{"summary": "x"}'
    out = oa.from_wire(NS(output=[msg], status="incomplete", incomplete_details=NS(reason="max_output_tokens"),
                          usage=None))
    assert out.stop_reason == "max_tokens"
