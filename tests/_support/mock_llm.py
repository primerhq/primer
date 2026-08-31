"""Deterministic OpenAI-compatible mock the e2e server calls over HTTP.

The test process owns the ScriptRegistry in-process; the separate primer
server reaches the app over HTTP. Responses are a pure function of the
request (rule matching), so loops + concurrent fan-out stay deterministic.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


@dataclass
class Rule:
    when_last_user_contains: str | None = None
    when_tool_result: bool | None = None
    when_tool_offered: str | None = None
    when_last_tool_result_contains: str | None = None
    emit_text: str | None = None
    emit_tool: str | None = None
    emit_args: dict[str, Any] = field(default_factory=dict)
    # Tool-call id to emit. Defaults to ``"call_0"`` (the historical fixed id
    # most tests rely on). Override it when a single chat/session issues
    # SEVERAL tool calls that must be told apart by id on resume -- e.g. a
    # chat that searches, asks, then later trips an approval gate: a fixed id
    # makes the resume's id-based reply lookup match the FIRST call_0, not the
    # gated one. Real providers always mint unique ids, so this only matters
    # for the scripted mock.
    emit_tool_call_id: str | None = None
    # When >= 400, the chat handler returns a JSON error with this status
    # instead of a streamed 200 (lets a scenario model force e.g. a 429).
    emit_status: int = 200
    emit_error_message: str | None = None
    # Slow-streaming support (01a04d91-a7a0, refresh-mid-turn diagnosis):
    # every default rule above resolves in a single event loop tick, which
    # can never reproduce what a real multi-second LLM call does to
    # turn_status/the UI's rowBusy gate. chunk_delay_s sleeps between EACH
    # emitted SSE chunk (the role-preamble, every word-chunk of emit_text,
    # and before the tool_calls chunk); text_chunk_words splits emit_text
    # into that many words per chunk instead of one lump. Both default to
    # 0/unset so every existing rule keeps resolving instantly.
    chunk_delay_s: float = 0.0
    text_chunk_words: int = 0

    def matches(self, req: dict[str, Any]) -> bool:
        msgs = req.get("messages", [])
        last_user = next(
            (m for m in reversed(msgs) if m.get("role") == "user"), {}
        )
        last_tool = next(
            (m for m in reversed(msgs) if m.get("role") == "tool"), {}
        )
        has_tool_result = any(m.get("role") == "tool" for m in msgs)
        offered = {
            t.get("function", {}).get("name") for t in req.get("tools", [])
        }
        if (
            self.when_last_user_contains
            and self.when_last_user_contains not in str(last_user.get("content", ""))
        ):
            return False
        if (
            self.when_tool_result is not None
            and bool(has_tool_result) != self.when_tool_result
        ):
            return False
        if self.when_tool_offered and not any(
            self.when_tool_offered in (name or "") for name in offered
        ):
            return False
        # Discriminate sequential tool-call chains by the content of the most
        # recent tool-role message (e.g. a watch_files resume vs a
        # put_document result), which user/offered predicates cannot see.
        if (
            self.when_last_tool_result_contains
            and self.when_last_tool_result_contains
            not in str(last_tool.get("content", ""))
        ):
            return False
        return True


class ScriptRegistry:
    def __init__(self) -> None:
        self._scripts: dict[str, list[Rule]] = {}
        self.strict = False
        self.requests: list[dict] = []  # captured for debugging

    def register(self, scenario_id: str, rules: list[Rule]) -> None:
        self._scripts[scenario_id] = rules

    def clear(self) -> None:
        self._scripts.clear()

    def models(self) -> list[str]:
        return list(self._scripts.keys()) or ["scripted:default"]

    def resolve(self, req: dict[str, Any]) -> Rule:
        self.requests.append(req)
        rules = self._scripts.get(req.get("model", ""), [])
        for r in rules:
            if r.matches(req):
                return r
        if self.strict:
            raise AssertionError(
                f"no scripted rule matched model={req.get('model')!r}"
            )
        return Rule(emit_text="ok")  # permissive default


def _chunk(model: str, delta: dict, finish: str | None = None) -> str:
    payload = {
        "id": "mock",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def build_app(registry: ScriptRegistry) -> Starlette:
    async def models(_req: Request) -> JSONResponse:
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": m, "object": "model"} for m in registry.models()
                ],
            }
        )

    async def chat(req: Request) -> Response:
        body = await req.json()
        model = body.get("model", "scripted:default")
        rule = registry.resolve(body)

        if rule.emit_status >= 400:
            return JSONResponse(
                {"error": {
                    "message": rule.emit_error_message or "scripted error",
                    "type": "rate_limit_error" if rule.emit_status == 429 else "error",
                }},
                status_code=rule.emit_status,
            )

        async def gen():
            yield _chunk(model, {"role": "assistant"})
            if rule.chunk_delay_s:
                await asyncio.sleep(rule.chunk_delay_s)
            if rule.emit_tool:
                tc = [
                    {
                        "index": 0,
                        "id": rule.emit_tool_call_id or "call_0",
                        "type": "function",
                        "function": {
                            "name": rule.emit_tool,
                            "arguments": json.dumps(rule.emit_args),
                        },
                    }
                ]
                yield _chunk(model, {"tool_calls": tc})
                if rule.chunk_delay_s:
                    # Real, pollable gap between the tool-calls delta and
                    # the finish_reason chunk (01a04d91-a7a0): a
                    # zero-gap pair collapses ToolCallStart+ToolCallEnd
                    # (primer.llm._openai_compat._translate_chunk emits
                    # Start on the delta chunk, End on the finish_reason
                    # chunk) into sub-millisecond real time - long enough
                    # for a live tap frame to catch but invisible to any
                    # REST poll, which is exactly how the first version of
                    # this e2e regression test silently never observed
                    # agent_phase="executing" despite the tool call
                    # genuinely happening (confirmed via the durable
                    # tool_call/tool_result records).
                    await asyncio.sleep(rule.chunk_delay_s)
                yield _chunk(model, {}, finish="tool_calls")
            else:
                text = rule.emit_text or "ok"
                words = text.split(" ") if rule.text_chunk_words else [text]
                step = rule.text_chunk_words or len(words)
                for i in range(0, len(words), step):
                    piece = " ".join(words[i:i + step])
                    if i > 0:
                        piece = " " + piece
                    yield _chunk(model, {"content": piece})
                    if rule.chunk_delay_s and i + step < len(words):
                        await asyncio.sleep(rule.chunk_delay_s)
                yield _chunk(model, {}, finish="stop")
            usage = {
                "id": "mock",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
            yield f"data: {json.dumps(usage)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return Starlette(
        routes=[
            Route("/v1/models", models, methods=["GET"]),
            Route("/v1/chat/completions", chat, methods=["POST"]),
        ]
    )


def slow_turn_with_mid_stream_tool_call(
    *,
    tool_name: str = "misc__uuid_v4",
    tool_args: dict[str, Any] | None = None,
    final_text: str = (
        "Based on what the tool returned, here is my detailed answer, "
        "explained step by step so the reasoning is easy to follow."
    ),
    total_seconds: float = 15.0,
) -> list[Rule]:
    """Two rules that together reproduce a genuinely long-running real
    turn: 01a04d64-b4ba's live diagnosis needed exactly this shape (a
    turn spanning 10-20s across two real LLM round-trips with a tool
    call in between) and had no way to get it except a real, rate-
    limited, sometimes-unreachable provider. Register on a model id with
    ``registry.register(model_id, slow_turn_with_mid_stream_tool_call())``
    and bind an agent/profile to a llm_providers row pointed at the
    mock_llm fixture's base_url (or a standalone run of this module, see
    scripts/e2e/run_mock_llm.py, for a real HTTP round-trip against a
    live :8765-style stack).

    A single OpenAI-shaped response is either a text message or a
    tool-calls message (this mock's ``gen()`` mirrors that split), so the
    "mid-stream tool call" is spread across the two real round-trips a
    tool-calling turn actually makes, matching what the live diagnosis
    observed (two separate real "OpenChat stream starting" log lines
    ~25s apart): round-trip 1 spends ~half of *total_seconds* split into
    two real, independently-pollable gaps - a "thinking" gap before the
    tool-calls chunk (a client polling mid-thought sees genuine business,
    not a reasoning delta - reasoning-capable providers would
    additionally stream reasoning deltas here, which this mock does not
    model), then an "executing" gap between the tool-calls delta and the
    finish_reason chunk (mock_llm.py's ``gen()`` - without this second
    gap, primer.llm._openai_compat._translate_chunk's ToolCallStart and
    ToolCallEnd collapse into sub-millisecond real time, long enough for
    a live tap frame but invisible to a REST poll) - then calls
    *tool_name*; round-trip 2, triggered once the tool result is back
    (``when_tool_result=True``), streams the final answer as slow
    multi-word chunks for the remaining ~half.
    """
    half = max(1.0, total_seconds / 2)
    words_per_chunk = 3
    final_chunk_count = max(1, len(final_text.split(" ")) / words_per_chunk)
    return [
        Rule(
            when_tool_result=False,
            emit_tool=tool_name,
            emit_args=tool_args or {},
            # Split in two: chunk_delay_s gaps BOTH the pre-tool-call
            # "thinking" wait and the tool-calls-delta -> finish_reason
            # "executing" wait (see gen()), so round-trip 1's total wall
            # time still lands at ~half, not 2x it.
            chunk_delay_s=half / 2,
        ),
        Rule(
            when_tool_result=True,
            emit_text=final_text,
            chunk_delay_s=half / final_chunk_count,
            text_chunk_words=words_per_chunk,
        ),
    ]
