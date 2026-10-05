"""A graph ``tool_call`` node whose tool is the value-yielding ``ask_user``.

Unlike an approval gate (which re-dispatches the tool with
``bypass_approval=True`` on resume), a value-yielding tool_call node's
RESULT is the operator's reply: the resume path must run the tool's resume
hook on the operator payload and feed the hook output back as the node
result, NOT re-dispatch the call (which would re-park forever).

This is the executor-level regression for the graph ask_user resume bug:
``ask`` (tool_call system__ask_user) -> ``classify`` reads ``nodes.ask.text``.
"""

from __future__ import annotations

import json

import pytest

# Importing the system toolset registers the ``ask_user`` resume hook used
# by the value-yield tool_call resume path.
import primer.toolset.system  # noqa: F401
from primer.graph._node_refs import _PendingToolCall, _is_value_yield_toolcall
from primer.graph.base import _GraphErrorEvent
from primer.graph.executor import GraphExecutor
from primer.model.graph import GraphNodeMessage, GraphThread
from primer.model.yield_ import Yielded, YieldToWorker

from tests._resume_hook_fakes import (
    build_ask_user_graph as _build_graph,
    drain as _drain,
    drain_until_yield as _drain_until_yield,
    make_toolcall_executor as _make_executor,
)
from tests.graph.test_toolcall_dispatch import _InMemoryStorage


def test_value_yield_toolcall_classified_by_tool_name() -> None:
    """The discriminator: ``ask_user`` -> value yield, ``_approval`` -> gate."""
    ask = _PendingToolCall(
        node_id="ask", tool_call_id="tc", parked_event_key="ask_user:s:tc",
        arguments={}, tool_name="ask_user", resume_metadata={"prompt": "?"},
    )
    gate = _PendingToolCall(
        node_id="ask", tool_call_id="tc", parked_event_key="tool_approval:s:tc",
        arguments={}, tool_name="_approval", resume_metadata={},
    )
    legacy = _PendingToolCall(
        node_id="ask", tool_call_id="tc", parked_event_key="x:s:tc",
        arguments={},  # no tool_name -> approval (back-compat)
    )
    assert _is_value_yield_toolcall(ask) is True
    assert _is_value_yield_toolcall(gate) is False
    assert _is_value_yield_toolcall(legacy) is False


@pytest.mark.asyncio
async def test_ask_user_toolcall_resumes_with_operator_reply() -> None:
    """Resume an ask_user tool_call park with ``{"response": "approve"}`` ->
    the node result IS the reply (run via the resume hook), the tool is NOT
    re-dispatched, and the graph drains to the end node."""
    graph = _build_graph()
    ek = "ask_user:sid:tc-ask"

    ask_yield = Yielded(
        tool_name="ask_user",
        event_key=ek,
        resume_metadata={"prompt": "Approve access?", "tool_call_id": "tc-ask"},
    )

    async def first_dispatcher(node, arguments):
        # The value-yielding ask_user tool suspends the turn.
        raise YieldToWorker(ask_yield, tool_call_id="tc-ask")

    redispatched = {"called": False}

    async def resume_dispatcher(node, arguments, bypass_approval=False):
        # The resume MUST NOT re-dispatch a value-yielding tool.
        redispatched["called"] = True  # pragma: no cover
        raise AssertionError("ask_user tool_call must not be re-dispatched")

    thread_storage: _InMemoryStorage[GraphThread] = _InMemoryStorage(GraphThread)
    message_storage: _InMemoryStorage[GraphNodeMessage] = _InMemoryStorage(
        GraphNodeMessage
    )
    thread = await GraphExecutor.open_thread(
        graph=graph, thread_storage=thread_storage,  # type: ignore[arg-type]
    )
    executor = _make_executor(
        graph, thread, thread_storage, message_storage, first_dispatcher,
    )

    _events, raised = await _drain_until_yield(executor.invoke([]))
    assert raised is not None
    assert raised.yielded.event_key == ek
    payload = executor.snapshot_state()

    # The checkpoint records the value-yield tool_name + resume_metadata.
    ptc = payload["pending_toolcalls"][0]
    assert ptc["tool_name"] == "ask_user"
    assert ptc["resume_metadata"]["prompt"] == "Approve access?"
    disp = payload["pending_dispatch"][0]
    assert disp["kind"] == "ask_user"
    assert disp["resume_metadata"]["prompt"] == "Approve access?"

    executor2 = _make_executor(
        graph, thread, thread_storage, message_storage, resume_dispatcher,
    )
    resume_events = await _drain(
        executor2.resume_from_checkpoint(
            payload,
            resumed_tcid="tc-ask",
            toolcall_payload={"response": "approve"},
        )
    )

    assert not redispatched["called"]
    assert not [e for e in resume_events if isinstance(e, _GraphErrorEvent)]
    node_out = executor2._context.nodes.get("ask") if executor2._context else None
    assert node_out is not None
    # ask_user_resume wraps the reply as {"response": <reply>}.
    assert json.loads(node_out.text) == {"response": "approve"}
    # The graph drained past the ask node to the end.
    assert "exit" in (executor2._context.nodes if executor2._context else {})
