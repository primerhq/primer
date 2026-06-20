"""Worker post-park hook: dispatches an envelope to the channel dispatcher."""

from __future__ import annotations

import asyncio

import pytest

from primer.channel.adapter import PromptEnvelope
from primer.model.workspace_session import WorkspaceSession, AgentSessionBinding, SessionStatus
from primer.model.yield_ import Yielded
from primer.worker.yield_runtime import _dispatch_to_channels
from datetime import datetime, timezone


class _FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[PromptEnvelope] = []

    async def dispatch_prompt(self, *, envelope: PromptEnvelope, session=None) -> list:
        self.calls.append(envelope)
        return [{"ok": True}]


def _ask_user_yielded() -> Yielded:
    return Yielded(
        tool_name="ask_user",
        event_key="ask_user:sess:tc",
        resume_metadata={
            "prompt": "do the thing?",
            "response_schema": None,
            "timeout_seconds": None,
        },
    )


def _approval_yielded() -> Yielded:
    return Yielded(
        tool_name="_approval",
        event_key="tool_approval:sess:tc",
        resume_metadata={
            "policy_id": "p1",
            "approval_type": "required",
            "gate_reason": "always",
            "original_call": {
                "id": "tc",
                "name": "delete_workspace",
                "arguments": {"id": "ws-x"},
            },
        },
    )


def _session() -> WorkspaceSession:
    """Build a minimal WorkspaceSession row."""
    return WorkspaceSession(
        id="sess",
        workspace_id="ws-1",
        binding=AgentSessionBinding(kind="agent", agent_id="agt"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_dispatch_ask_user_builds_envelope():
    d = _FakeDispatcher()
    await _dispatch_to_channels(dispatcher=d, session=_session(), yielded=_ask_user_yielded())
    assert len(d.calls) == 1
    env = d.calls[0]
    assert env.kind == "ask_user"
    assert env.workspace_id == "ws-1"
    assert env.session_id == "sess"
    assert env.tool_call_id == "tc"
    assert env.prompt == "do the thing?"


@pytest.mark.asyncio
async def test_dispatch_approval_builds_envelope_with_choices():
    d = _FakeDispatcher()
    await _dispatch_to_channels(dispatcher=d, session=_session(), yielded=_approval_yielded())
    assert len(d.calls) == 1
    env = d.calls[0]
    assert env.kind == "tool_approval"
    assert env.choices == ["Approve", "Reject"]
    assert "delete_workspace" in env.prompt


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_is_noop():
    d = _FakeDispatcher()
    await _dispatch_to_channels(
        dispatcher=d, session=_session(),
        yielded=Yielded(
            tool_name="sleep", event_key="sleep:tc", resume_metadata={},
        ),
    )
    assert d.calls == []


@pytest.mark.asyncio
async def test_dispatch_none_dispatcher_is_noop():
    await _dispatch_to_channels(
        dispatcher=None, session=_session(), yielded=_ask_user_yielded(),
    )


@pytest.mark.asyncio
async def test_dispatch_multi_event_sends_one_message_per_pending_node():
    from primer.worker.yield_runtime import _dispatch_to_channels_multi

    class _Disp:
        def __init__(self): self.calls = []
        async def dispatch_prompt(self, *, envelope, session=None):
            self.calls.append(envelope); return [{"ok": True}]

    d = _Disp()
    pending = [
        {"kind": "ask_user", "tool_call_id": "tc1",
         "resume_metadata": {"prompt": "color?"}},
        {"kind": "_approval", "tool_call_id": "tc2",
         "resume_metadata": {"original_call": {"id": "tc2", "name": "workspace__write",
                                               "arguments": {"path": "a.txt"}}}},
    ]
    sent = await _dispatch_to_channels_multi(
        dispatcher=d, workspace_id="w1", session_id="s1",
        pending=pending, already_sent=set())
    assert {e.kind for e in d.calls} == {"ask_user", "tool_approval"}
    assert {e.tool_call_id for e in d.calls} == {"tc1", "tc2"}
    appr = next(e for e in d.calls if e.kind == "tool_approval")
    assert appr.tool_name == "workspace__write"
    assert appr.tool_args == {"path": "a.txt"}
    assert sent == {"tc1", "tc2"}


@pytest.mark.asyncio
async def test_dispatch_multi_event_skips_already_sent():
    from primer.worker.yield_runtime import _dispatch_to_channels_multi

    class _Disp:
        def __init__(self): self.calls = []
        async def dispatch_prompt(self, *, envelope, session=None):
            self.calls.append(envelope); return [{"ok": True}]

    d = _Disp()
    pending = [{"kind": "ask_user", "tool_call_id": "tc1", "resume_metadata": {"prompt": "q"}}]
    sent = await _dispatch_to_channels_multi(
        dispatcher=d, workspace_id="w1", session_id="s1",
        pending=pending, already_sent={"tc1"})
    assert d.calls == []  # already sent -> no re-send
    assert sent == {"tc1"}
