"""primer.llm.counting.count_prompt_tokens: never raises, never mislabels.

The wrapper is the one place that turns a counter failure into an estimate.
These tests pin the contract the live turn path will rely on: every expected
failure becomes a labelled estimate; only TRANSIENT ones are negative-cached;
a deterministic rejection is never cached (one conversation's 400 must not
disable counting for every session on that model); an unexpected exception is a
bug (counted, logged at ERROR) and never a count; cancellation propagates.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import primer.observability.metrics as metrics
from primer.llm.counting import (
    NegativeCache,
    count_prompt_tokens,
    fallback_bug_count,
)
from primer.model.chat import Message, TextPart
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    NetworkError,
    ProviderTimeoutError,
    RateLimitError,
    ServerError,
    TokenCounterUnavailable,
    UnsupportedContentError,
)
from primer.model.token_count import TokenCount

MODEL = SimpleNamespace(provider_id="prov-1", profile_id="prof-1", model_name="gpt-4o")
MESSAGES = [
    Message(role="system", parts=[TextPart(text="be brief")]),
    Message(role="user", parts=[TextPart(text="hello world")]),
]


def estimate(messages, tools) -> int:
    return 1234


class _Counter:
    """An LLM whose counter does whatever the test scripts."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls: list[dict] = []

    async def count_tokens_detailed(self, *, model, messages, tools=None):
        self.calls.append({"model": model, "messages": messages, "tools": tools})
        outcome = self.behaviour
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome()
        return outcome


@pytest.fixture(autouse=True)
def _fresh_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


@pytest.fixture
def cache():
    clock = SimpleNamespace(now=0.0)
    cache = NegativeCache(ttl_s=60.0, clock=lambda: clock.now)
    cache.clock = clock
    return cache


def _sample(provider, source, outcome) -> float:
    return metrics.registry.get_sample_value(
        "llm_count_tokens_total",
        {"provider_id": provider, "source": source, "outcome": outcome},
    ) or 0.0


async def _count(llm, cache, **kwargs):
    return await count_prompt_tokens(
        llm, model=MODEL, messages=MESSAGES, estimate=estimate,
        negative_cache=cache, **kwargs,
    )


# ---- what a successful count is labelled -----------------------------------


@pytest.mark.parametrize(
    ("detail", "source"),
    [
        (TokenCount(total=500, exact=True), "native"),
        (TokenCount(total=500, exact=False), "native_approx"),
        (TokenCount(total=500, exact=True, estimated_components=("media",)), "native_plus_estimated"),
        (TokenCount(total=500, exact=False, estimated_components=("system", "tools")), "native_plus_estimated"),
    ],
)
async def test_a_successful_count_is_labelled_by_what_it_stands_on(detail, source, cache):
    result = await _count(_Counter(detail), cache)
    assert (result.total, result.source, result.outcome) == (500, source, "ok")
    assert result.estimated_components == detail.estimated_components
    assert _sample("prov-1", source, "ok") == 1.0


async def test_the_counter_receives_the_system_prompt_as_a_message_and_the_model_name(cache):
    counter = _Counter(TokenCount(total=1, exact=True))
    await _count(counter, cache)
    call = counter.calls[0]
    assert call["model"] == "gpt-4o"
    assert [m.role for m in call["messages"]] == ["system", "user"]
    assert call["tools"] is None


async def test_the_provider_label_falls_back_to_the_profile_for_an_aggregated_model(cache):
    aggregated = SimpleNamespace(provider_id=None, profile_id="agg-1", model_name=None)
    result = await count_prompt_tokens(
        _Counter(TokenCount(total=3, exact=False)), model=aggregated,
        messages=MESSAGES, estimate=estimate, negative_cache=cache,
    )
    assert result.outcome == "ok"
    assert _sample("agg-1", "native_approx", "ok") == 1.0


# ---- expected failures become labelled estimates ----------------------------


async def test_an_unavailable_vocabulary_is_an_estimate_and_is_not_cached(cache):
    counter = _Counter(TokenCounterUnavailable("no vocabulary"))
    first = await _count(counter, cache)
    assert (first.total, first.source, first.outcome) == (1234, "estimate", "fallback_unavailable")
    await _count(counter, cache)
    assert len(counter.calls) == 2, "a permanent failure is the loader's to remember, not the wrapper's"


async def test_a_transient_unavailable_is_cached_for_the_ttl_then_retried(cache):
    counter = _Counter(TokenCounterUnavailable("queue", transient=True))
    await _count(counter, cache)
    second = await _count(counter, cache)
    assert second.outcome == "negative_cached" and len(counter.calls) == 1
    cache.clock.now = 59.0
    assert (await _count(counter, cache)).outcome == "negative_cached"
    cache.clock.now = 61.0
    assert (await _count(counter, cache)).outcome == "fallback_unavailable"
    assert len(counter.calls) == 2


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (NetworkError("down"), "fallback_transient"),
        (ServerError("500"), "fallback_transient"),
        (RateLimitError("429"), "fallback_transient"),
        (ProviderTimeoutError("slow"), "fallback_timeout"),
        (TimeoutError(), "fallback_timeout"),
    ],
)
async def test_transient_provider_failures_estimate_and_are_cached(error, outcome, cache):
    counter = _Counter(error)
    first = await _count(counter, cache)
    assert (first.source, first.outcome, first.total) == ("estimate", outcome, 1234)
    assert (await _count(counter, cache)).outcome == "negative_cached"
    assert len(counter.calls) == 1


@pytest.mark.parametrize(
    "error",
    [BadRequestError("bad media"), AuthenticationError("401"), UnsupportedContentError("audio")],
)
async def test_a_deterministic_rejection_is_estimated_and_never_cached(error, cache, caplog):
    counter = _Counter(error)
    with caplog.at_level(logging.WARNING, logger="primer.llm.counting"):
        first = await _count(counter, cache)
    assert (first.source, first.outcome) == ("estimate", "fallback_rejected")
    assert any("rejected" in r.getMessage() for r in caplog.records)
    await _count(counter, cache)
    assert len(counter.calls) == 2, "caching a 400 would spread one conversation's failure to every session"


async def test_the_negative_cache_is_per_provider_and_model(cache):
    failing = _Counter(ServerError("500"))
    await _count(failing, cache)
    other_model = SimpleNamespace(provider_id="prov-1", profile_id="prof-1", model_name="gpt-4o-mini")
    ok = _Counter(TokenCount(total=9, exact=True))
    result = await count_prompt_tokens(
        ok, model=other_model, messages=MESSAGES, estimate=estimate, negative_cache=cache,
    )
    assert result.outcome == "ok"


async def test_a_counter_that_never_returns_is_cut_off_by_the_backstop(cache):
    async def forever():
        await asyncio.sleep(3600)

    counter = _Counter(forever)
    result = await _count(counter, cache, backstop_s=0.05)
    assert (result.source, result.outcome) == ("estimate", "fallback_timeout")
    assert (await _count(counter, cache, backstop_s=0.05)).outcome == "negative_cached"


# ---- the contract's edges ---------------------------------------------------


async def test_cancellation_propagates(cache):
    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _count(_Counter(cancelled), cache)


async def test_an_object_with_no_counter_is_an_estimate_not_an_error(cache):
    result = await _count(SimpleNamespace(), cache)
    assert (result.source, result.outcome, result.total) == ("estimate", "no_counter", 1234)


async def test_a_legacy_counter_is_accepted_and_never_labelled_exact(cache):
    class Legacy:
        async def count_tokens(self, *, model, messages, tools=None) -> int:
            return 77

    result = await _count(Legacy(), cache)
    assert (result.total, result.source, result.outcome) == (77, "native_approx", "ok")


@pytest.mark.allow_fallback_bug
async def test_an_unexpected_exception_is_a_bug_not_a_count(cache, caplog):
    before = fallback_bug_count()
    counter = _Counter(AttributeError("'NoneType' object has no attribute 'encode'"))
    with caplog.at_level(logging.ERROR, logger="primer.llm.counting"):
        result = await _count(counter, cache)
    assert (result.source, result.outcome, result.total) == ("estimate", "fallback_bug", 1234)
    assert fallback_bug_count() == before + 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and errors[0].exc_info, "a bug must log the traceback"
    assert _sample("prov-1", "estimate", "fallback_bug") == 1.0
    await _count(counter, cache)
    assert len(counter.calls) == 2, "a bug is never negative-cached; it must keep showing up"


async def test_one_warning_per_negative_cache_window(cache, caplog):
    counter = _Counter(ServerError("500"))
    with caplog.at_level(logging.WARNING, logger="primer.llm.counting"):
        for _ in range(5):
            await _count(counter, cache)
    assert len([r for r in caplog.records if "failed transiently" in r.getMessage()]) == 1


async def test_the_duration_histogram_and_ready_gauge_are_registered():
    metrics.llm_tokenizer_ready.labels("o200k_base").set(1)
    assert metrics.registry.get_sample_value(
        "llm_tokenizer_ready", {"name": "o200k_base"},
    ) == 1.0
    counter = _Counter(TokenCount(total=1, exact=True))
    await count_prompt_tokens(
        counter, model=MODEL, messages=MESSAGES, estimate=estimate,
        negative_cache=NegativeCache(),
    )
    assert metrics.registry.get_sample_value(
        "llm_count_tokens_seconds_count", {"provider_id": "prov-1", "source": "native"},
    ) == 1.0
