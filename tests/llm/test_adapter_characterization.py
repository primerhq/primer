"""Characterization of every provider adapter through its real SDK (Part 2 T50, T51).

Each adapter is driven through the SDK it wraps, with an in-process transport in place of the network, and the test pins
what is observable today: the request body on the wire, the translated stream (text and one tool call), the error class
and ``code`` for the statuses the classifiers already map, and the number of HTTP attempts a ``RetryingLLM`` makes for a
5xx that recovers (the SDK's own retry and the wrapper's retry both land on two). These must hold unchanged through the
retry, timeout and stream fixes. Every stream test runs with and without a connect timeout, because the timeout fixes
change the opening path only when one is set.
"""

from __future__ import annotations

import functools
import importlib
import json
from collections.abc import Callable
from typing import Any

import httpx
import ollama
import pytest
from anthropic import AsyncAnthropic
from google import genai
from google.genai import types as gtypes
from openai import AsyncOpenAI
from pydantic import HttpUrl, SecretStr

import primer.llm.anthropic as anthropic_mod
import primer.llm.gemini as gemini_mod
import primer.llm.ollama as ollama_mod
import primer.llm.openchat as openchat_mod
import primer.llm.openresponses as openresponses_mod
import primer.llm.openrouter as openrouter_mod
from primer.llm.retrying import RetryingLLM
from primer.model.chat import (
    Done,
    Error as ChatError,
    Message,
    StreamStart,
    TextDelta,
    TextPart,
    Tool,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from primer.model.except_ import AuthenticationError, BadRequestError, RateLimitError, ServerError
from primer.model.provider import (
    AnthropicConfig,
    GoogleConfig,
    Limits,
    LLMProvider,
    LLMProviderType,
    OllamaConfig,
    OpenChatConfig,
    OpenChatFlavor,
    OpenResponsesConfig,
    OpenRouterConfig,
)

MSGS = [
    Message(role="system", parts=[TextPart(text="be brief")]),
    Message(role="user", parts=[TextPart(text="hi")]),
]
TOOLS = [
    Tool(
        id="read_file", description="Read a file", toolset_id="t",
        args_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
]
_SCHEMA = {"properties": {"path": {"type": "string"}}, "type": "object"}
# A 429/5xx the SDK may replay on its own: the header keeps its wait at 1 ms.
_FAST = {"retry-after-ms": "1", "retry-after": "0"}


def _sse(events: list[tuple[str | None, dict]]) -> bytes:
    out = b""
    for name, data in events:
        if name is not None:
            out += f"event: {name}\n".encode()
        out += f"data: {json.dumps(data)}\n\n".encode()
    return out


def _chat_chunk(delta: dict, finish: str | None = None, **extra) -> tuple[None, dict]:
    return None, {
        "id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra,
    }


CHAT_SSE = _sse([
    _chat_chunk({"role": "assistant", "content": "Hel"}),
    _chat_chunk({"content": "lo"}),
    _chat_chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                 "function": {"name": "read_file", "arguments": '{"pa'}}]}),
    _chat_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.txt"}'}}]}),
    _chat_chunk({}, "tool_calls", usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}),
]) + b"data: [DONE]\n\n"

CHAT_EVENTS = [
    StreamStart(request_id="c1", model="m"),
    TextDelta(text="Hel", index=0),
    TextDelta(text="lo", index=0),
    ToolCallStart(id="call_1", name="read_file", index=0),
    ToolCallDelta(id="call_1", arguments_delta='{"pa', index=0),
    ToolCallDelta(id="call_1", arguments_delta='th": "a.txt"}', index=0),
    ToolCallEnd(id="call_1", arguments={"path": "a.txt"}, index=0),
    Usage(input_tokens=7, output_tokens=3, cumulative=False),
    Done(stop_reason="tool_use", raw_reason="tool_calls"),
]

CHAT_BODY = {
    "messages": [{"content": "be brief", "role": "system"}, {"content": "hi", "role": "user"}],
    "stream": True, "stream_options": {"include_usage": True}, "tool_choice": "auto",
    "tools": [{"function": {"description": "Read a file", "name": "read_file", "parameters": _SCHEMA}, "type": "function"}],
}

ANTHROPIC_SSE = _sse([
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
        "type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"pa'}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {
        "type": "input_json_delta", "partial_json": 'th": "a.txt"}'}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                       "usage": {"output_tokens": 3}}),
    ("message_stop", {"type": "message_stop"}),
])

RESPONSES_SSE = _sse([
    ("response.created", {"type": "response.created", "sequence_number": 0, "response": {
        "id": "resp_1", "object": "response", "created_at": 1, "model": "m", "status": "in_progress", "output": [],
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": []}}),
    ("response.output_item.added", {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0,
                                    "item": {"type": "message", "id": "msg_1", "role": "assistant",
                                             "status": "in_progress", "content": []}}),
    ("response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": 2, "item_id": "msg_1",
                                    "output_index": 0, "content_index": 0, "delta": "Hel", "logprobs": []}),
    ("response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": 3, "item_id": "msg_1",
                                    "output_index": 0, "content_index": 0, "delta": "lo", "logprobs": []}),
    ("response.output_item.added", {"type": "response.output_item.added", "sequence_number": 4, "output_index": 1,
                                    "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                                             "name": "read_file", "arguments": "", "status": "in_progress"}}),
    ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "sequence_number": 5,
                                                "item_id": "fc_1", "output_index": 1, "delta": '{"path": "a.txt"}'}),
    ("response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "sequence_number": 6,
                                               "item_id": "fc_1", "output_index": 1, "arguments": '{"path": "a.txt"}'}),
    ("response.completed", {"type": "response.completed", "sequence_number": 7, "response": {
        "id": "resp_1", "object": "response", "created_at": 1, "model": "m", "status": "completed", "output": [],
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10,
                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}}),
])

GEMINI_SSE = b"".join(b"data: " + json.dumps(d).encode() + b"\r\n\r\n" for d in [
    {"candidates": [{"content": {"role": "model", "parts": [{"text": "Hello"}]}}], "responseId": "r1"},
    {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"id": "fc1", "name": "read_file", "args": {"path": "a.txt"}}}]}, "finishReason": "STOP"}],
     "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3, "totalTokenCount": 10}, "responseId": "r1"},
])

OLLAMA_NDJSON = b"".join(json.dumps(d).encode() + b"\n" for d in [
    {"model": "m", "created_at": "2025-01-01T00:00:00Z", "message": {"role": "assistant", "content": "Hel"}, "done": False},
    {"model": "m", "created_at": "2025-01-01T00:00:00Z", "message": {"role": "assistant", "content": "lo"}, "done": False},
    {"model": "m", "created_at": "2025-01-01T00:00:00Z", "message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}]}, "done": False},
    {"model": "m", "created_at": "2025-01-01T00:00:00Z", "message": {"role": "assistant", "content": ""}, "done": True,
     "done_reason": "stop", "prompt_eval_count": 7, "eval_count": 3},
])


def _sdk_http(client: Any):
    """The HTTP library (httpx or httpx2) an SDK client is built on."""
    for cls in type(client._client).__mro__:
        root = cls.__module__.split(".")[0]
        if root in ("httpx", "httpx2"):
            return importlib.import_module(root)
    raise RuntimeError("SDK client is not built on httpx or httpx2")


class _Wire:
    """Replies with one scripted response per request (the last repeats) and records every request."""

    def __init__(self, http, replies: list[tuple[int, bytes, dict]]) -> None:
        self.http = http
        self.replies = replies
        self.requests: list = []

    def handle(self, request):
        self.requests.append(request)
        status, body, headers = self.replies[min(len(self.requests), len(self.replies)) - 1]
        return self.http.Response(status, content=body, headers=headers)

    def body(self) -> dict:
        return json.loads(self.requests[0].content)


def _route(monkeypatch, kind: str, replies: list[tuple[int, bytes, dict]]) -> _Wire:
    """Point the adapter module's SDK constructor at an in-process transport."""
    if kind == "gemini":
        wire = _Wire(httpx, replies)
        real_genai = genai.Client
        monkeypatch.setattr(gemini_mod.genai, "Client", lambda **kw: real_genai(**kw, http_options=gtypes.HttpOptions(
            httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)))))
        return wire
    if kind == "ollama":
        wire = _Wire(httpx, replies)
        monkeypatch.setattr(ollama_mod.ollama, "AsyncClient",
                            functools.partial(ollama.AsyncClient, transport=httpx.MockTransport(wire.handle)))
        return wire
    module, real = {
        "openchat": (openchat_mod, AsyncOpenAI), "openrouter": (openrouter_mod, AsyncOpenAI),
        "openresponses": (openresponses_mod, AsyncOpenAI), "anthropic": (anthropic_mod, AsyncAnthropic),
    }[kind]
    http = _sdk_http(real(api_key="probe"))
    wire = _Wire(http, replies)
    symbol = "AsyncAnthropic" if kind == "anthropic" else "AsyncOpenAI"
    monkeypatch.setattr(module, symbol, lambda **kw: real(
        **kw, http_client=http.AsyncClient(transport=http.MockTransport(wire.handle))))
    return wire


def _provider(kind: str, **limits) -> LLMProvider:
    config = {
        "openchat": (LLMProviderType.OPENCHAT, OpenChatConfig(
            url=HttpUrl("http://lm.test/v1/"), api_key=SecretStr("sk-test"), flavor=OpenChatFlavor.LMSTUDIO)),
        "openrouter": (LLMProviderType.OPENROUTER, OpenRouterConfig(api_key=SecretStr("sk-or-test"))),
        "openresponses": (LLMProviderType.OPENRESPONSES, OpenResponsesConfig(
            url=HttpUrl("http://resp.test/v1/"), api_key=SecretStr("sk-test"))),
        "anthropic": (LLMProviderType.ANTHROPIC, AnthropicConfig(api_key=SecretStr("sk-ant-test"))),
        "gemini": (LLMProviderType.GEMINI, GoogleConfig(api_key=SecretStr("g-test"))),
        "ollama": (LLMProviderType.OLLAMA, OllamaConfig(url=HttpUrl("http://ollama.test:11434"))),
    }[kind]
    return LLMProvider(id=kind, provider=config[0], config=config[1],
                       limits=Limits(**{"max_concurrency": 1, **limits}))


_ADAPTERS: dict[str, Callable[[LLMProvider], Any]] = {
    "openchat": openchat_mod.OpenChatLLM, "openrouter": openrouter_mod.OpenRouterLLM,
    "openresponses": openresponses_mod.OpenResponsesLLM, "anthropic": anthropic_mod.AnthropicLLM,
    "gemini": gemini_mod.GeminiLLM, "ollama": ollama_mod.OllamaLLM,
}
_MODEL = {"openchat": "m", "openrouter": "a/m", "openresponses": "m", "anthropic": "claude-x",
          "gemini": "gemini-x", "ollama": "m"}


async def _collect(llm, kind: str) -> list:
    try:
        return [e async for e in llm.stream(
            model=_MODEL[kind], messages=MSGS, tools=TOOLS, tool_choice="auto", max_output_tokens=100,
        )]
    finally:
        await llm.aclose()


_CONNECT = pytest.mark.parametrize("connect", [None, 5.0], ids=["no-connect-timeout", "connect-timeout"])
_OK = {"content-type": "text/event-stream"}


class TestOpenChat:
    @_CONNECT
    async def test_request_and_stream(self, monkeypatch, connect) -> None:
        wire = _route(monkeypatch, "openchat", [(200, CHAT_SSE, _OK)])
        events = await _collect(openchat_mod.OpenChatLLM(_provider("openchat", connect_timeout_seconds=connect)), "openchat")
        assert str(wire.requests[0].url) == "http://lm.test/v1/chat/completions"
        assert wire.body() == {**CHAT_BODY, "model": "m", "max_tokens": 100}
        assert events == CHAT_EVENTS
        assert len(wire.requests) == 1

    @pytest.mark.parametrize(("status", "cls", "code"), [
        (400, BadRequestError, None), (401, AuthenticationError, "auth_error"), (422, BadRequestError, None),
        (429, RateLimitError, "rate_limit"), (500, ServerError, "server_error"), (503, ServerError, "server_error"),
    ])
    async def test_error_classification(self, monkeypatch, status, cls, code) -> None:
        _route(monkeypatch, "openchat", [(status, b'{"error": {"message": "nope"}}', _FAST)])
        with pytest.raises(cls) as caught:
            await _collect(openchat_mod.OpenChatLLM(_provider("openchat")), "openchat")
        assert type(caught.value) is cls
        assert (caught.value.code, caught.value.status_code) == (code, status)

    async def test_retrying_llm_recovers_a_5xx_in_two_attempts(self, monkeypatch) -> None:
        wire = _route(monkeypatch, "openchat", [(500, b'{"error": {"message": "boom"}}', _FAST), (200, CHAT_SSE, _OK)])
        prov = _provider("openchat", retry_backoff_seconds=0.0, retry_backoff_max_seconds=0.0)
        events = await _collect(RetryingLLM(openchat_mod.OpenChatLLM(prov), prov), "openchat")
        assert events == CHAT_EVENTS
        assert len(wire.requests) == 2

    async def test_retrying_llm_never_replays_a_rejection(self, monkeypatch) -> None:
        wire = _route(monkeypatch, "openchat", [(400, b'{"error": {"message": "bad"}}', {})])
        prov = _provider("openchat", retry_backoff_seconds=0.0, retry_backoff_max_seconds=0.0)
        with pytest.raises(BadRequestError):
            await _collect(RetryingLLM(openchat_mod.OpenChatLLM(prov), prov), "openchat")
        assert len(wire.requests) == 1


class TestOpenRouter:
    @_CONNECT
    async def test_request_and_stream(self, monkeypatch, connect) -> None:
        wire = _route(monkeypatch, "openrouter", [(200, CHAT_SSE, _OK)])
        events = await _collect(openrouter_mod.OpenRouterLLM(_provider("openrouter", connect_timeout_seconds=connect)), "openrouter")
        assert str(wire.requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
        assert wire.body() == {**CHAT_BODY, "model": "a/m", "max_tokens": 100}
        assert events == CHAT_EVENTS

    @pytest.mark.parametrize(("status", "cls", "code"), [
        (400, BadRequestError, None), (401, AuthenticationError, "auth_error"),
        (429, RateLimitError, "rate_limit"), (502, ServerError, "server_error"),
    ])
    async def test_error_classification(self, monkeypatch, status, cls, code) -> None:
        _route(monkeypatch, "openrouter", [(status, b'{"error": {"message": "nope"}}', _FAST)])
        with pytest.raises(cls) as caught:
            await _collect(openrouter_mod.OpenRouterLLM(_provider("openrouter")), "openrouter")
        assert type(caught.value) is cls
        assert (caught.value.code, caught.value.status_code) == (code, status)

    async def test_retrying_llm_recovers_a_5xx_in_two_attempts(self, monkeypatch) -> None:
        wire = _route(monkeypatch, "openrouter", [(502, b'{"error": {"message": "boom"}}', _FAST), (200, CHAT_SSE, _OK)])
        prov = _provider("openrouter", retry_backoff_seconds=0.0, retry_backoff_max_seconds=0.0)
        assert await _collect(RetryingLLM(openrouter_mod.OpenRouterLLM(prov), prov), "openrouter") == CHAT_EVENTS
        assert len(wire.requests) == 2


class TestAnthropic:
    EVENTS = [
        StreamStart(request_id="msg_1", model="claude-x"),
        TextDelta(text="Hel", index=0),
        TextDelta(text="lo", index=0),
        ToolCallStart(id="toolu_1", name="read_file", index=1),
        ToolCallDelta(id="toolu_1", arguments_delta='{"pa', index=1),
        ToolCallDelta(id="toolu_1", arguments_delta='th": "a.txt"}', index=1),
        ToolCallEnd(id="toolu_1", arguments={"path": "a.txt"}, index=1),
        Usage(input_tokens=7, output_tokens=3, cumulative=False),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]

    @_CONNECT
    async def test_request_and_stream(self, monkeypatch, connect) -> None:
        wire = _route(monkeypatch, "anthropic", [(200, ANTHROPIC_SSE, _OK)])
        events = await _collect(anthropic_mod.AnthropicLLM(_provider("anthropic", connect_timeout_seconds=connect)), "anthropic")
        assert str(wire.requests[0].url) == "https://api.anthropic.com/v1/messages"
        assert wire.body() == {
            "max_tokens": 100, "model": "claude-x", "stream": True, "system": "be brief",
            "messages": [{"content": [{"text": "hi", "type": "text"}], "role": "user"}],
            "tool_choice": {"type": "auto"},
            "tools": [{"description": "Read a file", "input_schema": _SCHEMA, "name": "read_file"}],
        }
        assert events == self.EVENTS

    @pytest.mark.parametrize(("status", "cls", "code"), [
        (400, BadRequestError, None), (401, AuthenticationError, "auth_error"), (403, AuthenticationError, "auth_error"),
        (404, BadRequestError, None), (429, RateLimitError, "rate_limit"), (500, ServerError, "server_error"),
        (529, ServerError, "server_error"),
    ])
    async def test_error_classification(self, monkeypatch, status, cls, code) -> None:
        _route(monkeypatch, "anthropic", [(status, b'{"type": "error", "error": {"type": "x", "message": "nope"}}', _FAST)])
        with pytest.raises(cls) as caught:
            await _collect(anthropic_mod.AnthropicLLM(_provider("anthropic")), "anthropic")
        assert type(caught.value) is cls
        assert (caught.value.code, caught.value.status_code) == (code, status)

    async def test_retrying_llm_recovers_a_5xx_in_two_attempts(self, monkeypatch) -> None:
        wire = _route(monkeypatch, "anthropic", [
            (500, b'{"type": "error", "error": {"type": "api_error", "message": "boom"}}', _FAST),
            (200, ANTHROPIC_SSE, _OK),
        ])
        prov = _provider("anthropic", retry_backoff_seconds=0.0, retry_backoff_max_seconds=0.0)
        assert await _collect(RetryingLLM(anthropic_mod.AnthropicLLM(prov), prov), "anthropic") == self.EVENTS
        assert len(wire.requests) == 2


class TestOpenResponses:
    EVENTS = [
        StreamStart(request_id="resp_1", model="m"),
        TextDelta(text="Hel", index=1),
        TextDelta(text="lo", index=1),
        ToolCallStart(id="call_1", name="read_file", index=2),
        ToolCallDelta(id="call_1", arguments_delta='{"path": "a.txt"}', index=2),
        ToolCallEnd(id="call_1", arguments={"path": "a.txt"}, index=2),
        Usage(input_tokens=7, output_tokens=3, cached_input_tokens=0, reasoning_tokens=0, cumulative=False),
        Done(stop_reason="tool_use", raw_reason="completed"),
    ]

    @_CONNECT
    async def test_request_and_stream(self, monkeypatch, connect) -> None:
        wire = _route(monkeypatch, "openresponses", [(200, RESPONSES_SSE, _OK)])
        events = await _collect(
            openresponses_mod.OpenResponsesLLM(_provider("openresponses", connect_timeout_seconds=connect)), "openresponses",
        )
        assert str(wire.requests[0].url) == "http://resp.test/v1/responses"
        assert wire.body() == {
            "input": [{"content": [{"text": "be brief", "type": "input_text"}], "role": "system"},
                      {"content": [{"text": "hi", "type": "input_text"}], "role": "user"}],
            "model": "m", "store": False, "stream": True, "tool_choice": "auto", "max_output_tokens": 100,
            "tools": [{"description": "Read a file", "name": "read_file", "parameters": _SCHEMA, "type": "function"}],
        }
        assert events == self.EVENTS

    @pytest.mark.parametrize(("status", "cls", "code"), [
        (400, BadRequestError, None), (401, AuthenticationError, "auth_error"), (422, BadRequestError, None),
        (429, RateLimitError, "rate_limit"), (500, ServerError, "server_error"),
    ])
    async def test_error_classification(self, monkeypatch, status, cls, code) -> None:
        _route(monkeypatch, "openresponses", [(status, b'{"error": {"message": "nope"}}', _FAST)])
        with pytest.raises(cls) as caught:
            await _collect(openresponses_mod.OpenResponsesLLM(_provider("openresponses")), "openresponses")
        assert type(caught.value) is cls
        assert (caught.value.code, caught.value.status_code) == (code, status)

    async def test_retrying_llm_recovers_a_5xx_in_two_attempts(self, monkeypatch) -> None:
        wire = _route(monkeypatch, "openresponses", [(500, b'{"error": {"message": "boom"}}', _FAST), (200, RESPONSES_SSE, _OK)])
        prov = _provider("openresponses", retry_backoff_seconds=0.0, retry_backoff_max_seconds=0.0)
        assert await _collect(RetryingLLM(openresponses_mod.OpenResponsesLLM(prov), prov), "openresponses") == self.EVENTS
        assert len(wire.requests) == 2


class TestGemini:
    EVENTS = [
        StreamStart(request_id="r1", model="gemini-x"),
        TextDelta(text="Hello", index=0),
        ToolCallStart(id="fc1", name="read_file", index=1),
        ToolCallDelta(id="fc1", arguments_delta='{"path": "a.txt"}', index=1),
        ToolCallEnd(id="fc1", arguments={"path": "a.txt"}, index=1),
        Usage(input_tokens=7, output_tokens=3, cumulative=True),
        Done(stop_reason="tool_use", raw_reason="STOP"),
    ]

    @_CONNECT
    async def test_request_and_stream(self, monkeypatch, connect) -> None:
        wire = _route(monkeypatch, "gemini", [(200, GEMINI_SSE, _OK)])
        events = await _collect(gemini_mod.GeminiLLM(_provider("gemini", connect_timeout_seconds=connect)), "gemini")
        assert str(wire.requests[0].url).endswith("/models/gemini-x:streamGenerateContent?alt=sse")
        assert wire.body() == {
            "contents": [{"parts": [{"text": "hi"}], "role": "user"}],
            "generationConfig": {"maxOutputTokens": 100},
            "systemInstruction": {"parts": [{"text": "be brief"}], "role": "user"},
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
            "tools": [{"functionDeclarations": [
                {"description": "Read a file", "name": "read_file", "parameters_json_schema": _SCHEMA}]}],
        }
        assert events == self.EVENTS

    @_CONNECT
    @pytest.mark.parametrize(("status", "code"), [
        (400, "bad_request"), (401, "auth_error"), (403, "auth_error"), (404, "bad_request"),
        (429, "rate_limit"), (500, "server_error"),
    ])
    async def test_error_classification(self, monkeypatch, connect, status, code) -> None:
        """The google SDK sends the request on the first iteration, so a refusal is YIELDED as the only event."""
        wire = _route(monkeypatch, "gemini", [(status, b'{"error": {"code": %d, "message": "nope"}}' % status, {})])
        events = await _collect(gemini_mod.GeminiLLM(_provider("gemini", connect_timeout_seconds=connect)), "gemini")
        assert [(type(e), e.code, e.fatal) for e in events] == [(ChatError, code, True)]
        assert len(wire.requests) == 1


class TestOllama:
    EVENTS = [
        StreamStart(request_id=None, model="m"),
        TextDelta(text="Hel", index=0),
        TextDelta(text="lo", index=0),
        ToolCallStart(id="call_1", name="read_file", index=1),
        ToolCallDelta(id="call_1", arguments_delta='{"path": "a.txt"}', index=1),
        ToolCallEnd(id="call_1", arguments={"path": "a.txt"}, index=1),
        Usage(input_tokens=7, output_tokens=3, cumulative=False),
        Done(stop_reason="tool_use", raw_reason="stop"),
    ]

    @_CONNECT
    async def test_request_and_stream(self, monkeypatch, connect) -> None:
        wire = _route(monkeypatch, "ollama", [(200, OLLAMA_NDJSON, {"content-type": "application/x-ndjson"})])
        events = await _collect(ollama_mod.OllamaLLM(_provider("ollama", connect_timeout_seconds=connect)), "ollama")
        assert str(wire.requests[0].url) == "http://ollama.test:11434/api/chat"
        assert wire.body() == {
            "messages": [{"content": "be brief", "role": "system"}, {"content": "hi", "role": "user"}],
            "model": "m", "stream": True, "options": {"num_predict": 100},
            "tools": [{"function": {"description": "Read a file", "name": "read_file", "parameters": _SCHEMA}, "type": "function"}],
        }
        assert events == self.EVENTS

    @_CONNECT
    @pytest.mark.parametrize(("status", "code"), [
        (400, "bad_request"), (401, "auth_error"), (404, "bad_request"), (429, "rate_limit"), (500, "server_error"),
    ])
    async def test_error_classification(self, monkeypatch, connect, status, code) -> None:
        """The ollama SDK sends the request on the first iteration, so a refusal is YIELDED as the only event."""
        wire = _route(monkeypatch, "ollama", [(status, b'{"error": "nope"}', {})])
        events = await _collect(ollama_mod.OllamaLLM(_provider("ollama", connect_timeout_seconds=connect)), "ollama")
        assert [(type(e), e.code, e.fatal) for e in events] == [(ChatError, code, True)]
        assert len(wire.requests) == 1
