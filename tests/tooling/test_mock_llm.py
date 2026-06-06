import json

import httpx
import pytest

from tests._support.mock_llm import build_app, ScriptRegistry, Rule


async def _collect_sse(client, body):
    out = []
    async with client.stream("POST", "/v1/chat/completions", json=body) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: ") and not line.endswith("[DONE]"):
                out.append(json.loads(line[6:]))
    return out


@pytest.mark.asyncio
async def test_models_lists_scenarios():
    reg = ScriptRegistry()
    reg.register("scripted:demo", [Rule(emit_text="hi")])
    transport = httpx.ASGITransport(app=build_app(reg))
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        r = await c.get("/v1/models")
        assert any(m["id"] == "scripted:demo" for m in r.json()["data"])


@pytest.mark.asyncio
async def test_streams_text_then_toolcall_by_rule():
    reg = ScriptRegistry()
    reg.register(
        "scripted:demo",
        [
            Rule(when_tool_offered="echo", emit_tool="echo", emit_args={"x": 1}),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    transport = httpx.ASGITransport(app=build_app(reg))
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        # tool offered -> emits a tool_call, finishes with tool_calls
        body = {
            "model": "scripted:demo",
            "stream": True,
            "messages": [{"role": "user", "content": "go"}],
            "tools": [{"type": "function", "function": {"name": "echo"}}],
        }
        chunks = await _collect_sse(c, body)
        assert any(
            ch.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
            for ch in chunks
        )
        assert chunks[-2]["choices"][0]["finish_reason"] == "tool_calls"
        # after a tool result, the second rule emits final text + stop
        body2 = {
            "model": "scripted:demo",
            "stream": True,
            "messages": [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": ""},
                {"role": "tool", "content": "echoed"},
            ],
        }
        chunks2 = await _collect_sse(c, body2)
        texts = "".join(
            ch["choices"][0]["delta"].get("content", "")
            for ch in chunks2
            if ch.get("choices")
        )
        assert "done" in texts
        assert chunks2[-2]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_strict_mode_raises_on_unmatched():
    reg = ScriptRegistry()
    reg.strict = True
    reg.register("scripted:demo", [Rule(when_last_user_contains="never")])
    with pytest.raises(AssertionError):
        reg.resolve({"model": "scripted:demo", "messages": [{"role": "user", "content": "x"}]})


@pytest.mark.asyncio
async def test_permissive_default_when_not_strict():
    reg = ScriptRegistry()
    rule = reg.resolve({"model": "unknown", "messages": []})
    assert rule.emit_text == "ok"
