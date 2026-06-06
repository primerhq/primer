"""Deterministic OpenAI-compatible mock the e2e server calls over HTTP.

The test process owns the ScriptRegistry in-process; the separate primer
server reaches the app over HTTP. Responses are a pure function of the
request (rule matching), so loops + concurrent fan-out stay deterministic.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route


@dataclass
class Rule:
    when_last_user_contains: str | None = None
    when_tool_result: bool | None = None
    when_tool_offered: str | None = None
    emit_text: str | None = None
    emit_tool: str | None = None
    emit_args: dict[str, Any] = field(default_factory=dict)

    def matches(self, req: dict[str, Any]) -> bool:
        msgs = req.get("messages", [])
        last_user = next(
            (m for m in reversed(msgs) if m.get("role") == "user"), {}
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

    async def chat(req: Request) -> StreamingResponse:
        body = await req.json()
        model = body.get("model", "scripted:default")
        rule = registry.resolve(body)

        async def gen():
            yield _chunk(model, {"role": "assistant"})
            if rule.emit_tool:
                tc = [
                    {
                        "index": 0,
                        "id": "call_0",
                        "type": "function",
                        "function": {
                            "name": rule.emit_tool,
                            "arguments": json.dumps(rule.emit_args),
                        },
                    }
                ]
                yield _chunk(model, {"tool_calls": tc})
                yield _chunk(model, {}, finish="tool_calls")
            else:
                yield _chunk(model, {"content": rule.emit_text or "ok"})
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
