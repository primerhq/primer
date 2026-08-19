"""The switch_binding tool (S1 P3 Task 19).

Unlike the chat-era switch_to_agent, which yielded and handed the turn over
immediately, this tool never yields. It records the request on the row
and returns; the drain checkpoint applies it once the current turn
finishes. Next-turn semantics therefore need no yield protocol, no
resume-coordinator branch and no park.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.model.agent import Agent, AgentModel
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import ToolContext
from primer.toolset.system import build_system_toolset


@pytest.fixture
def sp(fake_storage_provider):
    return fake_storage_provider


@pytest.fixture
def toolset(fake_storage_provider, fake_provider_registry):
    return build_system_toolset(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )


async def _seed(sp, sid="sess-1", agent="agent-a"):
    await sp.get_storage(Agent).create(
        Agent(id=agent, description=agent,
              model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[]),
    )
    await sp.get_storage(Agent).create(
        Agent(id="agent-b", description="b",
              model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[]),
    )
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=sid, workspace_id="w1",
        binding=AgentSessionBinding(agent_id=agent),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
        turn_status="running",
    ))


def _handler(toolset):
    tool, handler = toolset._registry["switch_binding"]  # noqa: SLF001
    return tool, handler


class TestRegistration:
    def test_registered_and_non_yielding(self, toolset):
        """The whole point: no yield, so no park and no resume branch."""
        tool, _ = _handler(toolset)
        assert tool.id == "switch_binding"
        assert tool.yields is False

    def test_coexists_with_the_agent_facing_alias(self, toolset):
        """switch_to_agent is the name agents already know for this. It
        survives as an alias over the same seam rather than as the dead
        chat-era yield, which nothing would resume now that chats are
        gone."""
        assert "switch_to_agent" in toolset._registry  # noqa: SLF001
        assert "switch_binding" in toolset._registry  # noqa: SLF001
        alias, _ = toolset._registry["switch_to_agent"]  # noqa: SLF001
        assert alias.yields is False, (
            "the alias must not yield: the chat dispatch loop that used to "
            "resume it no longer exists"
        )


class TestHandler:
    async def test_queues_the_request_without_touching_the_binding(
        self, toolset, sp,
    ):
        await _seed(sp)
        _, handler = _handler(toolset)
        ctx = ToolContext(tool_call_id="tc1", session_id="sess-1",
                          workspace_id="w1")

        result = await handler(
            {"kind": "agent", "agent_id": "agent-b",
             "reason": "coding from here"}, ctx=ctx,
        )
        assert result.is_error is False
        assert "queued" in result.output

        row = await sp.get_storage(WorkspaceSession).get("sess-1")
        assert row.pending_binding_switch["agent_id"] == "agent-b"
        assert row.pending_binding_switch["actor"] == "agent"
        assert row.pending_binding_switch["reason"] == "coding from here"
        # The checkpoint applies it; the tool call itself must not.
        assert row.binding.agent_id == "agent-a"
        assert row.binding_epoch == 0

    async def test_rejected_outside_a_session(self, toolset, sp):
        """A binding is a property of a session; there is nothing else to
        switch."""
        await _seed(sp)
        _, handler = _handler(toolset)
        ctx = ToolContext(tool_call_id="tc1", session_id=None,
                          workspace_id=None, chat_id="chat-1")

        result = await handler({"kind": "agent", "agent_id": "agent-b"},
                               ctx=ctx)
        assert result.is_error is True
        assert "workspace sessions" in result.output

    async def test_unknown_target_is_refused_before_queueing(
        self, toolset, sp,
    ):
        """A queued switch to a missing agent would fail at the
        checkpoint, long after the agent could react to it."""
        await _seed(sp)
        _, handler = _handler(toolset)
        ctx = ToolContext(tool_call_id="tc1", session_id="sess-1",
                          workspace_id="w1")

        result = await handler({"kind": "agent", "agent_id": "nope"}, ctx=ctx)
        assert result.is_error is True
        row = await sp.get_storage(WorkspaceSession).get("sess-1")
        assert row.pending_binding_switch is None

    async def test_kind_and_target_must_agree(self, toolset, sp):
        await _seed(sp)
        _, handler = _handler(toolset)
        ctx = ToolContext(tool_call_id="tc1", session_id="sess-1",
                          workspace_id="w1")

        result = await handler({"kind": "graph", "agent_id": "agent-b"},
                               ctx=ctx)
        assert result.is_error is True
        assert "graph_id" in result.output


class TestSwitchToAgentAlias:
    """The chat-era name, ported onto the session binding switch.

    It kept its own two-field argument shape (agent_id + prompt), which
    is what every existing agent prompt writes, and delegates so there is
    exactly one implementation of "hand this conversation over".
    """

    async def test_alias_queues_the_same_request(self, toolset, sp):
        await _seed(sp)
        _, handler = toolset._registry["switch_to_agent"]  # noqa: SLF001
        ctx = ToolContext(tool_call_id="tc1", session_id="sess-1",
                          workspace_id="w1")

        result = await handler(
            {"agent_id": "agent-b", "prompt": "take the billing question"},
            ctx=ctx,
        )
        assert result.is_error is False
        assert "queued" in result.output

        row = await sp.get_storage(WorkspaceSession).get("sess-1")
        assert row.pending_binding_switch["kind"] == "agent"
        assert row.pending_binding_switch["agent_id"] == "agent-b"
        assert row.pending_binding_switch["actor"] == "agent"
        # The handoff prompt is what the transcript records as the reason.
        assert row.pending_binding_switch["reason"] == "take the billing question"
        assert row.binding.agent_id == "agent-a"

    async def test_alias_refuses_an_unknown_agent(self, toolset, sp):
        await _seed(sp)
        _, handler = toolset._registry["switch_to_agent"]  # noqa: SLF001
        ctx = ToolContext(tool_call_id="tc1", session_id="sess-1",
                          workspace_id="w1")

        result = await handler({"agent_id": "nope", "prompt": "go"}, ctx=ctx)
        assert result.is_error is True
        assert "does not exist" in result.output
