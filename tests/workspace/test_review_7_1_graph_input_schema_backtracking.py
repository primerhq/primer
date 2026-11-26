"""Review 2026-11-23 finding 7.1: a graph Begin ``input_schema`` regex must not
run unbounded on the event loop at session create.

``start_workspace_session`` pre-validates ``graph_input`` against the Begin
node's author-supplied ``input_schema``. That validation now runs bounded and
OFF the event loop (``primer.common.bounded_jsonschema.validate_bounded_off_loop``,
the same validator a node's ``output_schema`` uses, in a worker thread whose
``regex`` engine releases the GIL while it matches). A pattern with unbounded
backtracking (``^(a|aa)+$``) can no longer hold this replica's loop -- every
request, SSE tap and lease heartbeat on it -- for minutes; a schema that
cannot be checked in time is refused with a clear 4xx, not a hang.

Round 1 (reviewer): the stall input is 40 a's plus a 'b'. With it, the stdlib
``re`` engine (what the unfixed code ran, on the loop) takes about 12 s here
to give up (36 a's: about 2 s, 44 a's: about 88 s), while the bounded
validator's ``regex`` engine is stopped at the 1 s matching budget, so both
directions are decisive on any runner: unfixed code stalls the loop for 12 s
or more (red), the fixed code answers its timeout 422 within about 1 s with
the loop free (green). The round-0 input (32 a's) was fragile both ways: the
``regex`` engine took 0.62 s against the 1 s budget (a slow CI runner would
make the fixed code time out where the old test expected the plain refusal)
and the stdlib took 0.25 s against the 0.2 s threshold (a fast runner would
pass unfixed code). The message-shape test therefore uses a short input the
engine settles instantly, and the timeout and unusable-pattern 422 mappings
each get their own test.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from primer.model.access import Access
from primer.model.except_ import ValidationError
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.workspace_session import (
    GraphSessionBinding,
    WorkspaceSession,
)
from primer.workspace.session_factory import start_workspace_session

# Reuse the established session-factory harness (fakes + seeders).
from tests.workspace.test_session_factory import (
    _FakeClaimEngine,
    _FakeLiveWorkspace,
    _FakeScheduler,
    _full_deps,
    _seed_workspace,
)

# 40 a's + a 'b': the stdlib `re` engine (the unfixed code ran it on the
# loop) takes about 12.8 s here to give up, while the bounded validator's
# `regex` engine is stopped at the 1 s matching budget, so the fixed code
# answers in about 1 s with the loop free. 32 a's -- the round-0 input --
# was fragile both ways: the `regex` engine took 0.62 s against the 1 s
# budget (a slow CI runner would time out where a plain refusal was
# expected) and the stdlib took 0.25 s against the 0.2 s threshold (a fast
# runner would pass unfixed code).
_STALL_INPUT = {"q": "a" * 40 + "b"}

# A short non-matching input: the engine settles it instantly on any
# runner, so the plain refusal message is stable there.
_QUICK_INPUT = {"q": "a" * 6 + "b"}


def _begin_graph(pattern: str, graph_id: str = "gr-re") -> Graph:
    return Graph(
        access=Access(public=True),
        id=graph_id,
        description=f"Begin -> agent -> End with pattern {pattern!r}",
        nodes=[
            _BeginNode(
                id="b",
                input_schema={
                    "type": "object",
                    "properties": {"q": {"type": "string", "pattern": pattern}},
                },
            ),
            _AgentNodeRef(id="a", agent_id="ag-1"),
            _EndNode(id="e"),
        ],
        edges=[
            _StaticEdge(from_node="b", to_node="a"),
            _StaticEdge(from_node="a", to_node="e"),
        ],
    )


def _create(deps, graph: Graph, graph_input):
    return start_workspace_session(
        workspace_id="ws-1",
        binding=GraphSessionBinding(graph_id=graph.id),
        initial_instructions=None,
        graph_input=graph_input,
        auto_start=False,
        metadata={},
        parent_session_id=None,
        deps=deps,
    )


async def _max_loop_gap(create) -> tuple[float, str, str]:
    """Run ``create`` alongside a 10 ms heartbeat and return the largest
    gap between the heartbeat's ticks (how long the event loop was
    stalled), the outcome, and the refusal message ("" when accepted).

    A validation that holds the loop (GIL) for N seconds makes the
    heartbeat skip N seconds, so the largest gap is that stall. A
    validation that runs off the loop (and releases the GIL) leaves every
    gap near the 10 ms tick. The heartbeat gets a head start BEFORE the
    create call: if the stall swallows the heartbeat's first wake (the
    whole loop is blocked), the gap list would be empty and the stall
    invisible.
    """
    gaps: list[float] = []
    message = ""
    last = [time.monotonic()]

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last[0])
            last[0] = now

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    try:
        try:
            await create
            outcome = "accepted"
        except ValidationError as exc:
            outcome = "refused"
            message = str(exc)
    finally:
        # One more tick BEFORE the cancel: the stall's gap is the LAST
        # one (the first tick after the loop unblocks), and the cancel
        # would otherwise eat it.
        await asyncio.sleep(0.03)
        hb.cancel()
    return (max(gaps) if gaps else 0.0), outcome, message


@pytest.mark.asyncio
async def test_backtracking_input_schema_does_not_stall_the_event_loop(
    fake_storage_provider,
):
    """A backtracking pattern against the 40-a input must not hold the
    loop: a heartbeat ticking every 10 ms alongside the create call sees
    no gap anywhere near the backtracking time. With 40 a's the fixed
    code's own refusal is the timeout 422 (the engine needs past the 1 s
    budget for this input), and the loop stays free while it arrives. On
    the unfixed code (stdlib `re` on the loop) the gap is the whole
    12 s-or-more validation time."""
    await _seed_workspace(fake_storage_provider)
    graph = _begin_graph("^(a|aa)+$")
    await fake_storage_provider.get_storage(Graph).create(graph)
    live = _FakeLiveWorkspace()
    deps = _full_deps(
        fake_storage_provider, _FakeScheduler(), _FakeClaimEngine(), live
    )

    gap, outcome, message = await _max_loop_gap(
        _create(deps, graph, _STALL_INPUT)
    )

    assert outcome == "refused", (
        f"the 40-a input never matches ^(a|aa)+$, so the create must "
        f"refuse (gap {gap * 1000:.0f} ms, message: {message!r})"
    )
    assert gap < 0.2, (
        f"the event loop stalled {gap * 1000:.0f} ms while validating "
        f"graph_input (outcome: {outcome}): the Begin node's input_schema "
        "pattern ran unbounded on the event loop"
    )
    # 40 a's run past the 1 s matching budget on the bounded engine: the
    # refusal is the timeout 422, which names the spent matching budget.
    assert "matching budget" in message, (
        f"the 40-a input exceeds the matching budget, so the refusal "
        f"must be the timeout 422, not a plain refusal: {message!r}"
    )


@pytest.mark.asyncio
async def test_backtracking_input_is_refused_not_hung(
    fake_storage_provider,
):
    """The message-shape test, on a SHORT input: the engine settles it
    instantly, so the usual plain 4xx message is stable on any runner
    (the round-0 test ran this assertion on the 32-a input, whose engine
    time crossed the 1 s budget on a slow CI runner and would have
    produced the timeout message instead)."""
    await _seed_workspace(fake_storage_provider)
    graph = _begin_graph("^(a|aa)+$")
    await fake_storage_provider.get_storage(Graph).create(graph)
    live = _FakeLiveWorkspace()
    deps = _full_deps(
        fake_storage_provider, _FakeScheduler(), _FakeClaimEngine(), live
    )

    t0 = time.monotonic()
    with pytest.raises(ValidationError) as excinfo:
        await _create(deps, graph, _QUICK_INPUT)
    elapsed = time.monotonic() - t0

    assert "graph_input invalid at path ['q']" in str(excinfo.value)
    assert elapsed < 2.0, (
        f"the refusal took {elapsed:.1f} s: the pattern ran unbounded"
    )
    # Refused at create: no slot allocated.
    assert live.started == []


@pytest.mark.asyncio
async def test_the_timeout_422_names_the_matching_budget(
    fake_storage_provider,
):
    """The timeout 422 mapping (session_factory's UserRegexTimeout arm):
    a pattern that runs past the 1 s matching budget is refused with the
    message naming the spent matching budget, and the refusal arrives in
    budget time, not after the backtracking."""
    await _seed_workspace(fake_storage_provider)
    graph = _begin_graph("^(a|aa)+$")
    await fake_storage_provider.get_storage(Graph).create(graph)
    live = _FakeLiveWorkspace()
    deps = _full_deps(
        fake_storage_provider, _FakeScheduler(), _FakeClaimEngine(), live
    )

    t0 = time.monotonic()
    with pytest.raises(ValidationError) as excinfo:
        await _create(deps, graph, _STALL_INPUT)
    elapsed = time.monotonic() - t0

    msg = str(excinfo.value)
    assert "graph_input cannot be validated in time" in msg, msg
    assert "matching budget" in msg, msg
    assert elapsed < 5.0, (
        f"the timeout 422 took {elapsed:.1f} s: the pattern ran "
        "unbounded instead of being stopped at the budget"
    )
    # Refused at create: no slot allocated.
    assert live.started == []


@pytest.mark.asyncio
async def test_the_unusable_pattern_422_names_the_input_schema(
    fake_storage_provider,
):
    """The unusable-pattern 422 mapping (session_factory's UserRegexError
    arm): a pattern whose counted repeats unroll past the engine's
    repeat cap is refused at create, and the message names the
    input_schema -- the label session_factory passes to the bounded
    validator -- not the output_schema default the validator's existing
    callers keep."""
    await _seed_workspace(fake_storage_provider)
    # (a{100}){200} unrolls to 20,000 counted steps: past the 10,000
    # repeat cap, decided at compile time, so no input value matters.
    graph = _begin_graph("(a{100}){200}")
    await fake_storage_provider.get_storage(Graph).create(graph)
    live = _FakeLiveWorkspace()
    deps = _full_deps(
        fake_storage_provider, _FakeScheduler(), _FakeClaimEngine(), live
    )

    with pytest.raises(ValidationError) as excinfo:
        await _create(deps, graph, {"q": "a"})

    msg = str(excinfo.value)
    assert "graph input_schema has a pattern that cannot be used" in msg, msg
    assert "input_schema repeats too much to compile" in msg, msg
    assert "output_schema" not in msg, msg
    # Refused at create: no slot allocated.
    assert live.started == []


@pytest.mark.asyncio
async def test_normal_pattern_with_valid_input_still_creates_the_session(
    fake_storage_provider,
):
    """Benign: a normal pattern with a matching input still starts the
    session (the bounded validator must not over-refuse)."""
    await _seed_workspace(fake_storage_provider)
    graph = _begin_graph("^[a-z]+$")
    await fake_storage_provider.get_storage(Graph).create(graph)
    live = _FakeLiveWorkspace()
    deps = _full_deps(
        fake_storage_provider, _FakeScheduler(), _FakeClaimEngine(), live
    )

    sess = await _create(deps, graph, {"q": "hello"})

    assert isinstance(sess, WorkspaceSession)
    assert isinstance(sess.binding, GraphSessionBinding)
    assert len(live.started) == 1


@pytest.mark.asyncio
async def test_normal_pattern_with_invalid_input_is_refused_as_before(
    fake_storage_provider,
):
    """Benign: an invalid input against a normal pattern is refused with
    the same error as before the fix (same message shape)."""
    await _seed_workspace(fake_storage_provider)
    graph = _begin_graph("^[a-z]+$")
    await fake_storage_provider.get_storage(Graph).create(graph)
    live = _FakeLiveWorkspace()
    deps = _full_deps(
        fake_storage_provider, _FakeScheduler(), _FakeClaimEngine(), live
    )

    with pytest.raises(ValidationError) as excinfo:
        await _create(deps, graph, {"q": "UPPER-1"})

    assert "graph_input invalid at path ['q']" in str(excinfo.value)
    assert live.started == []
