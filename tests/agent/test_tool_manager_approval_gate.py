"""Verify ToolExecutionManager parks via YieldToWorker when a policy gates a call."""

from __future__ import annotations

import pytest

from primer.agent.approval import ApprovalResolver
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.tool_approval import (
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)
from primer.model.yield_ import YieldToWorker


class _FakeAgentSession:
    """Bare-minimum stand-in for AgentSession with a known session_id."""

    workspace_id = "ws-test"
    session_id = "sess-scoped-test"
    agent_id = "agent-test"
    workspace_tools: list = []


class _PoliciesOnlyResolver(ApprovalResolver):
    """Test resolver bypassing storage."""

    def __init__(self, policies: list[ToolApprovalPolicy]) -> None:
        self._policies = policies
        self._ttl = 60.0
        self._cache = {}

    async def find(self, *, toolset_id, tool_name):
        for p in self._policies:
            if p.toolset_id == toolset_id and p.tool_name == tool_name:
                return p
        return None


# The catalog scopes tool names as ``toolset_id__bare_name``, so the LLM
# (and these tests) use ``_test__echo`` as the call name.
_SCOPED_NAME = "_test__echo"


@pytest.mark.asyncio
async def test_execute_raises_yield_to_worker_on_required_policy(
    tool_manager_with_test_tools,
):
    tm = tool_manager_with_test_tools
    tm._approval_resolver = _PoliciesOnlyResolver(
        [
            ToolApprovalPolicy(
                id="p",
                toolset_id="_test",
                tool_name="echo",
                approval=RequiredApprovalConfig(),
            ),
        ]
    )
    from primer.model.chat import ToolCallPart

    with pytest.raises(YieldToWorker) as ei:
        await tm.execute(ToolCallPart(id="c1", name=_SCOPED_NAME, arguments={"x": 1}))
    yielded = ei.value.yielded
    assert yielded.tool_name == "_approval"
    assert "tool_approval:" in yielded.event_key
    md = yielded.resume_metadata
    assert md["original_call"]["name"] == _SCOPED_NAME
    assert md["original_call"]["arguments"] == {"x": 1}


@pytest.mark.asyncio
async def test_execute_bypass_approval_skips_gate(
    tool_manager_with_test_tools,
):
    tm = tool_manager_with_test_tools
    tm._approval_resolver = _PoliciesOnlyResolver(
        [
            ToolApprovalPolicy(
                id="p",
                toolset_id="_test",
                tool_name="echo",
                approval=RequiredApprovalConfig(),
            ),
        ]
    )
    from primer.model.chat import ToolCallPart

    result = await tm.execute(
        ToolCallPart(id="c2", name=_SCOPED_NAME, arguments={"x": 2}),
        bypass_approval=True,
    )
    assert result.id == "c2"


@pytest.mark.asyncio
async def test_execute_no_policy_dispatches_normally(
    tool_manager_with_test_tools,
):
    tm = tool_manager_with_test_tools
    tm._approval_resolver = _PoliciesOnlyResolver([])
    from primer.model.chat import ToolCallPart

    result = await tm.execute(
        ToolCallPart(id="c3", name=_SCOPED_NAME, arguments={"x": 3}),
    )
    assert result.id == "c3"


@pytest.mark.asyncio
async def test_approval_event_key_is_session_scoped(
    tool_manager_with_test_tools,
):
    """Regression: event key must be session-scoped, not shared across sessions.

    Before the fix, _session_id / _agent_id were never assigned in __init__,
    so every approval gate used the key ``tool_approval:unknown:<call_id>``.
    Two concurrent sessions sharing a call_id would collide: one session's
    approval response would spuriously resume the other.  The fix reads
    session_id from ``_workspace_session`` so the key becomes
    ``tool_approval:<session_id>:<call_id>``.
    """
    from primer.model.chat import ToolCallPart

    # The fixture manager has no workspace_session; build a fresh manager
    # that IS bound to a session with a known id so we can assert the exact key.
    sess = _FakeAgentSession()
    # Reuse the same toolset provider from the fixture manager.
    provider = tool_manager_with_test_tools._toolsets["_test"]
    tm = ToolExecutionManager(
        toolset_providers={"_test": provider},  # type: ignore[arg-type]
        workspace_session=sess,  # type: ignore[arg-type]
    )
    tm._approval_resolver = _PoliciesOnlyResolver(
        [
            ToolApprovalPolicy(
                id="p",
                toolset_id="_test",
                tool_name="echo",
                approval=RequiredApprovalConfig(),
            ),
        ]
    )

    with pytest.raises(YieldToWorker) as ei:
        await tm.execute(ToolCallPart(id="c1", name=_SCOPED_NAME, arguments={"x": 1}))

    ek = ei.value.yielded.event_key
    expected = f"tool_approval:{sess.session_id}:c1"
    assert ek == expected, f"event key {ek!r} != {expected!r}"
    assert "unknown" not in ek  # regression guard: the old bug produced this


def _tm_bound_to(sess, provider) -> ToolExecutionManager:
    tm = ToolExecutionManager(
        toolset_providers={"_test": provider},  # type: ignore[arg-type]
        workspace_session=sess,  # type: ignore[arg-type]
    )
    tm._approval_resolver = _PoliciesOnlyResolver(
        [
            ToolApprovalPolicy(
                id="p", toolset_id="_test", tool_name="echo",
                approval=RequiredApprovalConfig(),
            ),
        ]
    )
    return tm


@pytest.mark.asyncio
async def test_approval_event_key_folds_in_the_ambient_graph_node_id(
    tool_manager_with_test_tools,
):
    """01a0518f: when a graph node's turn is in flight (the ambient
    contextvar set by _stream_node/base.py's resume loop), the approval
    gate's event_key must include that node id - not just the session id
    - so concurrent fan-out siblings of the SAME node don't share a key."""
    from primer.graph._node_identity import (
        reset_current_graph_node_id,
        set_current_graph_node_id,
    )
    from primer.model.chat import ToolCallPart

    sess = _FakeAgentSession()
    provider = tool_manager_with_test_tools._toolsets["_test"]
    tm = _tm_bound_to(sess, provider)

    token = set_current_graph_node_id("worker[0]")
    try:
        with pytest.raises(YieldToWorker) as ei:
            await tm.execute(
                ToolCallPart(id="c1", name=_SCOPED_NAME, arguments={"x": 1}),
            )
    finally:
        reset_current_graph_node_id(token)

    ek = ei.value.yielded.event_key
    expected = f"tool_approval:{sess.session_id}:worker[0]:c1"
    assert ek == expected, f"event key {ek!r} != {expected!r}"
    # The stored/round-tripped tool_call_id (fed back to the LLM on
    # resume) must stay the RAW id, untouched by the node-scoping.
    assert ei.value.tool_call_id == "c1"
    assert ei.value.yielded.resume_metadata["original_call"]["id"] == "c1"


@pytest.mark.asyncio
async def test_concurrent_fanout_siblings_sharing_a_raw_id_get_distinct_event_keys(
    tool_manager_with_test_tools,
):
    """01a0518f: THE regression this task exists for. Two concurrent
    fan-out sibling node instances can legitimately mint the identical
    raw provider tool_call_id ("call_0" restarts every LLM stream call -
    see primer/session/persistence.py's own comment on this). Without
    node-scoping, both siblings' approval gates would produce the exact
    same event_key, so resolving either one's decision would ALSO
    (mis-)resolve the other's pending park - the wrong-sibling-resolved
    correctness bug flagged in the task filing. Simulates two concurrent
    dispatches (sequential here, but each under its own ambient node id,
    exactly as two real asyncio.create_task siblings would each see via
    the contextvar's per-task copy-on-create semantics) and asserts the
    resulting event_keys never collide."""
    from primer.graph._node_identity import (
        reset_current_graph_node_id,
        set_current_graph_node_id,
    )
    from primer.model.chat import ToolCallPart

    sess = _FakeAgentSession()
    provider = tool_manager_with_test_tools._toolsets["_test"]

    keys: dict[str, str] = {}
    for node_id in ("worker[0]", "worker[1]"):
        tm = _tm_bound_to(sess, provider)
        token = set_current_graph_node_id(node_id)
        try:
            with pytest.raises(YieldToWorker) as ei:
                await tm.execute(
                    ToolCallPart(
                        id="call_0", name=_SCOPED_NAME, arguments={"x": 1},
                    ),
                )
        finally:
            reset_current_graph_node_id(token)
        keys[node_id] = ei.value.yielded.event_key

    assert keys["worker[0]"] != keys["worker[1]"], (
        f"fan-out siblings sharing raw tool_call_id 'call_0' produced the "
        f"same event_key: {keys}"
    )
    assert keys["worker[0]"] == f"tool_approval:{sess.session_id}:worker[0]:call_0"
    assert keys["worker[1]"] == f"tool_approval:{sess.session_id}:worker[1]:call_0"
