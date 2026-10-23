"""G6-11: an event filter's regex fields and its rego expr are bounded in time.

Both tiers run in the shared, leader-elected dispatcher. A ``regex`` field matcher ran under stdlib ``re`` with no bound, and
an ``expr`` ran rego-cpp's backtracking ``regex.match`` (about x10 per 4 more characters) on the dispatcher's own thread.
Now a field search has the 10 ms bound of the channel text_pattern, and the dispatcher waits at most 0.5 s for an expr
evaluated on a worker thread; while an evaluation that ran past the bound is still going, the same expr is not run again.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

import primer.events.filters as filters_module
from primer.events.filters import evaluate_filter_expr, matches
from primer.model.event import Event, EventFilter, FieldMatcher

HOSTILE_FIELD = r"(a|aa)+$"  # about 5 s under stdlib re on the value below
HOSTILE_VALUE = "a" * 36 + "!"
# rego-cpp's regex.match backtracks: about 3 s for this input with nothing bounding it.
SLOW_EXPR = 'package primer.event_filter\nmatch := regex.match("(a+)+$", input.payload.s)\n'
SLOW_INPUT = "a" * 27 + "!"
QUICK_EXPR = 'package primer.event_filter\nmatch := input.payload.s == "kb"\n'


def _event(**payload) -> Event:
    return Event(
        id=1, event_type="collection.document_pushed", occurred_at=datetime.now(timezone.utc),
        actor="system", payload=payload,
    )


async def _until_no_evaluation_runs_past_the_bound() -> None:
    """A timed-out rego evaluation keeps the worker until it ends; let it end so the next test starts clean."""
    for _ in range(600):
        if not getattr(filters_module, "_RUNNING_PAST_BOUND", set()):
            return
        await asyncio.sleep(0.1)
    raise AssertionError("a timed-out evaluation is still running after 60 s")


async def _watch(body):
    gaps = [0.0]
    state = {"stop": False}

    async def ticker() -> None:
        last = time.perf_counter()
        while not state["stop"]:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    started = time.perf_counter()
    try:
        result = await body()
    finally:
        elapsed = time.perf_counter() - started
        state["stop"] = True
        await task
    return max(gaps), result, elapsed


# ---- regex fields -------------------------------------------------------------------------------------------------------


def test_a_catastrophic_field_regex_is_no_match_within_the_bound():
    flt = EventFilter(fields=[FieldMatcher(path="payload.s", op="regex", value=HOSTILE_FIELD)])
    started = time.perf_counter()
    assert not matches(_event(s=HOSTILE_VALUE), flt)
    assert time.perf_counter() - started < 0.5
    assert matches(_event(s="aaaa"), flt)


def test_field_regexes_use_the_regex_engine_dialect():
    flt = EventFilter(fields=[FieldMatcher(path="payload.s", op="regex", value=r"^\p{Lu}")])
    assert matches(_event(s="Kb"), flt)
    assert not matches(_event(s="kb"), flt)


def test_the_save_time_check_caps_a_field_regex_at_1000_characters():
    from primer.events.filters import validate_filter

    validate_filter(EventFilter(fields=[FieldMatcher(path="payload.s", op="regex", value="a" * 1000)]))
    with pytest.raises(ValueError, match="1001 characters long; the limit is 1000"):
        validate_filter(EventFilter(fields=[FieldMatcher(path="payload.s", op="regex", value="a" * 1001)]))
    with pytest.raises(ValueError, match="invalid regex"):
        validate_filter(EventFilter(fields=[FieldMatcher(path="payload.s", op="regex", value="(unclosed")]))
    with pytest.raises(ValueError, match="template strings"):
        validate_filter(EventFilter(expr='package primer.event_filter\nmatch := $"{input.x}" == ""\n'))


# ---- expr ---------------------------------------------------------------------------------------------------------------


async def test_a_slow_expr_is_no_match_within_the_bound():
    started = time.perf_counter()
    assert evaluate_filter_expr(SLOW_EXPR, {"payload": {"s": SLOW_INPUT}}) is False
    assert time.perf_counter() - started < 1.0
    # While that evaluation runs on, the same expr answers at once without starting another.
    started = time.perf_counter()
    assert evaluate_filter_expr(SLOW_EXPR, {"payload": {"s": SLOW_INPUT}}) is False
    assert time.perf_counter() - started < 0.1
    await _until_no_evaluation_runs_past_the_bound()
    # Once it ended, other exprs evaluate normally.
    assert evaluate_filter_expr(QUICK_EXPR, {"payload": {"s": "kb"}}) is True


async def test_the_async_form_never_blocks_the_loop():
    from primer.events.filters import matches_async

    flt = EventFilter(expr=SLOW_EXPR)
    gap, result, elapsed = await _watch(lambda: matches_async(_event(s=SLOW_INPUT), flt))
    assert result is False
    assert elapsed < 1.0
    assert gap < 0.2, gap
    await _until_no_evaluation_runs_past_the_bound()
    assert await matches_async(_event(s="kb"), EventFilter(expr=QUICK_EXPR)) is True


async def test_the_dispatcher_awaits_a_slow_expr_off_its_loop(tmp_path):
    from primer.events.dispatcher import EventDispatcher
    from primer.model.event import EventSubscription, LogSink
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await sp.initialize()
    try:
        await sp.get_event_store().ensure_schema()
        await sp.get_storage(EventSubscription).create(EventSubscription(
            id="sub-slow", description="t", filter=EventFilter(expr=SLOW_EXPR), sink=LogSink(),
        ))
        dispatcher = EventDispatcher(storage_provider=sp)
        await dispatcher.drain_once()  # pin the cursor at head
        await sp.get_event_store().append(event_type="agent.created", payload={"s": SLOW_INPUT})
        gap, delivered, elapsed = await _watch(dispatcher.drain_once)
        assert delivered == 0
        assert elapsed < 1.5
        assert gap < 0.2, gap
    finally:
        await _until_no_evaluation_runs_past_the_bound()
        await sp.aclose()
