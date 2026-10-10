"""``EngineFakePool._graph_agent_tool_result`` answers the way the real lookup does (ticket 01a125a1, follow-up of #701 round 4, N4).

The shared fake answered EVERY tool_call_id with an ask_user answer, so an approval-gated ToolCall node (which the real pool answers with ``None``, because its decision is applied by the
coordinator's bypass dispatch) was resumed with a reply it never gets in production, and one test (``_ApprovalPool``) had to override the fake to get the real shape back. The fake now mirrors
``graph_resume_coordinator.graph_agent_tool_result``: ``None`` when the checkpoint has no pending agent yield for the fired tool_call_id / event key, or when it is an approval; an answer only for a
hook-backed yield. Each case is checked against the real function too, so the two cannot drift apart again.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import primer.toolset.system  # noqa: F401  (registers the "ask_user" hook the real function calls)
from primer.worker import graph_resume_coordinator
from tests._resume_hook_fakes import EngineFakePool


def _checkpoint(*entries: tuple[str, str, str]) -> dict[str, Any]:
    """``entries``: (tool_call_id, tool_name, event_key) of each pending agent yield."""
    return {"pending_agent_yields": [
        {"tool_call_id": tcid, "tool_name": name, "event_key": key, "resume_metadata": {}} for tcid, name, key in entries
    ]}


def _fake() -> EngineFakePool:
    return EngineFakePool(storage=None, workspace_io=None, executor_factory=None)


_CASES = [
    pytest.param(_checkpoint(("tc1", "ask_user", "ask_user:s:tc1")), "tc1", None, True, id="a hook-backed yield is answered"),
    pytest.param(_checkpoint(("tc1", "ask_user", "ask_user:s:tc1")), "tc1", "ask_user:s:tc1", True, id="a hook-backed yield is answered by its event key"),
    pytest.param(_checkpoint(("tc1", "_approval", "tool_approval:s:tc1")), "tc1", None, False, id="an approval gate takes the decision path, no reply"),
    pytest.param(_checkpoint(("tc1", "ask_user", "ask_user:s:tc1")), "tc-other", None, False, id="a tool_call_id with no pending yield has no reply"),
    pytest.param(_checkpoint(("tc1", "ask_user", "ask_user:s:tc1")), "tc1", "ask_user:s:another", True, id="an event key that names no entry falls back to the tool_call_id"),
    pytest.param({}, "tc1", None, False, id="a checkpoint with no pending agent yields has no reply"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint,tcid,event_key,answered", _CASES)
async def test_the_fake_answers_exactly_when_the_real_lookup_does(checkpoint, tcid, event_key, answered) -> None:
    real = await graph_resume_coordinator.graph_agent_tool_result(
        SimpleNamespace(_provider_registry=None), checkpoint, tcid, {"response": "blue"}, session_id="gs-1", event_key=event_key,
    )
    fake = await _fake()._graph_agent_tool_result(checkpoint, tcid, {"response": "blue"}, session_id="gs-1", event_key=event_key)

    assert (real is not None) is answered, "the premise: what the real lookup answers for this checkpoint"
    assert (fake is not None) is answered, "the shared fake answers differently from the real pool"
    if fake is not None:
        (part,) = fake.parts
        assert part.id == tcid and part.error is False
