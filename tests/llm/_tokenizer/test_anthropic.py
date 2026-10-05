"""Anthropic count-tokens counter: bounded, honest, and it raises.

The counter used to swallow every exception and return the character heuristic
as a successful count, so a failed call was labelled native. These tests pin the
replacement: every failure is a mapped error or TokenCounterUnavailable that the
single wrapper (primer.llm.counting) turns into a labelled estimate.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest

from primer.llm._tokenizer.anthropic import (
    COUNT_TIMEOUT_S,
    count_tokens_anthropic_detailed,
)
from primer.llm.anthropic import _messages_to_anthropic, _tools_to_anthropic
from primer.llm.counting import NegativeCache, count_prompt_tokens
from primer.model.chat import (
    DocumentPart,
    ImagePart,
    Message,
    TextPart,
    Tool,
    ToolCallPart,
    ToolResultPart,
)
from primer.model.except_ import (
    BadRequestError,
    NetworkError,
    ProviderError,
    ProviderTimeoutError,
    TokenCounterUnavailable,
)
from primer.model.media_tokens import DOCUMENT_TOKENS, IMAGE_TOKENS

USER = [Message(role="user", parts=[TextPart(text="hi")])]


class _FakeClient:
    """Records ``with_options`` and the request; ``count_tokens`` is scripted."""

    def __init__(self, *, tokens: int = 0, exc: Exception | None = None) -> None:
        self.options: list[dict] = []
        self.messages = SimpleNamespace(
            count_tokens=AsyncMock(
                return_value=SimpleNamespace(input_tokens=tokens), side_effect=exc,
            )
        )

    def with_options(self, **options):
        self.options.append(options)
        return self

    @property
    def request(self) -> dict:
        return self.messages.count_tokens.await_args.kwargs


async def _count(client, messages=USER, tools=None, **kwargs):
    return await count_tokens_anthropic_detailed(
        client=client, model="claude-opus-4-7", messages=messages, tools=tools,
        messages_to_wire=_messages_to_anthropic, tools_to_wire=_tools_to_anthropic,
        **kwargs,
    )


def _sleeping_client(seconds: float):
    """A client whose count outlasts ``timeout_s``: the SDK's own ``timeout`` is a
    per-phase httpx timeout, so a slow response is not cut off by it."""
    calls = {"n": 0}

    async def count_tokens(**_kwargs):
        calls["n"] += 1
        await asyncio.sleep(seconds)
        return SimpleNamespace(input_tokens=1)

    client = SimpleNamespace(
        messages=SimpleNamespace(count_tokens=count_tokens),
        with_options=lambda **_options: client,
    )
    return client, calls


class TestRequestShape:
    async def test_returns_the_endpoints_count_and_says_it_is_exact(self) -> None:
        client = _FakeClient(tokens=123)
        got = await _count(client)
        assert (got.total, got.exact, got.estimated_components) == (123, True, ())

    async def test_every_call_is_bounded(self) -> None:
        client = _FakeClient(tokens=1)
        await _count(client)
        assert client.options == [{"max_retries": 0}], "the SDK default is two retries"
        assert client.request["timeout"] == COUNT_TIMEOUT_S == 3.0

    async def test_system_messages_become_the_system_parameter(self) -> None:
        client = _FakeClient(tokens=1)
        await _count(client, [
            Message(role="system", parts=[TextPart(text="be brief")]),
            Message(role="system", parts=[TextPart(text="be kind")]),
            *USER,
        ])
        assert client.request["system"] == "be brief\n\nbe kind"
        assert [m["role"] for m in client.request["messages"]] == ["user"]

    async def test_no_system_parameter_when_there_is_no_system_text(self) -> None:
        client = _FakeClient(tokens=1)
        await _count(client)
        assert "system" not in client.request
        assert "tools" not in client.request

    async def test_a_tool_using_history_is_sent_with_the_roles_the_count_api_accepts(self) -> None:
        """The count API takes only user and assistant. The live stream() translator
        turns a ``tool`` message into a user-role tool_result block; a private copy
        of that walk once sent role ``tool`` verbatim and every count of a
        tool-using history was a 400."""
        client = _FakeClient(tokens=1)
        await _count(client, [
            *USER,
            Message(role="assistant", parts=[ToolCallPart(id="t1", name="ls", arguments={})]),
            Message(role="tool", parts=[ToolResultPart(id="t1", output="a b c")]),
        ])
        wire = client.request["messages"]
        assert [m["role"] for m in wire] == ["user", "assistant", "user"]
        assert wire[2]["content"][0]["type"] == "tool_result"
        assert wire[2]["content"][0]["tool_use_id"] == "t1"

    async def test_the_request_is_built_by_the_live_translators(self) -> None:
        """No drift: the counted messages are exactly what stream() would send."""
        history = [
            Message(role="system", parts=[TextPart(text="be brief")]),
            *USER,
            Message(role="assistant", parts=[ToolCallPart(id="t1", name="ls", arguments={"p": 1})]),
            Message(role="tool", parts=[ToolResultPart(id="t1", output="x", error=True)]),
        ]
        client = _FakeClient(tokens=1)
        await _count(client, history)
        system, expected = _messages_to_anthropic(history)
        assert client.request["messages"] == expected and client.request["system"] == system

    async def test_tools_are_sent(self) -> None:
        client = _FakeClient(tokens=1)
        tools = [Tool(id="ls", description="list", toolset_id="x",
                      args_schema={"type": "object", "properties": {}})]
        await _count(client, tools=tools)
        assert client.request["tools"] == _tools_to_anthropic(tools)
        assert client.request["tools"][0]["name"] == "ls"

    async def test_media_is_never_sent_and_is_an_estimated_component(self) -> None:
        """An empty base64 placeholder is unverified against the API and likely
        rejected; media is estimated with the shared constants instead."""
        client = _FakeClient(tokens=100)
        got = await _count(client, [Message(role="user", parts=[
            TextPart(text="look"),
            ImagePart(mime_type="image/png", data=b"\x00"),
            DocumentPart(mime_type="application/pdf", data=b"%PDF"),
        ])])
        blocks = client.request["messages"][0]["content"]
        assert [b["type"] for b in blocks] == ["text"]
        assert '"data": ""' not in repr(client.request)
        assert got.total == 100 + IMAGE_TOKENS + DOCUMENT_TOKENS
        assert got.estimated_components == ("media",)

    async def test_nothing_natively_countable_is_unavailable_without_a_call(self) -> None:
        client = _FakeClient(tokens=1)
        with pytest.raises(TokenCounterUnavailable, match="nothing natively countable"):
            await _count(client, [Message(role="user", parts=[
                ImagePart(mime_type="image/png", data=b"\x00"),
            ])])
        client.messages.count_tokens.assert_not_awaited()


def _status_error(cls, status: int):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages/count_tokens")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


class TestFailuresRaiseMappedErrors:
    async def test_a_timeout_is_a_provider_timeout(self) -> None:
        exc = anthropic.APITimeoutError(request=httpx.Request("POST", "https://x"))
        with pytest.raises(ProviderTimeoutError):
            await _count(_FakeClient(exc=exc))

    async def test_a_count_that_outlives_the_deadline_is_a_timeout_not_a_long_wait(self) -> None:
        client, calls = _sleeping_client(30.0)
        started = time.monotonic()
        with pytest.raises(ProviderTimeoutError):
            await _count(client, timeout_s=0.1)
        assert time.monotonic() - started < 10.0, "the call waited out the sleep"
        assert calls["n"] == 1

    async def test_a_connection_failure_is_a_network_error(self) -> None:
        exc = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
        with pytest.raises(NetworkError):
            await _count(_FakeClient(exc=exc))

    async def test_an_unclassified_4xx_is_a_bad_request_not_a_bare_provider_error(self) -> None:
        """A 413 has no SDK class, so the classifier returns a bare ProviderError, which
        the wrapper would treat as a bug. A 4xx is the request being refused."""
        request = httpx.Request("POST", "https://x")
        exc = anthropic.APIStatusError("too large", response=httpx.Response(413, request=request), body=None)
        with pytest.raises(BadRequestError) as caught:
            await _count(_FakeClient(exc=exc))
        assert caught.value.status_code == 413

    async def test_a_400_is_a_bad_request(self) -> None:
        with pytest.raises(BadRequestError):
            await _count(_FakeClient(exc=_status_error(anthropic.BadRequestError, 400)))

    async def test_an_unexpected_exception_is_not_swallowed_into_a_number(self) -> None:
        with pytest.raises(ProviderError):
            await _count(_FakeClient(exc=RuntimeError("boom")))

    async def test_the_old_char_fallback_value_is_not_returned(self) -> None:
        # 'hello' used to come back as the heuristic's 10 from a failed call.
        client = _FakeClient(exc=RuntimeError("boom"))
        with pytest.raises(ProviderError):
            await _count(client, [Message(role="user", parts=[TextPart(text="hello")])])


class TestThroughTheWrapper:
    """N4: a failing client must reach the wrapper's labelled fallback, never native."""

    class _Llm:
        def __init__(self, client, timeout_s: float = COUNT_TIMEOUT_S) -> None:
            self.client = client
            self.timeout_s = timeout_s

        async def count_tokens_detailed(self, *, model, messages, tools=None):
            return await count_tokens_anthropic_detailed(
                client=self.client, model=model, messages=messages, tools=tools,
                messages_to_wire=_messages_to_anthropic, tools_to_wire=_tools_to_anthropic,
                timeout_s=self.timeout_s,
            )

    MODEL = SimpleNamespace(provider_id="p", profile_id="prof", model_name="claude-opus-4-7")

    @pytest.mark.parametrize(
        ("exc", "outcome"),
        [
            (anthropic.APITimeoutError(request=httpx.Request("POST", "https://x")), "fallback_timeout"),
            (anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")), "fallback_transient"),
            (_status_error(anthropic.RateLimitError, 429), "fallback_transient"),
            (_status_error(anthropic.InternalServerError, 500), "fallback_transient"),
            (_status_error(anthropic.BadRequestError, 400), "fallback_rejected"),
            (_status_error(anthropic.APIStatusError, 413), "fallback_rejected"),
            # No SDK class for 408 or 425; they are "try again", not a refusal, so
            # they must be cached rather than promoted to a rejection.
            (_status_error(anthropic.APIStatusError, 408), "fallback_timeout"),
            (_status_error(anthropic.APIStatusError, 425), "fallback_transient"),
        ],
    )
    async def test_a_failing_client_is_an_estimate_never_native(self, exc, outcome) -> None:
        result = await count_prompt_tokens(
            self._Llm(_FakeClient(exc=exc)), model=self.MODEL, messages=USER,
            negative_cache=NegativeCache(),
        )
        assert (result.source, result.outcome) == ("estimate", outcome)
        assert result.total > 0

    async def test_a_stalled_count_is_negative_cached(self) -> None:
        exc = anthropic.APITimeoutError(request=httpx.Request("POST", "https://x"))
        client = _FakeClient(exc=exc)
        cache = NegativeCache()
        llm = self._Llm(client)
        first = await count_prompt_tokens(llm, model=self.MODEL, messages=USER, negative_cache=cache)
        second = await count_prompt_tokens(llm, model=self.MODEL, messages=USER, negative_cache=cache)
        assert (first.outcome, second.outcome) == ("fallback_timeout", "negative_cached")
        assert client.messages.count_tokens.await_count == 1

    async def test_a_slow_count_is_a_cached_timeout_through_the_wrapper(self) -> None:
        client, calls = _sleeping_client(30.0)
        cache = NegativeCache()
        llm = self._Llm(client, timeout_s=0.1)
        started = time.monotonic()
        # The wrapper's own backstop (10s by default) would turn a MISSING provider deadline into the same
        # fallback_timeout, a hair under this test's bound; out of the way (60s), a missing deadline lets the
        # 30s sleep finish and the outcome is 'ok', a failure with a wide margin.
        first = await count_prompt_tokens(
            llm, model=self.MODEL, messages=USER, negative_cache=cache, backstop_s=60.0,
        )
        second = await count_prompt_tokens(
            llm, model=self.MODEL, messages=USER, negative_cache=cache, backstop_s=60.0,
        )
        assert (first.outcome, second.outcome) == ("fallback_timeout", "negative_cached")
        assert calls["n"] == 1
        assert time.monotonic() - started < 10.0

    async def test_a_working_client_is_labelled_native(self) -> None:
        result = await count_prompt_tokens(
            self._Llm(_FakeClient(tokens=77)), model=self.MODEL, messages=USER,
            negative_cache=NegativeCache(),
        )
        assert (result.total, result.source, result.outcome) == (77, "native", "ok")


class TestTheRealClientIsBounded:
    """The SDK's ``timeout`` is per phase, so only ``asyncio.wait_for`` bounds the call.
    This runs the REAL ``AsyncAnthropic`` client against a loopback server that
    trickles its response one byte at a time, which resets httpx's read timer on
    every byte: with the old code a ``timeout_s`` of 0.5 returned successfully after
    ~5 s (and 12 s at 1.0 against a slower trickle)."""

    # Trailing JSON whitespace pads the body to ~300 bytes: at BYTE_DELAY_S each, a call with no deadline takes ~30s.
    BODY = b'{"input_tokens": 7, "type": "message_tokens_count"}' + b" " * 250
    BYTE_DELAY_S = 0.1

    def _server(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        stop = threading.Event()

        def serve() -> None:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                    b"content-length: %d\r\n\r\n" % len(self.BODY)
                )
                for byte in self.BODY:
                    if stop.is_set():
                        break
                    conn.sendall(bytes([byte]))
                    time.sleep(self.BYTE_DELAY_S)
            except OSError:
                pass  # the client gave up: the point of the test
            finally:
                conn.close()

        threading.Thread(target=serve, daemon=True).start()
        return listener, stop

    async def test_a_trickling_response_is_cut_off_at_timeout_s(self) -> None:
        listener, stop = self._server()
        client = anthropic.AsyncAnthropic(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", api_key="test",
        )
        started = time.monotonic()
        try:
            with pytest.raises(ProviderTimeoutError):
                await _count(client, timeout_s=0.5)
            assert time.monotonic() - started < 10.0, "the whole call must stop at timeout_s"
        finally:
            stop.set()
            listener.close()
            await client.close()
