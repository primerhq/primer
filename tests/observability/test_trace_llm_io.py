"""Tests for the trace_llm_io opt-in flag across all four LLM adapters.

When trace_llm_io=False (default), no ``llm.request.messages`` span
attribute is recorded. When trace_llm_io=True the attribute is present
and contains the JSON-encoded messages array.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from matrix.observability.metrics import reset_for_test


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_metrics():
    reset_for_test()
    yield


@pytest.fixture
def in_memory_tracer_provider():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _patch_tracer(provider):
    return patch(
        "matrix.observability.tracing.get_tracer",
        side_effect=lambda name: provider.get_tracer(name),
    )


async def _drain(agen):
    items = []
    async for item in agen:
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# Adapter constructors with trace_llm_io flag
# ---------------------------------------------------------------------------


def _make_anthropic_adapter(*, trace_llm_io: bool):
    from matrix.coordinator.in_memory import InMemoryRateLimiter
    from matrix.llm.anthropic import AnthropicLLM
    from matrix.model.provider import (
        AnthropicConfig,
        LLMModel,
        LLMProvider,
        LLMProviderType,
        Limits as LLMProviderLimits,
    )

    config = AnthropicConfig(api_key="sk-test")
    provider = LLMProvider(
        id="test-anthropic",
        name="Test Anthropic",
        provider=LLMProviderType.ANTHROPIC,
        config=config,
        models=[LLMModel(name="claude-3-haiku-20240307", context_length=200000)],
        limits=LLMProviderLimits(max_concurrency=1),
    )
    return AnthropicLLM(provider, rate_limiter=InMemoryRateLimiter(), trace_llm_io=trace_llm_io)


def _make_openresponses_adapter(*, trace_llm_io: bool):
    from matrix.coordinator.in_memory import InMemoryRateLimiter
    from matrix.llm.openresponses import OpenResponsesLLM
    from matrix.model.provider import (
        LLMModel,
        LLMProvider,
        LLMProviderType,
        Limits as LLMProviderLimits,
        OpenResponsesConfig,
        OpenResponsesFlavor,
    )

    config = OpenResponsesConfig(
        url="http://localhost:1234",
        api_key="sk-test",
        flavor=OpenResponsesFlavor.OPENAI,
    )
    provider = LLMProvider(
        id="test-openresponses",
        name="Test OR",
        provider=LLMProviderType.OPENRESPONSES,
        config=config,
        models=[LLMModel(name="gpt-4o-mini", context_length=128000)],
        limits=LLMProviderLimits(max_concurrency=1),
    )
    return OpenResponsesLLM(provider, rate_limiter=InMemoryRateLimiter(), trace_llm_io=trace_llm_io)


def _make_gemini_adapter(*, trace_llm_io: bool):
    from matrix.coordinator.in_memory import InMemoryRateLimiter
    from matrix.llm.gemini import GeminiLLM
    from matrix.model.provider import (
        GoogleConfig,
        LLMModel,
        LLMProvider,
        LLMProviderType,
        Limits as LLMProviderLimits,
    )

    config = GoogleConfig(api_key="AIza-test")
    provider = LLMProvider(
        id="test-gemini",
        name="Test Gemini",
        provider=LLMProviderType.GEMINI,
        config=config,
        models=[LLMModel(name="gemini-2.0-flash", context_length=1000000)],
        limits=LLMProviderLimits(max_concurrency=1),
    )
    return GeminiLLM(provider, rate_limiter=InMemoryRateLimiter(), trace_llm_io=trace_llm_io)


def _make_ollama_adapter(*, trace_llm_io: bool):
    from matrix.coordinator.in_memory import InMemoryRateLimiter
    from matrix.llm.ollama import OllamaLLM
    from matrix.model.provider import (
        LLMModel,
        LLMProvider,
        LLMProviderType,
        Limits as LLMProviderLimits,
        OllamaConfig,
    )

    config = OllamaConfig(url="http://localhost:11434")
    provider = LLMProvider(
        id="test-ollama",
        name="Test Ollama",
        provider=LLMProviderType.OLLAMA,
        config=config,
        models=[LLMModel(name="llama3.2", context_length=128000)],
        limits=LLMProviderLimits(max_concurrency=1),
    )
    return OllamaLLM(provider, rate_limiter=InMemoryRateLimiter(), trace_llm_io=trace_llm_io)


# ---------------------------------------------------------------------------
# Minimal fake streams (reused from test_llm_instrumentation)
# ---------------------------------------------------------------------------


def _fake_anthropic_stream_events():
    msg_start = MagicMock()
    msg_start.type = "message_start"
    msg = MagicMock()
    msg.usage = MagicMock(input_tokens=5)
    msg.id = "req-1"
    msg.model = "claude-3-haiku-20240307"
    msg_start.message = msg

    msg_delta = MagicMock()
    msg_delta.type = "message_delta"
    delta = MagicMock()
    delta.stop_reason = "end_turn"
    msg_delta.delta = delta
    msg_delta.usage = MagicMock(output_tokens=5)

    msg_stop = MagicMock()
    msg_stop.type = "message_stop"

    return [msg_start, msg_delta, msg_stop]


def _fake_openresponses_stream_events():
    created = MagicMock()
    created.type = "response.created"
    response = MagicMock()
    response.id = "resp-1"
    response.model = "gpt-4o-mini"
    created.response = response

    completed = MagicMock()
    completed.type = "response.completed"
    usage = MagicMock()
    usage.input_tokens = 5
    usage.output_tokens = 5
    usage.input_tokens_details = None
    usage.output_tokens_details = None
    complete_response = MagicMock()
    complete_response.usage = usage
    completed.response = complete_response

    return [created, completed]


def _fake_gemini_stream_chunks():
    chunk1 = MagicMock()
    chunk1.candidates = []
    chunk1.usage_metadata = None

    chunk2 = MagicMock()
    chunk2.candidates = []
    usage_meta = MagicMock()
    usage_meta.prompt_token_count = 5
    usage_meta.candidates_token_count = 5
    chunk2.usage_metadata = usage_meta

    return [chunk1, chunk2]


def _fake_ollama_stream_chunks():
    chunk1 = MagicMock()
    chunk1.done = False
    chunk1.model = "llama3.2"
    message1 = MagicMock()
    message1.content = "hi"
    message1.thinking = None
    message1.tool_calls = []
    chunk1.message = message1

    chunk2 = MagicMock()
    chunk2.done = True
    chunk2.done_reason = "stop"
    chunk2.model = "llama3.2"
    chunk2.prompt_eval_count = 5
    chunk2.eval_count = 5
    message2 = MagicMock()
    message2.content = None
    message2.thinking = None
    message2.tool_calls = []
    chunk2.message = message2

    return [chunk1, chunk2]


# ---------------------------------------------------------------------------
# Helper: build a simple messages list
# ---------------------------------------------------------------------------


def _user_messages():
    from matrix.model.chat import Message, TextPart
    return [Message(role="user", parts=[TextPart(text="Hello world")])]


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_trace_llm_io_false_omits_messages(in_memory_tracer_provider):
    """With trace_llm_io=False, llm.request.messages is not set on the span."""
    tp, exporter = in_memory_tracer_provider
    adapter = _make_anthropic_adapter(trace_llm_io=False)

    async def _fake_sdk_stream():
        for e in _fake_anthropic_stream_events():
            yield e

    with _patch_tracer(tp):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            await _drain(adapter.stream(
                model="claude-3-haiku-20240307",
                messages=_user_messages(),
            ))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "llm.request.messages" not in spans[0].attributes


@pytest.mark.asyncio
async def test_anthropic_trace_llm_io_true_includes_messages(in_memory_tracer_provider):
    """With trace_llm_io=True, llm.request.messages is set and contains JSON."""
    tp, exporter = in_memory_tracer_provider
    adapter = _make_anthropic_adapter(trace_llm_io=True)

    async def _fake_sdk_stream():
        for e in _fake_anthropic_stream_events():
            yield e

    with _patch_tracer(tp):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            await _drain(adapter.stream(
                model="claude-3-haiku-20240307",
                messages=_user_messages(),
            ))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert "llm.request.messages" in attrs
    parsed = json.loads(attrs["llm.request.messages"])
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["role"] == "user"
    assert any(p["type"] == "text" for p in parsed[0]["parts"])


# ---------------------------------------------------------------------------
# OpenResponses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openresponses_trace_llm_io_false_omits_messages(in_memory_tracer_provider):
    tp, exporter = in_memory_tracer_provider
    adapter = _make_openresponses_adapter(trace_llm_io=False)

    async def _fake_sdk_stream():
        for e in _fake_openresponses_stream_events():
            yield e

    with _patch_tracer(tp):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.responses.create = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            await _drain(adapter.stream(
                model="gpt-4o-mini",
                messages=_user_messages(),
            ))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "llm.request.messages" not in spans[0].attributes


@pytest.mark.asyncio
async def test_openresponses_trace_llm_io_true_includes_messages(in_memory_tracer_provider):
    tp, exporter = in_memory_tracer_provider
    adapter = _make_openresponses_adapter(trace_llm_io=True)

    async def _fake_sdk_stream():
        for e in _fake_openresponses_stream_events():
            yield e

    with _patch_tracer(tp):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.responses.create = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            await _drain(adapter.stream(
                model="gpt-4o-mini",
                messages=_user_messages(),
            ))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert "llm.request.messages" in attrs
    parsed = json.loads(attrs["llm.request.messages"])
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["role"] == "user"


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_trace_llm_io_false_omits_messages(in_memory_tracer_provider):
    tp, exporter = in_memory_tracer_provider
    adapter = _make_gemini_adapter(trace_llm_io=False)

    async def _fake_sdk_stream():
        for c in _fake_gemini_stream_chunks():
            yield c

    with _patch_tracer(tp):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.aio.models.generate_content_stream = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            await _drain(adapter.stream(
                model="gemini-2.0-flash",
                messages=_user_messages(),
            ))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "llm.request.messages" not in spans[0].attributes


@pytest.mark.asyncio
async def test_gemini_trace_llm_io_true_includes_messages(in_memory_tracer_provider):
    tp, exporter = in_memory_tracer_provider
    adapter = _make_gemini_adapter(trace_llm_io=True)

    async def _fake_sdk_stream():
        for c in _fake_gemini_stream_chunks():
            yield c

    with _patch_tracer(tp):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.aio.models.generate_content_stream = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            await _drain(adapter.stream(
                model="gemini-2.0-flash",
                messages=_user_messages(),
            ))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert "llm.request.messages" in attrs
    parsed = json.loads(attrs["llm.request.messages"])
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["role"] == "user"


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_trace_llm_io_false_omits_messages(in_memory_tracer_provider):
    tp, exporter = in_memory_tracer_provider
    adapter = _make_ollama_adapter(trace_llm_io=False)

    async def _fake_sdk_stream():
        for c in _fake_ollama_stream_chunks():
            yield c

    with _patch_tracer(tp):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.chat = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            await _drain(adapter.stream(
                model="llama3.2",
                messages=_user_messages(),
            ))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "llm.request.messages" not in spans[0].attributes


@pytest.mark.asyncio
async def test_ollama_trace_llm_io_true_includes_messages(in_memory_tracer_provider):
    tp, exporter = in_memory_tracer_provider
    adapter = _make_ollama_adapter(trace_llm_io=True)

    async def _fake_sdk_stream():
        for c in _fake_ollama_stream_chunks():
            yield c

    with _patch_tracer(tp):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.chat = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            await _drain(adapter.stream(
                model="llama3.2",
                messages=_user_messages(),
            ))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert "llm.request.messages" in attrs
    parsed = json.loads(attrs["llm.request.messages"])
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["role"] == "user"
