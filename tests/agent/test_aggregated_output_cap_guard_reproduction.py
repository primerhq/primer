"""The output-cap guard once read the MIN member window of an aggregated profile, which was wrong under some
failover policies; these regressions pin the per-member fix for 01a10c6b item 1.

``_recover_from_overflow`` refuses to compact when ``output_cap_never_fits(max_output_tokens, context_length)``: an
output cap that alone is not below the window means no history fits beside it, so a compaction could only rewrite the
persisted history into a summary and the replay would be rejected the same way. For an aggregated profile
``resolve_model`` reports ``context_length = min(member windows)``, and that is the number the guard compares the cap
with. Whether the min is the right window depends on which member's rejection the executor is looking at, and that
depends on the pool's failover policy (``AggregatedLLM.stream``):

* ``TRANSIENT_AND_CONFIG``: a member's prompt-overflow 400 is eligible, so the pool goes on to the next member and the
  executor sees a rejection only when EVERY member rejected. With ``min < cap < max`` the larger member rejected
  because the HISTORY did not fit beside the cap in ITS window, so a compaction can fix it. The guard vetoes it. WRONG.
* strict ``TRANSIENT``: a 400 is not eligible, so the FIRST member's rejection is raised at once and the rest are never
  tried. The right window is that first member's. If it is the small one the veto is right; if it is a larger one the
  veto is wrong.

Per wrong case there is a plain scenario test (the harness really created the situation: which members were asked, that
the oversized history fits none, and that a compacted prompt WOULD have been answered) and a behaviour test of what a
correct fix must do. Two plain pins hold the cases where the veto is right today and must stay so.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from primer.common.context_overflow import is_context_overflow
from primer.llm.aggregated import AggregatedLLM
from primer.model.chat import Done, Error as ChatError, Message, StreamEvent, StreamStart, TextDelta, TextPart
from primer.model.except_ import BadRequestError, ContextOverflowUnrecoverable
from primer.model.model_profile import FailoverClasses, ModelProfile, ModelProfileConfig, RoutingStrategy
from primer.model_profile import ResolvedModel, resolve_model

from tests.agent.test_overflow_recovery import _Executor, _SpyCompaction

SMALL, LARGE = 8_192, 32_768
CAP = 10_000  # above the small window, below the large one: min < cap < max
CAP_ABOVE_EVERY_WINDOW = 40_000
HISTORY_WORDS = 24_000  # does not fit beside CAP in the large window (24_000 + 10_000 > 32_768); a summary does
OVERFLOW_TEXT = "This model's maximum context length is 32768 tokens; the prompt contains too many tokens."


def _words(messages: list[Message]) -> int:
    return sum(len(p.text.split()) for m in messages for p in m.parts if isinstance(p, TextPart))


@dataclass
class _Member:
    """A provider that rejects a request whose prompt plus output cap exceeds its window, by provider code before any
    event (the connect phase), the way an OpenAI-compatible server does."""

    window: int
    asked: list[tuple[int, int | None]] = field(default_factory=list)  # (prompt words, output cap) per call
    rejection: BadRequestError | None = None
    failure_channel: str = "raise-connect"

    async def stream(self, *, messages: list[Message], max_output_tokens: int | None = None, **_kw) -> AsyncIterator[StreamEvent]:
        prompt = _words(messages)
        self.asked.append((prompt, max_output_tokens))
        if prompt + (max_output_tokens or 0) > self.window:
            if self.failure_channel == "yield-first":
                yield ChatError(code="bad_request", message=OVERFLOW_TEXT, fatal=True)
                return
            if self.failure_channel == "yield-after-start":
                yield StreamStart(model=f"window-{self.window}")
                yield ChatError(code="bad_request", message=OVERFLOW_TEXT, fatal=True)
                return
            if self.failure_channel == "raise-after-start":
                yield StreamStart(model=f"window-{self.window}")
            if self.rejection is not None:
                raise self.rejection
            raise BadRequestError("Something went wrong", code="context_length_exceeded", status_code=400)
        yield TextDelta(text="all good", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


class _Profiles:
    def __init__(self, rows: dict[str, ModelProfile]) -> None:
        self.rows = rows

    async def get(self, id: str):
        return self.rows.get(id)

    def get_storage(self, _model_class):
        return self


@dataclass
class _Pool:
    """A real ``AggregatedLLM`` over two scripted members, and the model the executor would resolve for it."""

    llm: AggregatedLLM
    model: ResolvedModel
    members: dict[str, _Member]


async def _pool(
    order: list[str],
    *,
    failover_on: FailoverClasses,
    strategy: RoutingStrategy = RoutingStrategy.SEQUENTIAL,
) -> _Pool:
    members = {"small": _Member(SMALL), "large": _Member(LARGE)}
    rows = {
        name: ModelProfile(
            id=name, description=name, provider_id="prov", model_name=f"{name}-model", context_length=m.window,
        )
        for name, m in members.items()
    }
    rows["agg"] = ModelProfile(
        id="agg", description="agg", kind="aggregated", members=order, strategy=strategy,
        failover_on=failover_on,
    )

    async def resolve_member(member_id: str):
        return members[member_id], ResolvedModel(
            profile_id=member_id, provider_id="prov", model_name=f"{member_id}-model",
            context_length=members[member_id].window, config=ModelProfileConfig(),
        )

    model = await resolve_model(_Profiles(rows), default_profile_id="agg")  # type: ignore[arg-type]
    return _Pool(AggregatedLLM(rows["agg"], resolve_member=resolve_member), model, members)


class _PooledExecutor(_Executor):
    """The overflow-recovery harness's executor, over the pool and the model the production resolver reported for it."""

    def __init__(self, pool: _Pool, compaction: _SpyCompaction, *, max_output_tokens: int) -> None:
        super().__init__(pool.llm, compaction, max_output_tokens=max_output_tokens)
        self._model = pool.model
        self.history = [
            Message(role="user", parts=[TextPart(text="an earlier question")]),
            Message(role="assistant", parts=[TextPart(text="word " * HISTORY_WORDS)]),
        ]


@dataclass
class _Run:
    events: list[StreamEvent]
    unrecoverable: ContextOverflowUnrecoverable | None
    spy: _SpyCompaction
    executor: _PooledExecutor


async def _run(pool: _Pool, *, cap: int) -> _Run:
    spy = _SpyCompaction()
    executor = _PooledExecutor(pool, spy, max_output_tokens=cap)
    events: list[StreamEvent] = []
    try:
        async for ev in executor.invoke([Message(role="user", parts=[TextPart(text="go")])]):
            events.append(ev)
    except ContextOverflowUnrecoverable as exc:  # anything else is a harness error and must stay one
        return _Run(events, exc, spy, executor)
    return _Run(events, None, spy, executor)


async def _ask(pool: _Pool, *, words: int, cap: int = CAP) -> list[StreamEvent]:
    """One call straight at the pool: a prompt of ``words`` words and the cap the executor would send."""
    prompt = [Message(role="user", parts=[TextPart(text="word " * words)])]
    return [ev async for ev in pool.llm.stream(model="agg", messages=prompt, max_output_tokens=cap)]


def _compaction_and_replay_problems(run: _Run) -> list[str]:
    """What any correct fix must do here, whichever way it is written: the compaction runs, the persisted head is
    rewritten once, and the replay is answered by the member that can take it."""
    problems = []
    if run.unrecoverable is not None:
        problems.append(f"the turn failed with ContextOverflowUnrecoverable: {run.unrecoverable.message}")
    if run.spy.forced != 1:
        problems.append(f"force_compact ran {run.spy.forced} times, expected 1")
    if len(run.executor.replaced) != 1:
        problems.append(f"the persisted head was rewritten {len(run.executor.replaced)} times, expected 1")
    if "all good" not in "".join(e.text for e in run.events if isinstance(e, TextDelta)):
        problems.append("the replay was never answered")
    return problems


# --- TRANSIENT_AND_CONFIG: every member rejected, the larger one because of the HISTORY ----------------------


async def test_scenario_every_member_rejected_the_history_and_a_summary_would_have_fit() -> None:
    pool = await _pool(["small", "large"], failover_on=FailoverClasses.TRANSIENT_AND_CONFIG)
    assert pool.model.context_length == SMALL, "the resolver reports the MIN member window for an aggregated profile"

    with pytest.raises(BadRequestError) as rejected:
        await _ask(pool, words=HISTORY_WORDS)

    assert is_context_overflow(rejected.value), "the pool re-raises the last member's own overflow rejection"
    assert pool.llm.rejecting_context_length(rejected.value) == LARGE
    assert [len(m.asked) for m in pool.members.values()] == [1, 1], "both members were tried"
    # The larger member rejected because the history does not fit beside the cap: a summary does.
    assert HISTORY_WORDS + CAP > LARGE and 40 + CAP <= LARGE
    events = await _ask(pool, words=40)
    assert "all good" in "".join(e.text for e in events if isinstance(e, TextDelta))


async def test_every_member_rejecting_the_history_still_compacts_when_the_cap_fits_the_larger_window() -> None:
    pool = await _pool(["small", "large"], failover_on=FailoverClasses.TRANSIENT_AND_CONFIG)

    run = await _run(pool, cap=CAP)

    problems = _compaction_and_replay_problems(run)
    assert not problems, "\n".join(problems)


# --- strict TRANSIENT: the first member's rejection is the executor's, and it may be the LARGER one ------------


async def test_scenario_the_first_member_rejected_alone_and_a_summary_would_have_fit_it() -> None:
    pool = await _pool(["large", "small"], failover_on=FailoverClasses.TRANSIENT)

    with pytest.raises(BadRequestError) as rejected:
        await _ask(pool, words=HISTORY_WORDS)

    assert is_context_overflow(rejected.value)
    assert pool.llm.rejecting_context_length(rejected.value) == LARGE
    assert [len(m.asked) for m in pool.members.values()] == [0, 1], "only the first member (large) was asked"
    events = await _ask(pool, words=40)
    assert "all good" in "".join(e.text for e in events if isinstance(e, TextDelta))
    assert [len(m.asked) for m in pool.members.values()] == [0, 2], "the replay goes to the same first member"


async def test_a_larger_first_member_rejecting_the_history_still_compacts() -> None:
    pool = await _pool(["large", "small"], failover_on=FailoverClasses.TRANSIENT)

    run = await _run(pool, cap=CAP)

    problems = _compaction_and_replay_problems(run)
    assert not problems, "\n".join(problems)


async def test_a_larger_first_member_yielding_the_overflow_still_compacts() -> None:
    """The surfaced Error crosses TurnStreamOverflow reconstruction without losing its member window."""
    pool = await _pool(["large", "small"], failover_on=FailoverClasses.TRANSIENT)
    pool.members["large"].failure_channel = "yield-first"

    run = await _run(pool, cap=CAP)

    problems = _compaction_and_replay_problems(run)
    assert not problems, "\n".join(problems)
    assert [len(m.asked) for m in pool.members.values()] == [0, 2]


async def test_a_larger_member_raising_after_stream_start_still_compacts() -> None:
    """A non-content commit followed by a raised overflow keeps the current member window."""
    pool = await _pool(["large", "small"], failover_on=FailoverClasses.TRANSIENT)
    pool.members["large"].failure_channel = "raise-after-start"

    run = await _run(pool, cap=CAP)

    problems = _compaction_and_replay_problems(run)
    assert not problems, "\n".join(problems)
    assert [len(m.asked) for m in pool.members.values()] == [0, 2]


async def test_a_larger_member_yielding_after_stream_start_still_compacts() -> None:
    """A non-content commit followed by a yielded overflow keeps the Error's member window."""
    pool = await _pool(["large", "small"], failover_on=FailoverClasses.TRANSIENT)
    pool.members["large"].failure_channel = "yield-after-start"

    run = await _run(pool, cap=CAP)

    problems = _compaction_and_replay_problems(run)
    assert not problems, "\n".join(problems)
    assert [len(m.asked) for m in pool.members.values()] == [0, 2]


# --- the cases where the veto is right today, and must stay right under any fix --------------------------------


async def test_a_small_first_member_under_strict_transient_still_vetoes_the_compaction() -> None:
    """The first member's window is the small one and the cap does not fit it: the replay would go to that same
    member and be rejected the same way, after the persisted history was rewritten into a summary."""
    pool = await _pool(["small", "large"], failover_on=FailoverClasses.TRANSIENT)

    run = await _run(pool, cap=CAP)

    assert run.unrecoverable is not None, "the turn must fail with ContextOverflowUnrecoverable"
    assert (run.unrecoverable.forced_compaction, run.unrecoverable.replay_attempted) == (False, False)
    assert run.spy.forced == 0 and run.executor.replaced == [], "the persisted history must be left alone"
    assert [len(m.asked) for m in pool.members.values()] == [1, 0], "the first member rejected, the pool stopped"


async def test_a_cap_above_every_member_window_still_vetoes_the_compaction() -> None:
    """No member can ever take the call: compaction cannot help under either policy, so a fix must not stop vetoing."""
    pool = await _pool(["small", "large"], failover_on=FailoverClasses.TRANSIENT_AND_CONFIG)

    run = await _run(pool, cap=CAP_ABOVE_EVERY_WINDOW)

    assert run.unrecoverable is not None, "the turn must fail with ContextOverflowUnrecoverable"
    assert (run.unrecoverable.forced_compaction, run.unrecoverable.replay_attempted) == (False, False)
    assert run.spy.forced == 0 and run.executor.replaced == [], "the persisted history must be left alone"
    assert [len(m.asked) for m in pool.members.values()] == [1, 1], "every member was tried once and rejected"


async def test_concurrent_rejections_keep_each_calls_exact_member_window() -> None:
    """Even a reused exception object gets the right per-Task round-robin member window."""
    pool = await _pool(
        ["small", "large"],
        failover_on=FailoverClasses.TRANSIENT,
        strategy=RoutingStrategy.ROUND_ROBIN,
    )
    shared = BadRequestError("shared overflow", code="context_length_exceeded", status_code=400)
    for member in pool.members.values():
        member.rejection = shared

    async def reject() -> tuple[BadRequestError, int | None]:
        try:
            await _ask(pool, words=HISTORY_WORDS)
        except BadRequestError as exc:
            return exc, pool.llm.rejecting_context_length(exc)
        raise AssertionError("the oversized call unexpectedly succeeded")

    rejected = await asyncio.gather(reject(), reject())

    assert all(exc is shared for exc, _window in rejected)
    assert sorted(window for _exc, window in rejected if window is not None) == [SMALL, LARGE]
    assert [len(m.asked) for m in pool.members.values()] == [1, 1]


async def test_next_call_clears_stale_rejection_provenance() -> None:
    pool = await _pool(["small", "large"], failover_on=FailoverClasses.TRANSIENT)

    with pytest.raises(BadRequestError) as rejected:
        await _ask(pool, words=HISTORY_WORDS)
    assert pool.llm.rejecting_context_length(rejected.value) == SMALL

    await _ask(pool, words=40, cap=1_000)
    assert pool.llm.rejecting_context_length(rejected.value) is None
