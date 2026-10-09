"""Round 2 of the machine-wake fence (#702 security review, tickets 01a1208d and 01a12151-b225).

* B1: ``trigger:{trigger_id}`` carried no session. The park stamp fences a SINGLE park, but a GRAPH park is exempt from it, so session A's fire still woke graph
  session B parked on the same trigger with A's payload, past B's own subscription checks (the A-20 rank guard, a channel subscription's matcher). The key is now
  ``trigger:{session_id}:{trigger_id}``; the dispatcher publishes the key the park stored, so a park in flight keeps its own.
* B2: three producers read the park and published an UNSTAMPED wake: the cancel of a non-gate yield, the steer route's cancel of pending external calls, and the
  ``wait_for_event`` sink (tests/api/test_yields.py, tests/api/test_external_tools_steer.py, tests/events/test_dispatcher.py). A cancel is judged by the stamp and
  stays exempt from the deadline rule.
* Pins: the 5 s window, a timeout marker or timer fire to a GRAPH park, ``is_timeout_payload`` by key presence (as the classifier reads it), the timer key scoped by
  the graph node, the wording of the refusal WARNING.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

import primer.observability.metrics as metrics
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext, timer_event_key, with_wake_park
from primer.session.yields import flip_sessions_parked_on
from primer.toolset.trigger import _make_subscribe_channel_handler, _make_subscribe_handler
from primer.worker.yield_runtime import is_timeout_payload, make_cancelled_payload, make_timeout_payload
from tests.conftest import _FakeStorageProvider
from tests.toolset.test_subscribe_to_channel_event import _seed_channel_trigger
from tests.trigger.test_parked_session_e2e import _seed_trigger


@pytest.fixture(autouse=True)
def _fresh_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


def _refused() -> float:
    return metrics.session_wake_stale_refused_total._value.get()


def _row(
    sid: str, key: str, *, until: timedelta | None, tool: str = "sleep", meta: dict | None = None, graph: bool = False, parked_at: datetime | None = None,
) -> WorkspaceSession:
    parked_at = parked_at or datetime.now(UTC)
    meta = meta or {}
    state: dict = {"tool_call_id": "call_0", "yielded": {"tool_name": tool, "event_key": key, "resume_metadata": meta}}
    if graph:
        state["graph_checkpoint"] = {
            "pending_toolcalls": [],
            "pending_agent_yields": [{"node_id": "n1", "tool_call_id": "call_0", "event_key": key, "resume_metadata": meta}],
            "pending_dispatch": [],
        }
    return WorkspaceSession(
        id=sid, workspace_id="ws", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING, created_at=parked_at,
        parked_status="parked", parked_at=parked_at, parked_until=None if until is None else parked_at + until, parked_event_key=key, parked_state=state,
    )


async def _flip_one(row: WorkspaceSession, key: str, payload: dict) -> tuple[int, WorkspaceSession]:
    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(row)
    flipped = await flip_sessions_parked_on(key, payload, session_storage=storage, engine=None)
    return flipped, await storage.get(row.id)


# ---- N1: the window ---------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("graph", [False, True], ids=["single", "graph"])
@pytest.mark.parametrize("kind", ["timeout", "timer"])
@pytest.mark.parametrize(("seconds", "lands"), [(30, False), (6, False), (4, True)])
async def test_a_deadline_wake_lands_only_inside_the_five_second_window(kind, graph, seconds, lands) -> None:
    """The publisher selected the row on ITS clock; the flipping node tolerates 5 s of skew. A park 30 s or 6 s ahead is refused (a graph park too: the stamp
    does not judge a graph, the deadline does), one 4 s ahead is woken."""
    key = "timer:s:call_0" if kind == "timer" else "ask_user:s:call_0"
    payload = {} if kind == "timer" else make_timeout_payload()
    row = _row("s", key, until=timedelta(seconds=seconds), tool="sleep" if kind == "timer" else "ask_user", graph=graph)

    flipped, after = await _flip_one(row, key, payload)

    assert (flipped, after.parked_status) == ((1, "resumable") if lands else (0, "parked"))
    assert _refused() == (0 if lands else 1)


@pytest.mark.asyncio
async def test_a_cancel_of_a_graph_park_whose_deadline_is_ahead_still_lands() -> None:
    """A cancel is a decision, not a clock: exempt from the deadline rule (stamped with the park it read, it is judged by the stamp only on a single park)."""
    key = "ask_user:s:call_0"
    row = _row("s", key, until=timedelta(hours=1), tool="ask_user", graph=True)

    flipped, after = await _flip_one(row, key, with_wake_park(make_cancelled_payload(reason="operator"), row.parked_at))

    assert flipped == 1 and after.parked_status == "resumable"


@pytest.mark.asyncio
async def test_an_empty_payload_wake_that_is_not_a_timer_fire_lands_on_a_park_whose_deadline_is_ahead() -> None:
    """Only a ``timer:`` key makes an empty payload a timer fire. A trigger fire with an empty result (``{}``) is an ordinary wake: the deadline rule does not
    apply to it, and the park (here 1 hour from its deadline) is woken."""
    key = "trigger:s:T"
    row = _row("s", key, until=timedelta(hours=1), tool="subscribe_to_trigger", meta={"subscription_id": "sb-1", "trigger_id": "T"})

    flipped, after = await _flip_one(row, key, {})

    assert (flipped, after.parked_status) == (1, "resumable")
    assert _refused() == 0


# ---- N7: a timeout marker is recognised by its key, as the classifier reads it -------------------------------------------------------------------


def test_the_fence_and_the_classifier_agree_on_what_a_timeout_marker_is() -> None:
    assert is_timeout_payload({"__yield_timeout__": False}) is True
    assert is_timeout_payload({"__yield_timeout__": True}) is True
    assert is_timeout_payload({}) is False and is_timeout_payload(None) is False and is_timeout_payload({"response": "x"}) is False


@pytest.mark.asyncio
async def test_a_timeout_marker_with_a_false_value_is_still_judged_by_the_deadline() -> None:
    key = "ask_user:s:call_0"
    flipped, after = await _flip_one(_row("s", key, until=timedelta(hours=1), tool="ask_user"), key, {"__yield_timeout__": False})

    assert (flipped, after.parked_status) == (0, "parked")


# ---- B2: a stamped cancel of an old park cannot cancel the new one -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cancel_of_an_old_sleep_does_not_cancel_the_new_one() -> None:
    key = "timer:s1:call_0"
    old_park = datetime.now(UTC) - timedelta(minutes=2)
    cancel = with_wake_park(make_cancelled_payload(reason="operator"), old_park)

    flipped, after = await _flip_one(_row("s1", key, until=timedelta(hours=1)), key, cancel)

    assert (flipped, after.parked_status) == (0, "parked")


@pytest.mark.asyncio
async def test_the_cancel_of_the_pending_sleep_lands_whatever_its_deadline() -> None:
    key = "timer:s1:call_0"
    row = _row("s1", key, until=timedelta(hours=1))

    flipped, after = await _flip_one(row, key, with_wake_park(make_cancelled_payload(reason="operator"), row.parked_at))

    assert (flipped, after.parked_status) == (1, "resumable")


# ---- B1: the trigger key carries the session -----------------------------------------------------------------------------------------------------


async def _subscribe(sp, sid: str, trigger_id: str) -> str:
    handler = _make_subscribe_handler(sp)
    out = await handler({"trigger_id": trigger_id}, ctx=ToolContext(tool_call_id="call_0", session_id=sid, workspace_id="ws"))
    return out.event_key


@pytest.mark.asyncio
async def test_two_sessions_subscribing_to_one_trigger_wait_on_different_keys() -> None:
    sp = _FakeStorageProvider()
    await _seed_trigger(sp, trigger_id="T")

    assert await _subscribe(sp, "s1", "T") == "trigger:s1:T"
    assert await _subscribe(sp, "s2", "T") == "trigger:s2:T"


@pytest.mark.asyncio
async def test_two_sessions_subscribing_to_one_channel_trigger_wait_on_different_keys() -> None:
    sp = _FakeStorageProvider()
    await _seed_channel_trigger(sp, trigger_id="T")
    handler = _make_subscribe_channel_handler(sp)
    keys = []
    for sid in ("s1", "s2"):
        out = await handler(
            {"trigger_id": "T", "event_matcher": {"event_type": "command.invoked", "command_name": "approve"}},
            ctx=ToolContext(tool_call_id="call_0", session_id=sid, workspace_id="ws"),
        )
        keys.append(out.event_key)

    assert keys == ["trigger:s1:T", "trigger:s2:T"]


@pytest.mark.asyncio
async def test_one_sessions_trigger_fire_does_not_wake_a_graph_session_parked_on_the_same_trigger(caplog) -> None:
    """The reviewer's probe. A, B (a GRAPH park, which the stamp does not judge) and C subscribed to trigger T; the fire is A's (stamped with A's park). It
    reached B through the shared key, past B's own subscription checks. With the session in the key it reaches A only."""
    sp = _FakeStorageProvider()
    await _seed_trigger(sp, trigger_id="T")
    storage = sp.get_storage(WorkspaceSession)
    t0 = datetime.now(UTC) - timedelta(minutes=1)
    rows = {}
    for i, (sid, graph) in enumerate((("A", False), ("B", True), ("C", False))):
        key = await _subscribe(sp, sid, "T")
        rows[sid] = _row(sid, key, until=timedelta(hours=1), tool="subscribe_to_trigger", meta={"subscription_id": f"sb-{sid}", "trigger_id": "T"},
                         graph=graph, parked_at=t0 + timedelta(seconds=i))
        await storage.create(rows[sid])
    fire_for_a = with_wake_park({"ok": True, "fire_context": {}, "payload": {"for": "A"}}, rows["A"].parked_at)

    with caplog.at_level(logging.WARNING):
        await flip_sessions_parked_on(rows["A"].parked_event_key, fire_for_a, session_storage=storage, engine=None)

    assert {sid: (await storage.get(sid)).parked_status for sid in rows} == {"A": "resumable", "B": "parked", "C": "parked"}
    assert "resume_event_payload" not in ((await storage.get("B")).parked_state or {})


@pytest.mark.asyncio
async def test_a_subscription_made_before_the_scoping_keeps_the_key_it_has() -> None:
    """A park in flight sits on ``trigger:{trigger_id}``; the dispatcher publishes the key the park stored, so a fire for it still lands (one session)."""
    key = "trigger:T"
    row = _row("A", key, until=timedelta(hours=1), tool="subscribe_to_trigger", meta={"subscription_id": "sb-A", "trigger_id": "T"})

    flipped, after = await _flip_one(row, key, with_wake_park({"ok": True}, row.parked_at))

    assert (flipped, after.parked_status) == (1, "resumable")


# ---- N5: the timer key carries the graph node --------------------------------------------------------------------------------------------------


def test_a_call_with_a_chat_and_no_session_scopes_its_timer_key_by_the_chat() -> None:
    """A chat-surface call has no session but has a chat id (``ask_user`` falls back to it the same way): scoped by that, never by the bare tool call id."""
    ctx = ToolContext(tool_call_id="call_0", session_id=None, workspace_id=None, chat_id="chat-1")

    assert timer_event_key(ctx) == "timer:chat-1:call_0"
    assert timer_event_key(ctx, node_id="worker[1]") == "timer:chat-1:worker[1]:call_0"


def test_the_timer_key_inside_a_graph_node_carries_the_node() -> None:
    ctx = ToolContext(tool_call_id="call_0", session_id="s", workspace_id=None)

    assert timer_event_key(ctx, node_id="worker[1]") == "timer:s:worker[1]:call_0"
    assert timer_event_key(ctx, node_id=None) == "timer:s:call_0"
    assert timer_event_key(ctx) == "timer:s:call_0"


@pytest.mark.asyncio
async def test_the_sleep_tool_inside_a_graph_node_waits_on_a_node_scoped_key() -> None:
    from primer.graph._node_identity import reset_current_graph_node_id, set_current_graph_node_id
    from primer.toolset.misc import _sleep_handler

    ctx = ToolContext(tool_call_id="call_0", session_id="s", workspace_id=None)
    first, second = None, None
    token = set_current_graph_node_id("worker[0]")
    try:
        first = await _sleep_handler({"seconds": 30}, ctx=ctx)
    finally:
        reset_current_graph_node_id(token)
    token = set_current_graph_node_id("worker[1]")
    try:
        second = await _sleep_handler({"seconds": 30}, ctx=ctx)
    finally:
        reset_current_graph_node_id(token)

    assert (first.event_key, second.event_key) == ("timer:s:worker[0]:call_0", "timer:s:worker[1]:call_0"), "two siblings sharing a raw id wait on two keys"


def test_the_python_runner_timer_inside_a_graph_node_waits_on_a_node_scoped_key() -> None:
    from primer.graph._node_identity import reset_current_graph_node_id, set_current_graph_node_id
    from primer.toolset.python_runner.yielding import to_yielded

    token = set_current_graph_node_id("worker[1]")
    try:
        y = to_yielded({"kind": "timer", "params": {"seconds": 5}, "meta": {}}, tool_name="nap", ctx=ToolContext(tool_call_id="tc-1", session_id="s-1", workspace_id=None),
                       source_version=1)
    finally:
        reset_current_graph_node_id(token)

    assert y.event_key == "timer:s-1:worker[1]:tc-1"


# ---- N2: the refusal says why ------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_refusal_names_clock_skew_and_a_key_shared_across_sessions(caplog) -> None:
    key = "timer:s1:call_0"
    with caplog.at_level(logging.WARNING):
        await _flip_one(_row("s1", key, until=timedelta(hours=1)), key, {})

    [message] = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert "clock" in message and "shares the key" in message and "re-parked" in message


@pytest.mark.asyncio
async def test_the_stamp_refusal_names_a_key_shared_across_sessions_too(caplog) -> None:
    key = "trigger:T"
    row = _row("s1", key, until=timedelta(hours=1), tool="subscribe_to_trigger")
    with caplog.at_level(logging.WARNING):
        await _flip_one(row, key, with_wake_park({"ok": True}, row.parked_at - timedelta(minutes=5)))

    [message] = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert "shares the key" in message and "re-parked" in message
