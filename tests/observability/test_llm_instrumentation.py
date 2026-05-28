"""Tests for LLM adapter OTEL span + Prometheus metrics instrumentation.

Each test drives a fake stream through an adapter's ``stream()`` method
and verifies:
- Span attributes (llm.provider, llm.model, llm.usage.tokens_in/out)
- Counter increments for llm_tokens_total
- Duration histogram observation for llm_duration_seconds
- Failure counter and span exception recording on error
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from primer.observability.metrics import reset_for_test


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_metrics():
    reset_for_test()
    yield


@pytest.fixture
def in_memory_tracer_provider():
    """Return a TracerProvider that records spans in memory."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _patch_tracer(provider):
    """Context manager that replaces the primer.llm.*.get_tracer call."""
    return patch(
        "primer.observability.tracing.get_tracer",
        side_effect=lambda name: provider.get_tracer(name),
    )


# ---------------------------------------------------------------------------
# Helper: consume an async generator
# ---------------------------------------------------------------------------


async def _drain(agen):
    """Drain an async generator, returning all yielded items."""
    items = []
    async for item in agen:
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# Helpers to build minimal providers + adapters
# ---------------------------------------------------------------------------


def _make_anthropic_adapter():
    from primer.model.provider import (
        AnthropicConfig,
        LLMProvider,
        LLMProviderType,
        Limits as LLMProviderLimits,
        LLMModel,
    )
    from primer.llm.anthropic import AnthropicLLM
    from primer.coordinator.in_memory import InMemoryRateLimiter

    config = AnthropicConfig(api_key="sk-test")
    provider = LLMProvider(
        id="test-anthropic",
        name="Test Anthropic",
        provider=LLMProviderType.ANTHROPIC,
        config=config,
        models=[LLMModel(name="claude-3-haiku-20240307", context_length=200000)],
        limits=LLMProviderLimits(max_concurrency=1),
    )
    return AnthropicLLM(provider, rate_limiter=InMemoryRateLimiter())


def _make_openresponses_adapter():
    from primer.model.provider import (
        OpenResponsesConfig,
        OpenResponsesFlavor,
        LLMProvider,
        LLMProviderType,
        Limits as LLMProviderLimits,
        LLMModel,
    )
    from primer.llm.openresponses import OpenResponsesLLM
    from primer.coordinator.in_memory import InMemoryRateLimiter

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
    return OpenResponsesLLM(provider, rate_limiter=InMemoryRateLimiter())


def _make_gemini_adapter():
    from primer.model.provider import (
        GoogleConfig,
        LLMProvider,
        LLMProviderType,
        Limits as LLMProviderLimits,
        LLMModel,
    )
    from primer.llm.gemini import GeminiLLM
    from primer.coordinator.in_memory import InMemoryRateLimiter

    config = GoogleConfig(api_key="AIza-test")
    provider = LLMProvider(
        id="test-gemini",
        name="Test Gemini",
        provider=LLMProviderType.GEMINI,
        config=config,
        models=[LLMModel(name="gemini-2.0-flash", context_length=1000000)],
        limits=LLMProviderLimits(max_concurrency=1),
    )
    return GeminiLLM(provider, rate_limiter=InMemoryRateLimiter())


def _make_ollama_adapter():
    from primer.model.provider import (
        OllamaConfig,
        LLMProvider,
        LLMProviderType,
        Limits as LLMProviderLimits,
        LLMModel,
    )
    from primer.llm.ollama import OllamaLLM
    from primer.coordinator.in_memory import InMemoryRateLimiter

    config = OllamaConfig(url="http://localhost:11434")
    provider = LLMProvider(
        id="test-ollama",
        name="Test Ollama",
        provider=LLMProviderType.OLLAMA,
        config=config,
        models=[LLMModel(name="llama3.2", context_length=128000)],
        limits=LLMProviderLimits(max_concurrency=1),
    )
    return OllamaLLM(provider, rate_limiter=InMemoryRateLimiter())


# ---------------------------------------------------------------------------
# Minimal fake stream events
# ---------------------------------------------------------------------------


def _fake_anthropic_stream_events():
    """Return raw Anthropic SDK events that produce a Usage event."""
    # message_start
    msg_start = MagicMock()
    msg_start.type = "message_start"
    msg = MagicMock()
    msg.usage = MagicMock(input_tokens=10)
    msg.id = "req-123"
    msg.model = "claude-3-haiku-20240307"
    msg_start.message = msg

    # message_delta with stop reason
    msg_delta = MagicMock()
    msg_delta.type = "message_delta"
    delta = MagicMock()
    delta.stop_reason = "end_turn"
    msg_delta.delta = delta
    msg_delta.usage = MagicMock(output_tokens=20)

    # message_stop
    msg_stop = MagicMock()
    msg_stop.type = "message_stop"

    return [msg_start, msg_delta, msg_stop]


def _fake_openresponses_stream_events():
    """Return raw OpenAI Responses events that produce a Usage event."""
    # response.created
    created = MagicMock()
    created.type = "response.created"
    response = MagicMock()
    response.id = "resp-123"
    response.model = "gpt-4o-mini"
    created.response = response

    # response.completed
    completed = MagicMock()
    completed.type = "response.completed"
    usage = MagicMock()
    usage.input_tokens = 15
    usage.output_tokens = 25
    usage.input_tokens_details = None
    usage.output_tokens_details = None
    complete_response = MagicMock()
    complete_response.usage = usage
    completed.response = complete_response

    return [created, completed]


def _fake_gemini_stream_chunks():
    """Return fake Gemini chunks that produce text + usage."""
    # We need chunks that _translate_chunk can handle.
    # A chunk with candidates + a final chunk with usage_metadata.
    chunk1 = MagicMock()
    chunk1.candidates = []
    chunk1.usage_metadata = None

    chunk2 = MagicMock()
    chunk2.candidates = []
    usage_meta = MagicMock()
    usage_meta.prompt_token_count = 5
    usage_meta.candidates_token_count = 10
    usage_meta.total_token_count = 15
    chunk2.usage_metadata = usage_meta

    return [chunk1, chunk2]


def _fake_ollama_stream_chunks():
    """Return fake Ollama chunks including a final done=True chunk."""
    chunk1 = MagicMock()
    chunk1.done = False
    chunk1.model = "llama3.2"
    message1 = MagicMock()
    message1.content = "hello"
    message1.thinking = None
    message1.tool_calls = []
    chunk1.message = message1

    chunk2 = MagicMock()
    chunk2.done = True
    chunk2.done_reason = "stop"
    chunk2.model = "llama3.2"
    chunk2.prompt_eval_count = 8
    chunk2.eval_count = 16
    message2 = MagicMock()
    message2.content = None
    message2.thinking = None
    message2.tool_calls = []
    chunk2.message = message2

    return [chunk1, chunk2]


# ---------------------------------------------------------------------------
# Task 6 tests: Anthropic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_span_attributes(in_memory_tracer_provider):
    provider, exporter = in_memory_tracer_provider
    adapter = _make_anthropic_adapter()

    async def _fake_sdk_stream():
        for e in _fake_anthropic_stream_events():
            yield e

    mock_sdk_stream = _fake_sdk_stream()

    with _patch_tracer(provider):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(
                return_value=mock_sdk_stream
            )
            from primer.model.chat import Message, TextPart
            messages = [Message(role="user", parts=[TextPart(text="hi")])]
            await _drain(adapter.stream(
                model="claude-3-haiku-20240307",
                messages=messages,
            ))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "llm.stream"
    attrs = span.attributes
    assert attrs["llm.provider"] == "anthropic"
    assert attrs["llm.model"] == "claude-3-haiku-20240307"
    assert attrs["llm.usage.tokens_in"] == 10
    assert attrs["llm.usage.tokens_out"] == 20


@pytest.mark.asyncio
async def test_anthropic_token_counters(in_memory_tracer_provider):
    provider, exporter = in_memory_tracer_provider
    adapter = _make_anthropic_adapter()

    async def _fake_sdk_stream():
        for e in _fake_anthropic_stream_events():
            yield e

    from primer.observability import metrics as m

    with _patch_tracer(provider):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            from primer.model.chat import Message, TextPart
            messages = [Message(role="user", parts=[TextPart(text="hi")])]
            await _drain(adapter.stream(
                model="claude-3-haiku-20240307",
                messages=messages,
            ))

    # Verify counters incremented
    in_val = m.llm_tokens_total.labels("anthropic", "in")._value.get()
    out_val = m.llm_tokens_total.labels("anthropic", "out")._value.get()
    assert in_val == 10.0
    assert out_val == 20.0


@pytest.mark.asyncio
async def test_anthropic_failure_counter(in_memory_tracer_provider):
    provider, exporter = in_memory_tracer_provider
    adapter = _make_anthropic_adapter()

    from primer.observability import metrics as m
    from primer.model.except_ import MatrixError

    with _patch_tracer(provider):
        with patch.object(adapter, "_get_client") as mock_client:
            # Use a RuntimeError — something the Anthropic classifier doesn't know,
            # so it wraps to ProviderError (still a MatrixError).
            mock_client.return_value.messages.create = AsyncMock(
                side_effect=RuntimeError("boom")
            )
            from primer.model.chat import Message, TextPart
            messages = [Message(role="user", parts=[TextPart(text="hi")])]
            with pytest.raises(MatrixError):
                await _drain(adapter.stream(
                    model="claude-3-haiku-20240307",
                    messages=messages,
                ))

    # The failure counter should have been incremented with the classified exception type.
    # RuntimeError → classify_anthropic_exception wraps to ProviderError.
    fail_samples = [
        s for metric in m.llm_failure_total.collect()
        for s in metric.samples
        if s.labels.get("provider") == "anthropic" and s.name == "llm_failure_total"
    ]
    total = sum(s.value for s in fail_samples)
    assert total == 1.0, f"expected 1 failure count; samples={fail_samples}"

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert len(spans[0].events) > 0  # exception event recorded


# ---------------------------------------------------------------------------
# Task 6 tests: OpenResponses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openresponses_span_attributes(in_memory_tracer_provider):
    provider, exporter = in_memory_tracer_provider
    adapter = _make_openresponses_adapter()

    async def _fake_sdk_stream():
        for e in _fake_openresponses_stream_events():
            yield e

    with _patch_tracer(provider):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.responses.create = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            from primer.model.chat import Message, TextPart
            messages = [Message(role="user", parts=[TextPart(text="hi")])]
            await _drain(adapter.stream(
                model="gpt-4o-mini",
                messages=messages,
            ))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["llm.provider"] == "openresponses"
    assert attrs["llm.model"] == "gpt-4o-mini"
    assert attrs["llm.usage.tokens_in"] == 15
    assert attrs["llm.usage.tokens_out"] == 25


@pytest.mark.asyncio
async def test_openresponses_token_counters(in_memory_tracer_provider):
    provider, exporter = in_memory_tracer_provider
    adapter = _make_openresponses_adapter()

    from primer.observability import metrics as m

    async def _fake_sdk_stream():
        for e in _fake_openresponses_stream_events():
            yield e

    with _patch_tracer(provider):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.responses.create = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            from primer.model.chat import Message, TextPart
            messages = [Message(role="user", parts=[TextPart(text="hi")])]
            await _drain(adapter.stream(
                model="gpt-4o-mini",
                messages=messages,
            ))

    in_val = m.llm_tokens_total.labels("openresponses", "in")._value.get()
    out_val = m.llm_tokens_total.labels("openresponses", "out")._value.get()
    assert in_val == 15.0
    assert out_val == 25.0


# ---------------------------------------------------------------------------
# Task 6 tests: Gemini
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_span_attributes(in_memory_tracer_provider):
    provider, exporter = in_memory_tracer_provider
    adapter = _make_gemini_adapter()

    async def _fake_sdk_stream():
        for c in _fake_gemini_stream_chunks():
            yield c

    with _patch_tracer(provider):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.aio.models.generate_content_stream = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            from primer.model.chat import Message, TextPart
            messages = [Message(role="user", parts=[TextPart(text="hi")])]
            await _drain(adapter.stream(
                model="gemini-2.0-flash",
                messages=messages,
            ))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["llm.provider"] == "gemini"
    assert attrs["llm.model"] == "gemini-2.0-flash"


# ---------------------------------------------------------------------------
# Task 6 tests: Ollama
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_span_attributes(in_memory_tracer_provider):
    provider, exporter = in_memory_tracer_provider
    adapter = _make_ollama_adapter()

    async def _fake_sdk_stream():
        for c in _fake_ollama_stream_chunks():
            yield c

    with _patch_tracer(provider):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.chat = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            from primer.model.chat import Message, TextPart
            messages = [Message(role="user", parts=[TextPart(text="hi")])]
            await _drain(adapter.stream(
                model="llama3.2",
                messages=messages,
            ))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["llm.provider"] == "ollama"
    assert attrs["llm.model"] == "llama3.2"
    assert attrs["llm.usage.tokens_in"] == 8
    assert attrs["llm.usage.tokens_out"] == 16


@pytest.mark.asyncio
async def test_ollama_token_counters(in_memory_tracer_provider):
    provider, exporter = in_memory_tracer_provider
    adapter = _make_ollama_adapter()

    from primer.observability import metrics as m

    async def _fake_sdk_stream():
        for c in _fake_ollama_stream_chunks():
            yield c

    with _patch_tracer(provider):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.chat = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            from primer.model.chat import Message, TextPart
            messages = [Message(role="user", parts=[TextPart(text="hi")])]
            await _drain(adapter.stream(
                model="llama3.2",
                messages=messages,
            ))

    in_val = m.llm_tokens_total.labels("ollama", "in")._value.get()
    out_val = m.llm_tokens_total.labels("ollama", "out")._value.get()
    assert in_val == 8.0
    assert out_val == 16.0


@pytest.mark.asyncio
async def test_llm_duration_observed(in_memory_tracer_provider):
    """Duration histogram should have a sample after a successful stream."""
    provider, exporter = in_memory_tracer_provider
    adapter = _make_ollama_adapter()

    from primer.observability import metrics as m

    async def _fake_sdk_stream():
        for c in _fake_ollama_stream_chunks():
            yield c

    with _patch_tracer(provider):
        with patch.object(adapter, "_get_client") as mock_client:
            mock_client.return_value.chat = AsyncMock(
                return_value=_fake_sdk_stream()
            )
            from primer.model.chat import Message, TextPart
            messages = [Message(role="user", parts=[TextPart(text="hi")])]
            await _drain(adapter.stream(
                model="llama3.2",
                messages=messages,
            ))

    # Collect from the parent metric (not the labeled child) to get all samples
    samples = {s.name: s.value for metric in m.llm_duration_seconds.collect()
               for s in metric.samples if s.labels.get("provider") == "ollama"}
    count = samples.get("llm_duration_seconds_count", 0)
    assert count == 1, f"expected 1 observation, got {count}; samples={samples}"
