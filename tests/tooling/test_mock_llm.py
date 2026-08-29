import json
import time

import httpx
import pytest

from tests._support.mock_llm import (
    build_app,
    ScriptRegistry,
    Rule,
    slow_turn_with_mid_stream_tool_call,
)


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


@pytest.mark.asyncio
async def test_chunk_delay_and_word_splitting_stream_multiple_content_chunks():
    """01a04d91-a7a0: chunk_delay_s/text_chunk_words are the primitive a
    slow, realistic (multi-second, multi-chunk) turn needs - the refresh-
    mid-turn diagnosis (01a04d64-b4ba) had no way to produce one without a
    real, rate-limited, sometimes-unreachable provider. Content must still
    arrive concatenated correctly across chunks, and the real delay must
    actually elapse (not just be accepted as a no-op parameter)."""
    reg = ScriptRegistry()
    reg.register(
        "scripted:slow",
        [Rule(
            emit_text="one two three four five six",
            text_chunk_words=2,
            chunk_delay_s=0.05,
        )],
    )
    transport = httpx.ASGITransport(app=build_app(reg))
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        body = {
            "model": "scripted:slow",
            "stream": True,
            "messages": [{"role": "user", "content": "go"}],
        }
        t0 = time.monotonic()
        chunks = await _collect_sse(c, body)
        elapsed = time.monotonic() - t0

        texts = [
            ch["choices"][0]["delta"].get("content")
            for ch in chunks
            if ch.get("choices") and ch["choices"][0].get("delta", {}).get("content")
        ]
        assert texts == ["one two", " three four", " five six"]
        assert "".join(texts) == "one two three four five six"
        # 2 delays before the 2nd/3rd content chunks (none after the last,
        # none before the role-preamble's own single upfront sleep) -
        # loose bound, just proving real time elapsed, not a busy-loop.
        assert elapsed >= 0.05, (
            f"chunk_delay_s did not actually delay anything: {elapsed:.3f}s"
        )


@pytest.mark.asyncio
async def test_slow_turn_with_mid_stream_tool_call_produces_a_real_tool_call_then_final_text():
    """The reusable preset (registered for real e2e/diagnostic use via
    ``registry.register(model_id, slow_turn_with_mid_stream_tool_call())``)
    must actually produce a tool call on the first round-trip and a
    final text answer once the tool result comes back on the second -
    the exact two-round-trip shape a real tool-calling turn takes."""
    reg = ScriptRegistry()
    reg.register(
        "scripted:slow-tool",
        slow_turn_with_mid_stream_tool_call(
            tool_name="misc__uuid_v4", total_seconds=0.2,
        ),
    )
    transport = httpx.ASGITransport(app=build_app(reg))
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        t0 = time.monotonic()
        first = await _collect_sse(c, {
            "model": "scripted:slow-tool",
            "stream": True,
            "messages": [{"role": "user", "content": "go"}],
            "tools": [{"type": "function", "function": {"name": "misc__uuid_v4"}}],
        })
        first_elapsed = time.monotonic() - t0
        tool_calls = [
            ch["choices"][0]["delta"]["tool_calls"]
            for ch in first
            if ch.get("choices") and ch["choices"][0].get("delta", {}).get("tool_calls")
        ]
        assert tool_calls and tool_calls[0][0]["function"]["name"] == "misc__uuid_v4"
        assert first[-2]["choices"][0]["finish_reason"] == "tool_calls"
        # ~half of total_seconds spent thinking before the tool call.
        assert first_elapsed >= 0.05

        t1 = time.monotonic()
        second = await _collect_sse(c, {
            "model": "scripted:slow-tool",
            "stream": True,
            "messages": [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": ""},
                {"role": "tool", "content": "a-real-uuid"},
            ],
        })
        second_elapsed = time.monotonic() - t1
        final_text = "".join(
            ch["choices"][0]["delta"].get("content", "")
            for ch in second
            if ch.get("choices")
        )
        assert final_text, "second round-trip must stream a real final answer"
        assert second[-2]["choices"][0]["finish_reason"] == "stop"
        assert second_elapsed >= 0.05
        # Both round-trips together land in the requested ballpark rather
        # than resolving instantly (the whole point of the asset).
        assert first_elapsed + second_elapsed >= 0.15
