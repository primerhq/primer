"""``ChildGraphFailed``: what a failed child graph is, and the one body every delivery site sends the agent.

The end-to-end delivery (through ``run_invoke_graph`` and the continuation walk) is pinned in
``tests/worker/test_child_graph_failure_delivery.py``; this file pins the pieces it cannot reach: the refusal code
set, the body shapes, the tool-name lookup, and the stream rules of the two helpers (the first terminal error is the
root failure; a re-park wins over a failure).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from primer.graph.base import _GraphEndOutputEvent, _GraphErrorEvent
from primer.graph.invoke_graph import (
    ChildGraphFailed,
    GraphInvocationServices,
    _gated_tool_name,
    resume_invoke_graph,
    run_invoke_graph,
)
from primer.model.yield_ import Yielded, YieldToWorker


@pytest.mark.parametrize(
    ("code", "refused"),
    [
        ("tool_approval_rejected", True),
        ("tool_approval_timeout", True),
        ("tool_approval_cancelled", True),
        ("tool_execution_failed", False),
        ("routing_failed", False),
        ("fanin_upstream_failed", False),
    ],
)
def test_only_the_approval_codes_are_refusals(code, refused):
    assert ChildGraphFailed(code=code, message="m", node_id="n").refused is refused


def test_a_refusal_body_is_the_flat_approval_gates_shape():
    failed = ChildGraphFailed(code="tool_approval_rejected", message="no thanks", node_id="n", tool_name="danger__wipe")
    assert json.loads(failed.result_json()) == {"rejected": True, "reason": "no thanks", "tool_name": "danger__wipe"}


def test_a_refusal_with_no_reason_or_tool_name_still_has_both_fields():
    failed = ChildGraphFailed(code="tool_approval_rejected", message="", node_id="n")
    assert json.loads(failed.result_json()) == {"rejected": True, "reason": "(no reason supplied)", "tool_name": "unknown"}


def test_any_other_failure_body_names_the_code_the_message_and_the_node():
    failed = ChildGraphFailed(code="routing_failed", message="no branch matched", node_id="decider", tool_name="ignored")
    assert json.loads(failed.result_json()) == {"error": "routing_failed", "message": "no branch matched", "node_id": "decider"}


def test_the_gated_tool_is_read_from_the_parked_entry_of_that_node():
    ck = {
        "pending_toolcalls": [
            {"node_id": "other", "resume_metadata": {"original_call": {"name": "not__this"}}},
            {"node_id": "n1", "resume_metadata": {"original_call": {"name": "danger__wipe"}}},
        ],
        "pending_agent_yields": [{"node_id": "n2", "resume_metadata": {"original_call": {"name": "agent__tool"}}}],
    }
    assert _gated_tool_name(ck, "n1") == "danger__wipe"
    assert _gated_tool_name(ck, "n2") == "agent__tool"
    assert _gated_tool_name(ck, "gone") is None
    assert _gated_tool_name({"pending_toolcalls": [{"node_id": "n1", "resume_metadata": {}}]}, "n1") is None
    assert _gated_tool_name(None, "n1") is None


class _Child:
    """A stub child executor whose stream is the given events (an exception in the list is raised in order)."""

    def __init__(self, *events: Any) -> None:
        self._events = events

    async def _stream(self):
        for ev in self._events:
            if isinstance(ev, BaseException):
                raise ev
            yield ev

    def invoke(self, messages):
        return self._stream()

    def resume_from_checkpoint(self, checkpoint, **kwargs):
        return self._stream()


def _error(code: str, node_id: str = "n") -> _GraphErrorEvent:
    return _GraphErrorEvent(code=code, message=f"{code} message", node_id=node_id)


def _services(child: _Child) -> GraphInvocationServices:
    async def resolve_graph(graph_id):
        return {"id": graph_id}

    async def build_child_executor(*, graph, gsid):
        return child

    return GraphInvocationServices(
        resolve_graph=resolve_graph, build_child_executor=build_child_executor,
        session_id="s", workspace_id="w", graph_session_id="gs",
    )


@pytest.mark.asyncio
async def test_the_first_terminal_error_is_the_root_failure_and_beats_any_output():
    child = _Child(_error("tool_execution_failed", "a"), _error("fanin_upstream_failed", "b"), _GraphEndOutputEvent(text="partial", parsed=None, end_node_id="exit"))
    with pytest.raises(ChildGraphFailed) as raised:
        await run_invoke_graph(graph_id="g", graph_input="x", services=_services(child), tool_call_id="tc")
    assert (raised.value.code, raised.value.node_id) == ("tool_execution_failed", "a")


@pytest.mark.asyncio
async def test_a_resumed_child_that_fails_raises_with_the_gated_tool_name():
    child = _Child(_error("tool_approval_rejected", "n1"))
    checkpoint = {"pending_toolcalls": [{"node_id": "n1", "resume_metadata": {"original_call": {"name": "danger__wipe"}}}]}
    with pytest.raises(ChildGraphFailed) as raised:
        await resume_invoke_graph(child=child, checkpoint=checkpoint, payload={"decision": "approved"})
    assert raised.value.tool_name == "danger__wipe" and raised.value.refused


@pytest.mark.asyncio
async def test_a_resumed_child_that_re_parks_is_a_repark_not_a_failure():
    park = YieldToWorker(Yielded(tool_name="ask_user", event_key="ask_user:gs:n2"), tool_call_id="n2")
    child = _Child(_error("tool_execution_failed"), park)
    out, repark = await resume_invoke_graph(child=child, checkpoint={}, payload={"decision": "approved"})
    assert repark is park and out == ""
