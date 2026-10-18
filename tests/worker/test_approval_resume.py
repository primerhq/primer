"""Resume-path tests for tool_name='_approval'."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.model.chat import ToolCallPart, ToolResultPart
from primer.model.yield_ import YieldCancelled, YieldTimeout
from primer.worker.yield_runtime import _resume_tool_approval


class _FakeToolManager:
    def __init__(self) -> None:
        self.calls: list[tuple[ToolCallPart, bool]] = []

    async def execute(self, call: ToolCallPart, *, bypass_approval: bool = False):
        self.calls.append((call, bypass_approval))
        return ToolResultPart(
            id=call.id,
            output=json.dumps({"ran": True, "name": call.name, "args": call.arguments}),
            error=False,
        )


def _blob():
    return {
        "yielded": {
            "tool_name": "_approval",
            "event_key": "tool_approval:sess:c1",
            "resume_metadata": {
                "policy_id": "p1",
                "approval_type": "required",
                "gate_reason": "always-on",
                "original_call": {
                    "id": "c1",
                    "name": "delete_workspace",
                    "arguments": {"id": "ws-1"},
                },
            },
        },
    }


@pytest.mark.asyncio
async def test_resume_approved_invokes_original_call():
    tm = _FakeToolManager()
    res = await _resume_tool_approval(
        blob=_blob(), payload={"decision": "approved"}, tool_manager=tm,
    )
    assert len(tm.calls) == 1
    call, bypass = tm.calls[0]
    assert call.id == "c1"
    assert call.name == "delete_workspace"
    assert call.arguments == {"id": "ws-1"}
    assert bypass is True
    assert res.id == "c1"
    assert res.error is False


class _PrincipalAwareToolManager:
    """The real manager takes the principal string of the run (``ToolExecutionManager.execute(call, principal=, bypass_approval=)``)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, bool]] = []

    async def execute(self, call: ToolCallPart, *, principal: str | None = None, bypass_approval: bool = False):
        self.calls.append((call.name, principal, bypass_approval))
        return ToolResultPart(id=call.id, output="ran", error=False)


@pytest.mark.asyncio
async def test_resume_approved_runs_the_call_with_the_resumed_runs_principal_string():
    """A graph executor resumes its agent node's gate through this with the principal string it dispatches with; the agent session passes none (the test above: its manager takes no such argument)."""
    tm = _PrincipalAwareToolManager()
    res = await _resume_tool_approval(blob=_blob(), payload={"decision": "approved"}, tool_manager=tm, principal="alice")
    assert tm.calls == [("delete_workspace", "alice", True)]
    assert res.error is False


@pytest.mark.asyncio
async def test_resume_rejected_synthesises_error():
    tm = _FakeToolManager()
    res = await _resume_tool_approval(
        blob=_blob(),
        payload={"decision": "rejected", "reason": "no thanks"},
        tool_manager=tm,
    )
    assert tm.calls == []
    assert res.error is True
    body = json.loads(res.output)
    assert body["rejected"] is True
    assert body["reason"] == "no thanks"


@pytest.mark.asyncio
async def test_resume_timeout_synthesises_rejection():
    tm = _FakeToolManager()
    res = await _resume_tool_approval(
        blob=_blob(),
        payload=YieldTimeout(elapsed_seconds=600.0),
        tool_manager=tm,
    )
    assert res.error is True
    body = json.loads(res.output)
    assert "timed-out" in body["reason"]


@pytest.mark.asyncio
async def test_resume_cancelled_synthesises_rejection():
    tm = _FakeToolManager()
    res = await _resume_tool_approval(
        blob=_blob(),
        payload=YieldCancelled(
            reason="operator skipped",
            cancelled_at=datetime.now(timezone.utc),
            elapsed_seconds=12.0,
        ),
        tool_manager=tm,
    )
    assert res.error is True
    body = json.loads(res.output)
    assert "operator skipped" in body["reason"]


@pytest.mark.asyncio
async def test_resume_malformed_payload_rejects():
    tm = _FakeToolManager()
    res = await _resume_tool_approval(
        blob=_blob(), payload="not-a-dict", tool_manager=tm,
    )
    assert res.error is True
    body = json.loads(res.output)
    assert "malformed" in body["reason"]
