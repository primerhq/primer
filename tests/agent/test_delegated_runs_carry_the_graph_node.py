"""A delegated run is stamped with the graph node that delegated to it, through the real ``run_subagent`` / ``resume_subagent`` (ticket 01a11cca).

Two fan-out siblings of a graph run concurrently, each in its own task with its own ambient node id (``primer.graph._node_identity``); each delegates under the SAME raw
call id because their providers synthesise ``call_0``. The recorder's records must say which node each run belongs to, or the console nests both runs under the last call. Frames
that park keep the node in the resume context, so a run resumed on another worker still says it.
"""

from __future__ import annotations

import asyncio

from primer.agent.invoke import invocation_depth_guard, resume_subagent, run_subagent
from primer.graph._node_identity import reset_current_graph_node_id, set_current_graph_node_id
from primer.model.chat import ToolResultPart
from primer.session.delegation import DelegationRecorder, reset_delegation_sink, set_delegation_sink
from primer.worker.frames import AgentResumeContext
from tests.agent.test_delegated_runs_carry_a_run_id import (
    _Bus,
    _ProviderRegistry,
    _ScriptedLLM,
    _StorageProvider,
    _Writer,
    _agent,
    _payloads,
    _provider_row,
    _text,
    _tool_call,
    _world,
)


async def _delegate(storage, registry, node: str | None) -> None:
    token = set_current_graph_node_id(node) if node is not None else None
    try:
        with invocation_depth_guard():
            await run_subagent(
                agent_id="agent-sub", prompt=f"for {node}", storage_provider=storage, provider_registry=registry,
                principal="user-1", session_id="sess-1", workspace_id="ws-1", invoke_tool_call_id="call_0", turn_no=1,
            )
    finally:
        if token is not None:
            reset_current_graph_node_id(token)


def _one_agent_world(answer: str):
    storage = _StorageProvider(agent=_agent(tools=[]), provider_row=_provider_row())
    registry = _ProviderRegistry(llm=_ScriptedLLM([_text(answer)]), toolset=None)
    return storage, registry


async def test_a_run_made_inside_a_graph_node_is_stamped_with_that_node() -> None:
    storage, registry = _one_agent_world("answer")
    writer = _Writer()
    token = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-1"))
    try:
        await _delegate(storage, registry, "worker[1]")
    finally:
        reset_delegation_sink(token)
    payloads = _payloads(writer)
    assert payloads and {p["delegate_node_id"] for p in payloads} == {"worker[1]"}


async def test_a_run_outside_a_graph_has_no_node() -> None:
    storage, registry = _one_agent_world("answer")
    writer = _Writer()
    token = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-1"))
    try:
        await _delegate(storage, registry, None)
    finally:
        reset_delegation_sink(token)
    assert _payloads(writer) and all("delegate_node_id" not in p for p in _payloads(writer))


async def test_concurrent_siblings_that_reuse_one_raw_call_id_keep_their_own_nodes() -> None:
    """The ticket's scene: nodes A and B run at once and both delegate under ``call_0``."""
    storage_a, registry_a = _one_agent_world("answer of A")
    storage_b, registry_b = _one_agent_world("answer of B")
    writer = _Writer()
    token = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-1"))
    try:
        await asyncio.gather(
            asyncio.create_task(_delegate(storage_a, registry_a, "A")),
            asyncio.create_task(_delegate(storage_b, registry_b, "B")),
        )
    finally:
        reset_delegation_sink(token)
    by_text = {p["text"]: p["delegate_node_id"] for p in _payloads(writer) if "text" in p}
    assert by_text == {"answer of A": "A", "answer of B": "B"}
    assert {p["delegate_tool_call_id"] for p in _payloads(writer)} == {"call_0"}, "the raw id is shared: only the node tells them apart"


async def test_a_nested_run_keeps_the_node_of_the_run_that_delegated_to_it() -> None:
    storage, registry = _world([_tool_call("call_0"), _text("inner"), _text("outer")])
    writer = _Writer()
    token = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-1"))
    try:
        await _delegate(storage, registry, "A")
    finally:
        reset_delegation_sink(token)
    payloads = _payloads(writer)
    assert {p["delegate_run_id"] for p in payloads} and len({p["delegate_run_id"] for p in payloads}) == 2, "a child and a grandchild run"
    assert {p["delegate_node_id"] for p in payloads} == {"A"}


async def test_a_resumed_run_is_stamped_with_the_node_its_frame_carried() -> None:
    storage = _StorageProvider(agent=_agent(tools=[]), provider_row=_provider_row())
    registry = _ProviderRegistry(llm=_ScriptedLLM([_text("done after the resume")]), toolset=None)
    context = AgentResumeContext(
        session_id="sess-1", workspace_id="ws-1", chat_id=None, principal="user-1", tools=[], turn_no=1,
        delegate_run_id="run-before-the-park", delegate_parent_run_id=None, delegate_node_id="worker[0]",
    )
    writer = _Writer()
    token = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-1"))
    try:
        await resume_subagent(
            agent_id="agent-sub", context=context, llm_messages=[], child_result=ToolResultPart(id="call-x", output="ok"),
            depth=1, storage_provider=storage, provider_registry=registry, invoke_tool_call_id="call_0",
        )
    finally:
        reset_delegation_sink(token)
    assert _payloads(writer) and {p["delegate_node_id"] for p in _payloads(writer)} == {"worker[0]"}


def test_the_node_survives_the_park_blob_and_an_old_blob_still_loads() -> None:
    context = AgentResumeContext(
        session_id="s", workspace_id="w", chat_id=None, principal="p", tools=["t"], turn_no=3,
        delegate_run_id="run-1", delegate_parent_run_id="run-0", delegate_node_id="worker[2]",
    )
    assert AgentResumeContext.from_jsonable(context.to_jsonable()).delegate_node_id == "worker[2]"
    old = AgentResumeContext.from_jsonable({"session_id": "s", "workspace_id": "w", "chat_id": None, "principal": "p", "tools": []})
    assert old.delegate_node_id is None
