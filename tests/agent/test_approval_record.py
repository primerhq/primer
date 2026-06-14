"""Unit tests for the approval-record builders + best-effort writer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.agent.approval_record import (
    record_from_chat_pending,
    record_from_parked_blob,
    write_approval_record,
)
from primer.model.tool_approval import ToolApprovalRecord


def _blob() -> dict:
    return {
        "tool_call_id": "c1",
        "yielded": {
            "tool_name": "_approval",
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


def test_record_from_parked_blob_captures_fields():
    now = datetime.now(UTC)
    rec = record_from_parked_blob(
        blob=_blob(),
        decision="approved",
        reason=None,
        agent_id="agt",
        session_id="sess-1",
        requested_at=now,
    )
    assert rec.tool_name == "delete_workspace"
    assert rec.arguments == {"id": "ws-1"}
    assert rec.tool_call_id == "c1"
    assert rec.agent_id == "agt"
    assert rec.session_id == "sess-1"
    assert rec.chat_id is None
    assert rec.decision == "approved"
    assert rec.reason is None
    assert rec.policy_id == "p1"
    assert rec.approval_type == "required"
    assert rec.gate_reason == "always-on"
    assert rec.requested_at == now
    assert rec.decided_at is not None
    assert rec.id.startswith("tool-approval-record-")


def test_record_from_parked_blob_via_call_tool_principal():
    blob = {
        "tool_call_id": "c2",
        "yielded": {
            "resume_metadata": {
                "original_call": {"id": "c2", "name": "x", "arguments": {}},
                "via_call_tool": {"toolset_id": "stripe", "principal": "alice"},
            },
        },
    }
    rec = record_from_parked_blob(blob=blob, decision="rejected", reason="no")
    assert rec.toolset_id == "stripe"
    assert rec.principal == "alice"
    assert rec.decision == "rejected"
    assert rec.reason == "no"


def test_record_from_chat_pending_captures_fields():
    pending = {
        "tool_call_id": "ctc-1",
        "mode": "approval",
        "original_call": {"id": "ctc-1", "name": "send", "arguments": {"to": "x"}},
        "policy_id": "pp",
        "approval_type": "policy",
        "gate_reason": "spend",
    }
    rec = record_from_chat_pending(
        pending=pending, decision="cancelled", reason="cancelled by user",
        chat_id="chat-1", agent_id="agt",
    )
    assert rec.chat_id == "chat-1"
    assert rec.session_id is None
    assert rec.tool_name == "send"
    assert rec.arguments == {"to": "x"}
    assert rec.tool_call_id == "ctc-1"
    assert rec.decision == "cancelled"
    assert rec.reason == "cancelled by user"
    assert rec.policy_id == "pp"
    assert rec.approval_type == "policy"
    assert rec.gate_reason == "spend"


@pytest.mark.asyncio
async def test_write_approval_record_none_storage_is_noop():
    rec = ToolApprovalRecord(
        tool_name="x", decided_at=datetime.now(UTC), decision="approved",
    )
    # Must not raise when storage is unwired.
    await write_approval_record(None, rec)


@pytest.mark.asyncio
async def test_write_approval_record_swallows_storage_error():
    class _Boom:
        async def create(self, _entity):
            raise RuntimeError("backend down")

    rec = ToolApprovalRecord(
        tool_name="x", decided_at=datetime.now(UTC), decision="approved",
    )
    # Best-effort: a storage failure must not propagate.
    await write_approval_record(_Boom(), rec)


@pytest.mark.asyncio
async def test_write_approval_record_persists_once():
    created: list[ToolApprovalRecord] = []

    class _Storage:
        async def create(self, entity):
            created.append(entity)
            return entity

    rec = ToolApprovalRecord(
        tool_name="x", decided_at=datetime.now(UTC), decision="rejected",
    )
    await write_approval_record(_Storage(), rec)
    assert len(created) == 1
    assert created[0] is rec
