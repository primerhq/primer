"""``run_agent_turn``'s claims-seam ignition (Phase 3 stage 7a, 01a0518b,
7a gate verdict item 5) - direct tests for the ACTUAL routing gate
(``_dispatch_tool_calls``) and ``_dispatch_as_claims``' own body.

Every existing park test across this arc hand-raises ``ToolWaitPark``
directly (a fake LLM/executor layer raising it, or the exception
constructed by hand) - the gate condition itself
(``tool_calls_as_claims_enabled and resolve_scoped_call is not None``,
further gated on the batch having at least one CLAIMABLE call) and
``_dispatch_as_claims``' own per-call contract (barrier-once,
resolver-for-every-call, scoped notifying-result ids) had never
actually been exercised through the real code that implements them.
"""

from __future__ import annotations

import pytest

from primer.agent.loop import _dispatch_as_claims, _dispatch_tool_calls
from primer.model.chat import ToolCallPart, ToolResultPart
from primer.model.yield_ import ToolWaitPark


class _FakeToolManager:
    def __init__(self, *, notifying_names: frozenset = frozenset()) -> None:
        self._notifying_names = notifying_names
        self.executed: list[str] = []
        self.delivered: list[str] = []

    def is_notifying(self, name: str) -> bool:
        return name in self._notifying_names

    async def deliver_notifying(self, call: ToolCallPart, *, principal):
        self.delivered.append(call.id)
        return ToolResultPart(id=call.id, output=f"notified {call.name}", error=False)

    async def execute(self, call: ToolCallPart, *, principal):
        self.executed.append(call.id)
        return ToolResultPart(id=call.id, output=f"executed {call.name}", error=False)


def _resolver(mapping: dict[str, tuple[str, int]]):
    def _resolve(raw_id: str) -> tuple[str, int]:
        return mapping[raw_id]
    return _resolve


def _call(id_: str, name: str = "tool") -> ToolCallPart:
    return ToolCallPart(id=id_, name=name, arguments={})


# ===========================================================================
# _dispatch_tool_calls - the routing gate itself
# ===========================================================================


@pytest.mark.asyncio
async def test_routing_falls_through_when_flag_off() -> None:
    """Flag off -> classic in-process dispatch, regardless of resolver."""
    tm = _FakeToolManager()
    result = await _dispatch_tool_calls(
        [_call("c1")], tool_manager=tm, principal=None, actions_out=[],
        tool_calls_as_claims_enabled=False,
        resolve_scoped_call=_resolver({"c1": ("x:tool:0:1", 1)}),
    )
    assert tm.executed == ["c1"]
    assert result and result[0].role == "tool"


@pytest.mark.asyncio
async def test_routing_falls_through_when_resolver_none() -> None:
    """Flag on but resolver None -> classic in-process dispatch (the gate
    is an AND of both, per run_agent_turn's own resolve_scoped_call
    docstring: "a NON-None resolver is required... even when
    tool_calls_as_claims_enabled is True")."""
    tm = _FakeToolManager()
    result = await _dispatch_tool_calls(
        [_call("c1")], tool_manager=tm, principal=None, actions_out=[],
        tool_calls_as_claims_enabled=True, resolve_scoped_call=None,
    )
    assert tm.executed == ["c1"]
    assert result and result[0].role == "tool"


@pytest.mark.asyncio
async def test_routing_falls_through_when_batch_is_entirely_notifying() -> None:
    """Flag+resolver both set, but the batch has NO claimable call once
    partitioned - nothing to park ON, so the classic path handles it
    inline (see _dispatch_tool_calls' own docstring)."""
    tm = _FakeToolManager(notifying_names=frozenset({"notify"}))
    result = await _dispatch_tool_calls(
        [_call("c1", name="notify")], tool_manager=tm, principal=None,
        actions_out=[], tool_calls_as_claims_enabled=True,
        resolve_scoped_call=_resolver({"c1": ("x:tool:0:1", 1)}),
    )
    assert tm.delivered == ["c1"]
    assert tm.executed == []
    assert result and result[0].role == "tool"


@pytest.mark.asyncio
async def test_routing_fires_claims_path_with_flag_and_resolver_and_claimable_call() -> None:
    tm = _FakeToolManager()
    with pytest.raises(ToolWaitPark):
        await _dispatch_tool_calls(
            [_call("c1")], tool_manager=tm, principal=None, actions_out=[],
            tool_calls_as_claims_enabled=True,
            resolve_scoped_call=_resolver({"c1": ("x:tool:0:1", 1)}),
        )
    # Never reaches the classic in-process execute path.
    assert tm.executed == []


@pytest.mark.asyncio
async def test_routing_fires_claims_path_for_mixed_batch_with_one_claimable() -> None:
    """A batch with BOTH a notifying and a claimable call still routes
    to the claims path (the gate only needs ONE claimable call, per
    _dispatch_tool_calls' own `if claimable_calls:` check)."""
    tm = _FakeToolManager(notifying_names=frozenset({"notify"}))
    with pytest.raises(ToolWaitPark):
        await _dispatch_tool_calls(
            [_call("n1", name="notify"), _call("c1")],
            tool_manager=tm, principal=None, actions_out=[],
            tool_calls_as_claims_enabled=True,
            resolve_scoped_call=_resolver({
                "n1": ("x:tool:0:1", 1), "c1": ("x:tool:0:2", 2),
            }),
        )
    assert tm.executed == []


# ===========================================================================
# _dispatch_as_claims - own body: always raises, barrier-once,
# resolver-for-every-call, scoped notifying-result ids
# ===========================================================================


@pytest.mark.asyncio
async def test_dispatch_as_claims_always_raises_never_returns() -> None:
    tm = _FakeToolManager()
    with pytest.raises(ToolWaitPark):
        await _dispatch_as_claims(
            [], [_call("c1")], tool_manager=tm, principal=None, actions_out=[],
            resolve_scoped_call=_resolver({"c1": ("x:tool:0:1", 1)}),
        )


@pytest.mark.asyncio
async def test_barrier_awaited_once_before_any_resolve() -> None:
    calls_order: list[str] = []

    def _resolve(raw_id: str) -> tuple[str, int]:
        calls_order.append(f"resolve:{raw_id}")
        return (f"x:tool:0:{raw_id}", 1)

    async def _barrier() -> None:
        calls_order.append("barrier")

    tm = _FakeToolManager(notifying_names=frozenset({"notify"}))
    with pytest.raises(ToolWaitPark):
        await _dispatch_as_claims(
            [_call("n1", name="notify")], [_call("c1")],
            tool_manager=tm, principal=None, actions_out=[],
            resolve_scoped_call=_resolve, await_dispatch_barrier=_barrier,
        )
    assert calls_order == ["barrier", "resolve:n1", "resolve:c1"]
    assert calls_order.count("barrier") == 1


@pytest.mark.asyncio
async def test_no_barrier_is_a_noop_when_none() -> None:
    """The chat/workspace surface never binds a barrier - None must not
    be called or raise."""
    tm = _FakeToolManager()
    with pytest.raises(ToolWaitPark):
        await _dispatch_as_claims(
            [], [_call("c1")], tool_manager=tm, principal=None, actions_out=[],
            resolve_scoped_call=_resolver({"c1": ("x:tool:0:1", 1)}),
            await_dispatch_barrier=None,
        )


@pytest.mark.asyncio
async def test_resolver_called_for_every_call_notifying_and_claimable() -> None:
    resolved: list[str] = []

    def _resolve(raw_id: str) -> tuple[str, int]:
        resolved.append(raw_id)
        return (f"x:tool:0:{raw_id}", 1)

    tm = _FakeToolManager(notifying_names=frozenset({"notify"}))
    with pytest.raises(ToolWaitPark) as excinfo:
        await _dispatch_as_claims(
            [_call("n1", name="notify")], [_call("c1"), _call("c2")],
            tool_manager=tm, principal=None, actions_out=[],
            resolve_scoped_call=_resolve,
        )
    assert resolved == ["n1", "c1", "c2"]
    assert excinfo.value.outstanding_task_ids == ["x:tool:0:c1", "x:tool:0:c2"]


@pytest.mark.asyncio
async def test_notifying_results_carry_scoped_ids_not_raw_ids() -> None:
    tm = _FakeToolManager(notifying_names=frozenset({"notify"}))
    with pytest.raises(ToolWaitPark) as excinfo:
        await _dispatch_as_claims(
            [_call("n1", name="notify")], [_call("c1")],
            tool_manager=tm, principal=None, actions_out=[],
            resolve_scoped_call=_resolver({
                "n1": ("x:tool:0:1", 1), "c1": ("x:tool:0:2", 2),
            }),
        )
    notifying_results = excinfo.value.notifying_results
    assert len(notifying_results) == 1
    scoped_id, result_part = notifying_results[0]
    # The tuple's own key is the SCOPED id - what _node_dispatch.py's
    # ToolCallTask row creation actually keys off - not the raw provider
    # id ("n1") deliver_notifying's own ToolResultPart still carries.
    assert scoped_id == "x:tool:0:1"
    assert result_part.output == "notified notify"


@pytest.mark.asyncio
async def test_event_key_is_synthetic_not_pub_sub() -> None:
    """ToolWaitPark.event_key is observability-only (its own docstring) -
    keyed on the first outstanding task, never looked up by anything."""
    tm = _FakeToolManager()
    with pytest.raises(ToolWaitPark) as excinfo:
        await _dispatch_as_claims(
            [], [_call("c1"), _call("c2")],
            tool_manager=tm, principal=None, actions_out=[],
            resolve_scoped_call=_resolver({
                "c1": ("x:tool:0:1", 1), "c2": ("x:tool:0:2", 2),
            }),
        )
    assert excinfo.value.event_key == "tool_wait:x:tool:0:1"
