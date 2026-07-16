"""Unit tests for AggregatedLLM (fake-LLM async generators; no SDK/network)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest

from primer.int.llm import LLM
from primer.llm.aggregated import AggregatedLLM
from primer.model.chat import (
    Done,
    Error as ChatError,
    StreamStart,
    StreamEvent,
    TextDelta,
)
from primer.model.except_ import (
    AuthenticationError,
    BadRequestError,
    ConfigError,
    NotFoundError,
    ProviderTimeoutError,
    RateLimitError,
    ServerError,
)
from primer.model.provider import (
    AggregatedLLMConfig,
    AggregatedMember,
    FailoverClasses,
    FailoverPoint,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
    RoutingStrategy,
)


class _FakeLLM(LLM):
    """Scripted downstream LLM.

    ``events`` is the list yielded once the stream opens. ``connect_exc``,
    if set, is raised on the first ``__anext__`` (connect phase). ``mid_exc``,
    if set, is RAISED after all ``events`` are yielded (models a mid-stream
    raise, e.g. ProviderTimeoutError - anthropic.py:760, openchat.py:284).
    ``record`` (a list) captures each ``model=`` the adapter was asked to
    stream. ``stream_closed`` flips True when the stream generator is
    finalized (normal exhaustion OR GeneratorExit from ``aclose``); ``yielded``
    counts events actually pulled, so an abandonment test can prove the
    downstream stream was cut short and cleaned up.
    """

    def __init__(self, *, events=None, connect_exc=None, mid_exc=None,
                 record=None, name="fake"):
        self._events = events or []
        self._connect_exc = connect_exc
        self._mid_exc = mid_exc
        self._record = record
        self._name = name
        self.closed = False
        self.stream_closed = False
        self.yielded = 0

    async def list_models(self):
        return [self._name]

    async def count_tokens(self, *, model, messages, tools=None) -> int:
        return 7

    async def stream(self, *, model, messages, **kwargs) -> AsyncIterator[StreamEvent]:
        if self._record is not None:
            self._record.append(model)
        if self._connect_exc is not None:
            raise self._connect_exc
        try:
            for ev in self._events:
                self.yielded += 1
                yield ev
            if self._mid_exc is not None:
                raise self._mid_exc
        finally:
            # Runs on normal exhaustion AND on GeneratorExit thrown by
            # ``agen.aclose()`` when the aggregated stream is abandoned.
            self.stream_closed = True

    async def aclose(self) -> None:
        self.closed = True


def _row(config: AggregatedLLMConfig) -> LLMProvider:
    return LLMProvider(
        id="agg-1",
        provider=LLMProviderType.AGGREGATED,
        models=[LLMModel(name="virtual-1", context_length=200000)],
        config=config,
        limits=Limits(max_concurrency=4),
    )


def _resolver(mapping: dict[str, LLM]):
    async def resolve(pid: str) -> LLM:
        if pid not in mapping:
            raise NotFoundError(f"LLMProvider {pid!r} does not exist")
        return mapping[pid]
    return resolve


_MSG = []  # empty message list is fine for the fakes


async def _drain(agen) -> list[StreamEvent]:
    return [ev async for ev in agen]


@pytest.mark.asyncio
async def test_connect_raise_fails_over_to_next_member():
    good = _FakeLLM(events=[StreamStart(model="m2"), TextDelta(text="hi", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    bad = _FakeLLM(connect_exc=RateLimitError("429"))
    cfg = AggregatedLLMConfig(members=[
        AggregatedMember(provider_id="bad", model_name="m1"),
        AggregatedMember(provider_id="good", model_name="m2"),
    ])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    events = await _drain(agg.stream(model="virtual-1", messages=_MSG))
    assert any(isinstance(e, TextDelta) for e in events)
    assert isinstance(events[-1], Done)


@pytest.mark.asyncio
async def test_first_event_error_fails_over_no_tokens_emitted():
    # Realistic: real classifiers emit code=None for a rate-limit that
    # surfaces as a YIELDED fatal Error (anthropic_errors.py, ollama.py).
    # code=None is eligible under either policy.
    bad = _FakeLLM(events=[ChatError(fatal=True, code=None, message="429")])
    good = _FakeLLM(events=[StreamStart(model="m2"), TextDelta(text="ok", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(members=[
        AggregatedMember(provider_id="bad", model_name="m1"),
        AggregatedMember(provider_id="good", model_name="m2"),
    ])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    events = await _drain(agg.stream(model="virtual-1", messages=_MSG))
    # No Error from the failed member reached the subscriber.
    assert not any(isinstance(e, ChatError) for e in events)
    assert any(isinstance(e, TextDelta) for e in events)


@pytest.mark.asyncio
async def test_first_event_error_with_hypothetical_transient_code_fails_over():
    # Hypothetical: no current adapter emits a non-null transient code on a
    # yielded Error, but if one did, a config-eligible code fails over under
    # the default TRANSIENT_AND_CONFIG. Kept as the one non-null-code case.
    bad = _FakeLLM(events=[ChatError(fatal=True, code="rate_limit", message="429")])
    good = _FakeLLM(events=[TextDelta(text="ok", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(members=[
        AggregatedMember(provider_id="bad", model_name="m1"),
        AggregatedMember(provider_id="good", model_name="m2"),
    ])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    events = await _drain(agg.stream(model="virtual-1", messages=_MSG))
    assert any(isinstance(e, TextDelta) for e in events)


@pytest.mark.asyncio
async def test_stream_start_then_error_surfaces_no_failover():
    # PINNED DECISION A: the commit point is the FIRST event of ANY kind
    # (StreamStart counts). Member[0] yields StreamStart then a fatal Error;
    # since StreamStart already committed, the Error SURFACES in the default
    # BEFORE_FIRST_TOKEN mode - no failover to member[1].
    bad = _FakeLLM(events=[StreamStart(model="m1"),
                           ChatError(fatal=True, code=None, message="boom")])
    good = _FakeLLM(events=[TextDelta(text="SHOULD-NOT-APPEAR", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(members=[
        AggregatedMember(provider_id="bad", model_name="m1"),
        AggregatedMember(provider_id="good", model_name="m2"),
    ])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    events = await _drain(agg.stream(model="virtual-1", messages=_MSG))
    assert isinstance(events[0], StreamStart)
    assert isinstance(events[-1], ChatError)
    assert not any(isinstance(e, TextDelta) for e in events)  # member[1] never ran


@pytest.mark.asyncio
async def test_abandoned_stream_closes_downstream_generator():
    # IMPORTANT: on consumer abandonment (task cancellation / client
    # disconnect) the aggregated stream's per-member ``finally`` must aclose
    # the downstream generator so its rate-limiter slot is released.
    good = _FakeLLM(events=[StreamStart(model="m"), TextDelta(text="a", index=0),
                            TextDelta(text="b", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(members=[AggregatedMember(provider_id="good", model_name="m")])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"good": good}))
    agen = agg.stream(model="virtual-1", messages=_MSG)
    first = await agen.__anext__()          # StreamStart committed
    assert isinstance(first, StreamStart)
    await agen.aclose()                     # consumer abandons the stream
    assert good.stream_closed is True       # downstream generator finalized
    assert good.yielded < 4                 # member stream was cut short


@pytest.mark.asyncio
async def test_token_then_error_before_first_token_surfaces_error_no_failover():
    # Member[0] commits (a token) then yields a fatal Error. In the default
    # BEFORE_FIRST_TOKEN mode the error is surfaced; no failover, no dup.
    bad = _FakeLLM(events=[TextDelta(text="partial", index=0),
                           ChatError(fatal=True, code=None, message="429")])
    good = _FakeLLM(events=[TextDelta(text="SHOULD-NOT-APPEAR", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(members=[
        AggregatedMember(provider_id="bad", model_name="m1"),
        AggregatedMember(provider_id="good", model_name="m2"),
    ])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    events = await _drain(agg.stream(model="virtual-1", messages=_MSG))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["partial"]
    assert isinstance(events[-1], ChatError)


@pytest.mark.asyncio
async def test_all_members_fail_raises_aggregated_rate_limit():
    a = _FakeLLM(connect_exc=RateLimitError("429 a"))
    b = _FakeLLM(connect_exc=ServerError("500 b"))
    cfg = AggregatedLLMConfig(members=[
        AggregatedMember(provider_id="a", model_name="m"),
        AggregatedMember(provider_id="b", model_name="m"),
    ])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"a": a, "b": b}))
    with pytest.raises(RateLimitError, match="all 2 aggregated members failed"):
        await _drain(agg.stream(model="virtual-1", messages=_MSG))


@pytest.mark.asyncio
async def test_not_found_member_is_skipped():
    good = _FakeLLM(events=[TextDelta(text="ok", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(members=[
        AggregatedMember(provider_id="missing", model_name="m1"),
        AggregatedMember(provider_id="good", model_name="m2"),
    ])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"good": good}))
    events = await _drain(agg.stream(model="virtual-1", messages=_MSG))
    assert any(isinstance(e, TextDelta) for e in events)


@pytest.mark.asyncio
async def test_non_eligible_exception_propagates_unchanged():
    # ConfigError is neither ProviderError nor NetworkError -> propagate.
    bad = _FakeLLM(connect_exc=ConfigError("boom"))
    cfg = AggregatedLLMConfig(members=[AggregatedMember(provider_id="bad", model_name="m")])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad}))
    with pytest.raises(ConfigError, match="boom"):
        await _drain(agg.stream(model="virtual-1", messages=_MSG))


@pytest.mark.asyncio
async def test_stream_maps_virtual_model_to_each_member_model_name():
    record: list[str] = []
    good = _FakeLLM(events=[Done(stop_reason="stop", raw_reason="stop")], record=record)
    cfg = AggregatedLLMConfig(members=[AggregatedMember(provider_id="good", model_name="member-model")])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"good": good}))
    await _drain(agg.stream(model="virtual-1", messages=_MSG))
    # The incoming virtual name is NOT forwarded; the member's own model is.
    assert record == ["member-model"]


@pytest.mark.asyncio
async def test_list_models_returns_virtual_names():
    cfg = AggregatedLLMConfig(members=[AggregatedMember(provider_id="p", model_name="m")])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({}))
    assert list(await agg.list_models()) == ["virtual-1"]


@pytest.mark.asyncio
async def test_count_tokens_delegates_to_first_resolvable_member():
    good = _FakeLLM(name="g")
    cfg = AggregatedLLMConfig(members=[
        AggregatedMember(provider_id="missing", model_name="m1"),
        AggregatedMember(provider_id="good", model_name="m2"),
    ])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"good": good}))
    assert await agg.count_tokens(model="virtual-1", messages=_MSG) == 7


@pytest.mark.asyncio
async def test_aclose_is_noop_does_not_close_members():
    good = _FakeLLM()
    cfg = AggregatedLLMConfig(members=[AggregatedMember(provider_id="good", model_name="m")])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"good": good}))
    await agg.aclose()
    assert good.closed is False


@pytest.mark.asyncio
async def test_failover_is_logged(caplog):
    bad = _FakeLLM(connect_exc=RateLimitError("429"))
    good = _FakeLLM(events=[Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(members=[
        AggregatedMember(provider_id="bad", model_name="m1"),
        AggregatedMember(provider_id="good", model_name="m2"),
    ])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    with caplog.at_level(logging.INFO, logger="primer.llm.aggregated"):
        await _drain(agg.stream(model="virtual-1", messages=_MSG))
    assert any("failing over" in r.message or "failing over" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_empty_stream_member_is_treated_as_failure():
    # Member[0]'s stream ends immediately (StopAsyncIteration on the first
    # fetch) -> recorded as a member failure and failover proceeds to
    # member[1] (aggregated.py:224-227).
    empty = _FakeLLM(events=[])
    good = _FakeLLM(events=[TextDelta(text="ok", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(members=[
        AggregatedMember(provider_id="empty", model_name="m1"),
        AggregatedMember(provider_id="good", model_name="m2"),
    ])
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"empty": empty, "good": good}))
    events = await _drain(agg.stream(model="virtual-1", messages=_MSG))
    assert any(isinstance(e, TextDelta) for e in events)
    assert isinstance(events[-1], Done)


@pytest.mark.asyncio
async def test_sequential_always_starts_at_member_zero():
    records = {"a": [], "b": []}
    a = _FakeLLM(events=[Done(stop_reason="stop", raw_reason="stop")], record=records["a"], name="a")
    b = _FakeLLM(events=[Done(stop_reason="stop", raw_reason="stop")], record=records["b"], name="b")
    cfg = AggregatedLLMConfig(
        strategy=RoutingStrategy.SEQUENTIAL,
        members=[
            AggregatedMember(provider_id="a", model_name="ma"),
            AggregatedMember(provider_id="b", model_name="mb"),
        ],
    )
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"a": a, "b": b}))
    for _ in range(3):
        await _drain(agg.stream(model="virtual-1", messages=_MSG))
    # Member[0] served every call; member[1] never reached.
    assert len(records["a"]) == 3
    assert len(records["b"]) == 0


@pytest.mark.asyncio
async def test_round_robin_rotates_starting_member():
    records = {"a": [], "b": [], "c": []}

    def mk(k):
        return _FakeLLM(events=[Done(stop_reason="stop", raw_reason="stop")],
                        record=records[k], name=k)

    a, b, c = mk("a"), mk("b"), mk("c")
    cfg = AggregatedLLMConfig(
        strategy=RoutingStrategy.ROUND_ROBIN,
        members=[
            AggregatedMember(provider_id="a", model_name="ma"),
            AggregatedMember(provider_id="b", model_name="mb"),
            AggregatedMember(provider_id="c", model_name="mc"),
        ],
    )
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"a": a, "b": b, "c": c}))
    for _ in range(3):
        await _drain(agg.stream(model="virtual-1", messages=_MSG))
    # Each call commits to its start member, so each member served exactly once.
    assert len(records["a"]) == 1
    assert len(records["b"]) == 1
    assert len(records["c"]) == 1


@pytest.mark.asyncio
async def test_transient_only_propagates_auth_error():
    # failover_on=TRANSIENT: an AuthenticationError is NOT eligible -> propagate.
    bad = _FakeLLM(connect_exc=AuthenticationError("401"))
    good = _FakeLLM(events=[Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(
        failover_on=FailoverClasses.TRANSIENT,
        members=[
            AggregatedMember(provider_id="bad", model_name="m1"),
            AggregatedMember(provider_id="good", model_name="m2"),
        ],
    )
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    with pytest.raises(AuthenticationError):
        await _drain(agg.stream(model="virtual-1", messages=_MSG))


@pytest.mark.asyncio
async def test_transient_and_config_fails_over_auth_error():
    bad = _FakeLLM(connect_exc=AuthenticationError("401"))
    good = _FakeLLM(events=[TextDelta(text="ok", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(
        failover_on=FailoverClasses.TRANSIENT_AND_CONFIG,
        members=[
            AggregatedMember(provider_id="bad", model_name="m1"),
            AggregatedMember(provider_id="good", model_name="m2"),
        ],
    )
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    events = await _drain(agg.stream(model="virtual-1", messages=_MSG))
    assert any(isinstance(e, TextDelta) for e in events)


def test_yielded_eligibility_policy():
    from primer.llm.aggregated import _yielded_eligible
    # timeout code + None -> eligible under both policies.
    for policy in (FailoverClasses.TRANSIENT, FailoverClasses.TRANSIENT_AND_CONFIG):
        assert _yielded_eligible("stream_timeout", policy) is True
        assert _yielded_eligible(None, policy) is True
    # a non-null, non-timeout code (bad-request / OpenAI native) -> config only.
    assert _yielded_eligible("invalid_request_error", FailoverClasses.TRANSIENT) is False
    assert _yielded_eligible("invalid_request_error", FailoverClasses.TRANSIENT_AND_CONFIG) is True


@pytest.mark.asyncio
async def test_round_robin_cursor_is_concurrency_safe():
    # N concurrent stream calls must each get a distinct start member (a clean
    # rotation with no cursor races), exercising _cursor_lock for real.
    import asyncio

    n = 6
    records = {str(i): [] for i in range(n)}

    def mk(k):
        return _FakeLLM(events=[Done(stop_reason="stop", raw_reason="stop")],
                        record=records[k], name=k)

    fakes = {str(i): mk(str(i)) for i in range(n)}
    cfg = AggregatedLLMConfig(
        strategy=RoutingStrategy.ROUND_ROBIN,
        members=[AggregatedMember(provider_id=str(i), model_name=f"m{i}") for i in range(n)],
    )
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver(fakes))
    await asyncio.gather(*[
        _drain(agg.stream(model="virtual-1", messages=_MSG)) for _ in range(n)
    ])
    # Each member was the committed start member exactly once (a full rotation
    # with no duplicates), proving the cursor advanced atomically per call.
    served = sorted(k for k, calls in records.items() if calls)
    assert served == sorted(str(i) for i in range(n))
    assert all(len(calls) == 1 for calls in records.values())


# --- MID_STREAM failover mode (Task 4: regression-pin, expected to pass) ---


@pytest.mark.asyncio
async def test_mid_stream_restarts_on_next_member_with_dup():
    # Member[0] commits a token then yields a fatal eligible Error mid-stream.
    # With MID_STREAM, the adapter restarts on member[1]; the "partial" token
    # was already yielded (documented duplication).
    bad = _FakeLLM(events=[
        TextDelta(text="partial", index=0),
        ChatError(fatal=True, code=None, message="mid-stream 429"),
    ])
    good = _FakeLLM(events=[
        TextDelta(text="partial", index=0),
        TextDelta(text=" full", index=0),
        Done(stop_reason="stop", raw_reason="stop"),
    ])
    cfg = AggregatedLLMConfig(
        failover_point=FailoverPoint.MID_STREAM,
        members=[
            AggregatedMember(provider_id="bad", model_name="m1"),
            AggregatedMember(provider_id="good", model_name="m2"),
        ],
    )
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    events = await _drain(agg.stream(model="virtual-1", messages=_MSG))
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    # "partial" appears twice (member[0] then member[1] restart) -> duplication.
    assert texts == ["partial", "partial", " full"]
    assert isinstance(events[-1], Done)
    # The failed member's Error never reached the subscriber.
    assert not any(isinstance(e, ChatError) for e in events)


@pytest.mark.asyncio
async def test_before_first_token_does_not_restart_on_mid_stream_error():
    # Same script, default BEFORE_FIRST_TOKEN: no restart, error surfaced.
    bad = _FakeLLM(events=[
        TextDelta(text="partial", index=0),
        ChatError(fatal=True, code=None, message="mid-stream 429"),
    ])
    good = _FakeLLM(events=[TextDelta(text="unused", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(
        failover_point=FailoverPoint.BEFORE_FIRST_TOKEN,
        members=[
            AggregatedMember(provider_id="bad", model_name="m1"),
            AggregatedMember(provider_id="good", model_name="m2"),
        ],
    )
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    events = await _drain(agg.stream(model="virtual-1", messages=_MSG))
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["partial"]
    assert isinstance(events[-1], ChatError)


@pytest.mark.asyncio
async def test_mid_stream_raised_timeout_restarts_under_mid_stream():
    # Mid-stream timeouts are RAISED, not yielded (openchat.py:284,
    # anthropic.py:760). Under MID_STREAM an eligible raised exception after
    # commit restarts on the next member.
    bad = _FakeLLM(
        events=[StreamStart(model="m1"), TextDelta(text="partial", index=0)],
        mid_exc=ProviderTimeoutError("stalled", code="stream_timeout"),
    )
    good = _FakeLLM(events=[TextDelta(text="served", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(
        failover_point=FailoverPoint.MID_STREAM,
        members=[
            AggregatedMember(provider_id="bad", model_name="m1"),
            AggregatedMember(provider_id="good", model_name="m2"),
        ],
    )
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    events = await _drain(agg.stream(model="virtual-1", messages=_MSG))
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["partial", "served"]      # restarted after the raised timeout
    assert isinstance(events[-1], Done)


@pytest.mark.asyncio
async def test_before_first_token_propagates_raised_mid_stream_timeout():
    # In the default mode a post-commit RAISED failure cannot fail over
    # (would re-emit) -> it propagates.
    from primer.model.except_ import ProviderTimeoutError as _PTE
    bad = _FakeLLM(
        events=[StreamStart(model="m1"), TextDelta(text="partial", index=0)],
        mid_exc=_PTE("stalled", code="stream_timeout"),
    )
    good = _FakeLLM(events=[Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(
        failover_point=FailoverPoint.BEFORE_FIRST_TOKEN,
        members=[
            AggregatedMember(provider_id="bad", model_name="m1"),
            AggregatedMember(provider_id="good", model_name="m2"),
        ],
    )
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    with pytest.raises(_PTE):
        await _drain(agg.stream(model="virtual-1", messages=_MSG))


@pytest.mark.asyncio
async def test_mid_stream_failover_closes_abandoned_downstream():
    # The failed-over member's generator must be aclosed (rate-limiter slot).
    bad = _FakeLLM(events=[
        TextDelta(text="partial", index=0),
        ChatError(fatal=True, code=None, message="mid-stream 429"),
        TextDelta(text="never", index=0),
    ])
    good = _FakeLLM(events=[Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(
        failover_point=FailoverPoint.MID_STREAM,
        members=[
            AggregatedMember(provider_id="bad", model_name="m1"),
            AggregatedMember(provider_id="good", model_name="m2"),
        ],
    )
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    await _drain(agg.stream(model="virtual-1", messages=_MSG))
    assert bad.stream_closed is True


@pytest.mark.asyncio
async def test_mid_stream_non_eligible_yielded_error_surfaces_no_restart():
    # A non-null, non-timeout code (e.g. bad-request) is config-eligible only,
    # so under FailoverClasses.TRANSIENT it is NOT eligible even in MID_STREAM
    # mode -> the Error surfaces to the subscriber; member[1] never runs.
    bad = _FakeLLM(events=[
        TextDelta(text="partial", index=0),
        ChatError(fatal=True, code="invalid_request_error", message="bad request mid-stream"),
    ])
    good = _FakeLLM(events=[TextDelta(text="SHOULD-NOT-APPEAR", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(
        failover_point=FailoverPoint.MID_STREAM,
        failover_on=FailoverClasses.TRANSIENT,
        members=[
            AggregatedMember(provider_id="bad", model_name="m1"),
            AggregatedMember(provider_id="good", model_name="m2"),
        ],
    )
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    events = await _drain(agg.stream(model="virtual-1", messages=_MSG))
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["partial"]                # no restart, no duplication
    assert isinstance(events[-1], ChatError)    # the error surfaced, not raised


@pytest.mark.asyncio
async def test_mid_stream_non_eligible_raised_error_propagates_no_restart():
    # AuthenticationError is config-eligible only; under FailoverClasses.
    # TRANSIENT it is NOT eligible even in MID_STREAM mode -> the raised
    # exception propagates instead of restarting on member[1].
    bad = _FakeLLM(
        events=[StreamStart(model="m1"), TextDelta(text="partial", index=0)],
        mid_exc=AuthenticationError("401 mid-stream"),
    )
    good = _FakeLLM(events=[TextDelta(text="SHOULD-NOT-APPEAR", index=0),
                            Done(stop_reason="stop", raw_reason="stop")])
    cfg = AggregatedLLMConfig(
        failover_point=FailoverPoint.MID_STREAM,
        failover_on=FailoverClasses.TRANSIENT,
        members=[
            AggregatedMember(provider_id="bad", model_name="m1"),
            AggregatedMember(provider_id="good", model_name="m2"),
        ],
    )
    agg = AggregatedLLM(_row(cfg), resolve_member=_resolver({"bad": bad, "good": good}))
    with pytest.raises(AuthenticationError):
        await _drain(agg.stream(model="virtual-1", messages=_MSG))
