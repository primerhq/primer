"""RED probe (D-A3-03): a reasoning-only response must fail the turn, not end as a silent success.

A reasoning model that burns its output budget thinking emits reasoning
deltas and a Done and nothing the default converter can turn into an answer
(``ReasoningDelta`` is dropped by ``output_to_message``). The agent loop's
empty-stream branch logs a warning and returns, so the turn ends in success
with no ``TurnStreamFailure`` and no ``ended_detail``: the session looks
healthy while the model said nothing, and the orchestrator tight-loops the
LLM (all three qwen3.8 boxes are reasoning models). The branch must fail the
turn as a ``TurnStreamFailure`` carrying ``reasoning_only_response``, the
way the sibling failure branches in the same file already do; the existing
failure exit then stamps ``last_turn_error``.
"""

from __future__ import annotations

import json

import pytest

from primer.model.chat import Done, Message, ReasoningDelta, TextPart
from primer.model.workspace_session import SessionMessageKind, WorkspaceSession
from tests.session.test_dispatch import (  # noqa: F401 (fixtures are used by name)
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)
from tests.session.test_dispatch_interrupt import _run


def _records(io, session_id: str) -> list[dict]:
    return [json.loads(line) for line in io.read_lines(session_id)]


class _Manager:
    """The minimal tool surface the loop reads (no tools offered)."""

    def is_notifying(self, tool_name: str) -> bool:
        return False

    async def list_tools(self, *, principal=None):
        return []

    async def execute(self, call, *, principal=None):
        raise AssertionError("the reasoning-only turn must not dispatch a tool")


class _ReasoningOnlyLlm:
    """Burns its whole output on thinking: reasoning deltas, then Done, no text."""

    def stream(self, **_kwargs):
        async def gen():
            yield ReasoningDelta(text="thinking hard", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        return gen()


class _RealLoopExecutor:
    """Drives the real agent loop; the defect sits in its empty-stream branch."""

    last_done_reason = "stop"

    def __init__(self) -> None:
        self.event = None

    def bind_interrupt_event(self, event) -> None:
        self.event = event

    async def invoke(self, messages, **_kwargs):
        from primer.agent.loop import run_agent_turn
        from primer.model.agent import Agent, AgentModel
        from primer.model.model_profile import ModelProfileConfig
        from primer.model_profile import ResolvedModel

        holder: list[bool] = []
        async for ev in run_agent_turn(
            agent=Agent(id="ag", description="x", model=AgentModel(profile_id="p--m"), max_tool_turns=10),
            llm=_ReasoningOnlyLlm(), llm_model=ResolvedModel(
                profile_id="p", provider_id="prov", model_name="m", context_length=4096,
                config=ModelProfileConfig(),
            ),
            tool_manager=_Manager(), prompt=[Message(role="user", parts=[TextPart(text="go")])],
            interrupt=self.event, interrupted_out=holder,
        ):
            yield ev


@pytest.mark.asyncio
async def test_a_reasoning_only_response_fails_the_turn_with_ended_detail(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
) -> None:
    sid = seeded_session.id
    outcome = await _run(
        fake_storage_provider, fake_workspace_io, fake_event_bus, _RealLoopExecutor(), sid,
    )

    assert outcome.success is False, "a reasoning-only response ended the turn as a silent success"
    row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
    assert row.last_turn_error is not None and row.last_turn_error.code == "reasoning_only_response", (
        f"the failure must carry the reasoning_only_response detail, got {row.last_turn_error}"
    )
    kinds = [r["kind"] for r in _records(fake_workspace_io, sid)]
    assert SessionMessageKind.REASONING in kinds, "the thinking the model did say must stay in the log"
    assert SessionMessageKind.ASSISTANT_TOKEN not in kinds, "nothing was convertible into an answer"
