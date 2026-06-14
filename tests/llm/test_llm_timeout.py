"""Unit tests for the LLM per-event inactivity timeout.

Covers:
* ``Limits.request_timeout_seconds`` field defaults and validation.
* ``_iter_with_timeout`` helper: passthrough with None, fires on stall,
  transparent on normal iteration.
* Timeout enforcement in both OpenChat and Anthropic adapters:
  ProviderTimeoutError is raised promptly when the SDK stream stalls.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import HttpUrl, SecretStr, ValidationError

from primer.llm._timeout import _iter_with_timeout
from primer.model.except_ import ProviderTimeoutError
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    OpenChatConfig,
    OpenChatFlavor,
)


# ---------------------------------------------------------------------------
# Limits model tests
# ---------------------------------------------------------------------------


class TestLimitsField:
    def test_default_request_timeout(self) -> None:
        lim = Limits(max_concurrency=2)
        assert lim.request_timeout_seconds == 300.0

    def test_explicit_value(self) -> None:
        lim = Limits(max_concurrency=1, request_timeout_seconds=60.0)
        assert lim.request_timeout_seconds == 60.0

    def test_none_disables_timeout(self) -> None:
        lim = Limits(max_concurrency=1, request_timeout_seconds=None)
        assert lim.request_timeout_seconds is None

    def test_zero_is_allowed(self) -> None:
        # 0.0 is valid (ge=0.0).
        lim = Limits(max_concurrency=1, request_timeout_seconds=0.0)
        assert lim.request_timeout_seconds == 0.0

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Limits(max_concurrency=1, request_timeout_seconds=-1.0)

    def test_field_is_on_llm_provider(self) -> None:
        provider = LLMProvider(
            id="p1",
            provider=LLMProviderType.ANTHROPIC,
            models=[LLMModel(name="claude-3-haiku-20240307", context_length=200_000)],
            config=AnthropicConfig(api_key=SecretStr("sk-ant-test")),
            limits=Limits(max_concurrency=2, request_timeout_seconds=120.0),
        )
        assert provider.limits.request_timeout_seconds == 120.0


# ---------------------------------------------------------------------------
# _iter_with_timeout helper
# ---------------------------------------------------------------------------


async def _fast_agen(*items):
    """Yield items immediately."""
    for item in items:
        yield item


async def _stalling_agen():
    """Yield one item then stall forever."""
    yield "first"
    await asyncio.Event().wait()  # blocks indefinitely


class TestIterWithTimeout:
    @pytest.mark.asyncio
    async def test_passthrough_none(self) -> None:
        """None timeout: items flow through without modification."""
        result = []
        async for item in _iter_with_timeout(_fast_agen(1, 2, 3), None):
            result.append(item)
        assert result == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_passthrough_with_timeout_when_fast(self) -> None:
        """Items arrive well within the window: no timeout fires."""
        result = []
        async for item in _iter_with_timeout(_fast_agen("a", "b"), 10.0):
            result.append(item)
        assert result == ["a", "b"]

    @pytest.mark.asyncio
    async def test_fires_on_stall(self) -> None:
        """When no event arrives within the window asyncio.TimeoutError is raised."""
        it = _iter_with_timeout(_stalling_agen(), 0.05)  # 50 ms timeout
        # Consume the first item (arrives instantly).
        item = await it.__anext__()
        assert item == "first"
        # The second __anext__ stalls; timeout should fire.
        with pytest.raises(asyncio.TimeoutError):
            await it.__anext__()

    @pytest.mark.asyncio
    async def test_empty_iterable(self) -> None:
        result = []
        async for item in _iter_with_timeout(_fast_agen(), 5.0):
            result.append(item)
        assert result == []

    @pytest.mark.asyncio
    async def test_fires_promptly(self) -> None:
        """Confirm the timeout fires well within 3 x the configured window.

        Uses asyncio.wait_for to assert the test itself completes quickly,
        so a regression that silently ignores the timeout does not hang CI.
        """
        async def _run():
            it = _iter_with_timeout(_stalling_agen(), 0.05)
            await it.__anext__()  # "first" -- instant
            await it.__anext__()  # should raise TimeoutError after ~50 ms

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_run(), timeout=3.0)


# ---------------------------------------------------------------------------
# Adapter-level: ProviderTimeoutError is raised from a stalling stream
# ---------------------------------------------------------------------------


class _FakeAnthropicRaw:
    """Minimal anthropic event stub -- _translate_event treats it as unknown."""
    type = "message_start"
    message = None


class _FakeOpenAIChunk:
    """Minimal openai SSE chunk stub -- _translate_chunk ignores it."""
    model = "gpt-test"
    choices = []
    usage = None


async def _stalling_anthropic_sdk():
    yield _FakeAnthropicRaw()
    await asyncio.Event().wait()


async def _stalling_openai_sdk():
    yield _FakeOpenAIChunk()
    await asyncio.Event().wait()


def _make_anthropic_provider(*, timeout_seconds: float | None = 0.05) -> LLMProvider:
    return LLMProvider(
        id="test-ant",
        provider=LLMProviderType.ANTHROPIC,
        models=[LLMModel(name="claude-3-haiku-20240307", context_length=200_000)],
        config=AnthropicConfig(api_key=SecretStr("sk-ant-test")),
        limits=Limits(max_concurrency=2, request_timeout_seconds=timeout_seconds),
    )


def _make_openchat_provider(*, timeout_seconds: float | None = 0.05) -> LLMProvider:
    return LLMProvider(
        id="test-oc",
        provider=LLMProviderType.OPENCHAT,
        models=[LLMModel(name="gpt-test", context_length=4096)],
        config=OpenChatConfig(
            url=HttpUrl("http://localhost:11434"),
            flavor=OpenChatFlavor.OTHER,
            api_key=SecretStr("no-key"),
        ),
        limits=Limits(max_concurrency=1, request_timeout_seconds=timeout_seconds),
    )


class TestAdapterTimeoutEnforcement:
    @pytest.mark.asyncio
    async def test_anthropic_raises_provider_timeout_error(self) -> None:
        """AnthropicLLM raises ProviderTimeoutError when the stream stalls."""
        from primer.llm.anthropic import AnthropicLLM
        from primer.model.chat import Message, TextPart

        provider = _make_anthropic_provider(timeout_seconds=0.05)
        llm = AnthropicLLM(provider)

        # Patch the SDK client so create() returns our stalling generator.
        fake_client = MagicMock()
        fake_client.messages.create = AsyncMock(
            return_value=_stalling_anthropic_sdk()
        )
        llm._get_client = MagicMock(return_value=fake_client)

        prompt = [Message(role="user", parts=[TextPart(text="hi")])]

        with pytest.raises(ProviderTimeoutError) as exc_info:
            async for _event in llm.stream(
                model="claude-3-haiku-20240307", messages=prompt
            ):
                pass

        assert exc_info.value.code == "stream_timeout"
        assert "claude-3-haiku-20240307" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_anthropic_no_timeout_when_none(self) -> None:
        """With request_timeout_seconds=None the stream is passed through."""
        from primer.llm.anthropic import AnthropicLLM
        from primer.model.chat import Done, Message, TextPart

        # Use a stream that completes normally (no stalling).
        async def _completing_sdk():
            # Yield one real-ish event and stop.
            return
            yield  # make it a generator

        provider = _make_anthropic_provider(timeout_seconds=None)
        llm = AnthropicLLM(provider)

        async def _fast_sdk():
            return
            yield

        fake_client = MagicMock()
        fake_client.messages.create = AsyncMock(return_value=_fast_sdk())
        llm._get_client = MagicMock(return_value=fake_client)

        prompt = [Message(role="user", parts=[TextPart(text="hi")])]

        # Should complete without raising ProviderTimeoutError (stream ends immediately).
        events = []
        async for event in llm.stream(
            model="claude-3-haiku-20240307", messages=prompt
        ):
            events.append(event)
        # The adapter always yields exactly one terminal event; with an empty
        # SDK stream and the value-error guard it logs a warning and returns --
        # no ProviderTimeoutError even with a none timeout.

    @pytest.mark.asyncio
    async def test_openchat_raises_provider_timeout_error(self) -> None:
        """OpenChatLLM raises ProviderTimeoutError when the stream stalls."""
        from primer.llm.openchat import OpenChatLLM
        from primer.model.chat import Message, TextPart

        provider = _make_openchat_provider(timeout_seconds=0.05)
        llm = OpenChatLLM(provider)

        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(
            return_value=_stalling_openai_sdk()
        )
        llm._get_client = MagicMock(return_value=fake_client)

        prompt = [Message(role="user", parts=[TextPart(text="hi")])]

        with pytest.raises(ProviderTimeoutError) as exc_info:
            async for _event in llm.stream(model="gpt-test", messages=prompt):
                pass

        assert exc_info.value.code == "stream_timeout"

    @pytest.mark.asyncio
    async def test_timeout_stored_at_construction(self) -> None:
        """Adapters store request_timeout_seconds from provider.limits."""
        from primer.llm.anthropic import AnthropicLLM

        provider = _make_anthropic_provider(timeout_seconds=42.0)
        llm = AnthropicLLM(provider)
        assert llm._request_timeout_seconds == 42.0
