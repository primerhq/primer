"""Unit tests for the approval-record builders + best-effort writer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.agent.approval_record import (
    record_from_chat_pending,
    record_from_parked_blob,
    write_approval_record,
)
from primer.model.except_ import ConflictError
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


def test_record_from_parked_blob_gate_event_key_defaults_to_none():
    """01a068da: every pre-existing caller omits the new kwarg -- must
    not become required and must not silently invent a value."""
    rec = record_from_parked_blob(blob=_blob(), decision="approved", reason=None)
    assert rec.gate_event_key is None


def test_record_from_parked_blob_carries_gate_event_key_through():
    rec = record_from_parked_blob(
        blob=_blob(), decision="approved", reason=None,
        gate_event_key="approval:sess-1:c1",
    )
    assert rec.gate_event_key == "approval:sess-1:c1"


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


@pytest.mark.asyncio
async def test_write_approval_record_swallows_a_gate_key_conflict(caplog):
    """01a068da: a ConflictError means the OTHER write site (respond-time
    vs. the resume-time fallback) already won the race for this
    gate_event_key -- ToolApprovalRecord's unique index working exactly
    as intended, not a failure. Must not raise, and should log quietly
    (DEBUG) rather than as an error."""
    import logging

    class _AlreadyThere:
        async def create(self, _entity):
            raise ConflictError("ToolApprovalRecord with id 'x' already exists")

    rec = ToolApprovalRecord(
        tool_name="x", decided_at=datetime.now(UTC), decision="approved",
        gate_event_key="approval:sess-1:c1",
    )
    with caplog.at_level(logging.DEBUG, logger="primer.agent.approval_record"):
        await write_approval_record(_AlreadyThere(), rec)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert any(
        "gate_event_key" in r.getMessage() and "approval:sess-1:c1" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_write_approval_record_still_logs_a_real_failure_as_an_error(caplog):
    """A non-conflict exception is a genuine write failure and must stay
    at ERROR level (via logger.exception) -- only the specific,
    known-benign duplicate-gate race gets the quieter treatment."""
    import logging

    class _Boom:
        async def create(self, _entity):
            raise RuntimeError("backend down")

    rec = ToolApprovalRecord(
        tool_name="x", decided_at=datetime.now(UTC), decision="approved",
    )
    with caplog.at_level(logging.DEBUG, logger="primer.agent.approval_record"):
        await write_approval_record(_Boom(), rec)
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


class _StorageWithExistingRecord:
    """Fake storage: create() always loses the gate_event_key race
    (ConflictError), find() returns the ONE record that won it."""

    def __init__(self, existing: ToolApprovalRecord) -> None:
        self._existing = existing

    async def create(self, _entity):
        raise ConflictError("ToolApprovalRecord with id 'x' already exists")

    async def find(self, _predicate, page, order_by=None):
        from primer.model.storage import OffsetPageResponse

        return OffsetPageResponse(
            offset=page.offset, length=1, total=1, items=[self._existing],
        )


@pytest.mark.asyncio
async def test_write_approval_record_warns_loudly_on_a_real_disagreement(caplog):
    """01a06b82 gate-review R1: warn_on_decision_mismatch=True is what a
    resume-time TERMINAL synthesis (timeout/cancel) passes. If the record
    that already won the gate_event_key race disagrees with the true
    terminal outcome computed here, that is NOT an ordinary benign dedup
    race (a channel reply's "approved" write whose publish never actually
    landed, followed by the gate genuinely timing out) -- it must be
    logged loudly (ERROR, both values) rather than silently swallowed at
    DEBUG like an everyday duplicate."""
    import logging

    existing = ToolApprovalRecord(
        tool_name="x", decided_at=datetime.now(UTC), decision="approved",
        gate_event_key="tool_approval:sess-1:c1",
    )
    rec = ToolApprovalRecord(
        tool_name="x", decided_at=datetime.now(UTC), decision="rejected",
        reason="timed-out", gate_event_key="tool_approval:sess-1:c1",
    )
    with caplog.at_level(logging.DEBUG, logger="primer.agent.approval_record"):
        await write_approval_record(
            _StorageWithExistingRecord(existing), rec,
            warn_on_decision_mismatch=True,
        )
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(error_records) == 1
    message = error_records[0].getMessage()
    assert "disagreement" in message
    assert "approved" in message
    assert "rejected" in message
    assert "timed-out" in message


@pytest.mark.asyncio
async def test_write_approval_record_stays_quiet_when_the_winning_decision_agrees(
    caplog,
):
    """Even with warn_on_decision_mismatch=True, a race where both writers
    agree (the common, ordinary case -- e.g. the same real operator
    decision written twice) is still just a benign dedup no-op at DEBUG,
    not a disagreement."""
    import logging

    existing = ToolApprovalRecord(
        tool_name="x", decided_at=datetime.now(UTC), decision="approved",
        gate_event_key="tool_approval:sess-1:c1",
    )
    rec = ToolApprovalRecord(
        tool_name="x", decided_at=datetime.now(UTC), decision="approved",
        gate_event_key="tool_approval:sess-1:c1",
    )
    with caplog.at_level(logging.DEBUG, logger="primer.agent.approval_record"):
        await write_approval_record(
            _StorageWithExistingRecord(existing), rec,
            warn_on_decision_mismatch=True,
        )
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert any("gate_event_key" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_write_approval_record_disagreement_check_read_failure_stays_diagnostic(
    caplog,
):
    """A failure reading the winning record for the disagreement check
    must not crash the write (still best-effort) and must not CLAIM a
    disagreement it never actually confirmed."""
    import logging

    class _FindBoom:
        async def create(self, _entity):
            raise ConflictError("dup")

        async def find(self, _predicate, _page, order_by=None):
            raise RuntimeError("read backend down")

    rec = ToolApprovalRecord(
        tool_name="x", decided_at=datetime.now(UTC), decision="rejected",
        reason="timed-out", gate_event_key="tool_approval:sess-1:c1",
    )
    with caplog.at_level(logging.DEBUG, logger="primer.agent.approval_record"):
        await write_approval_record(
            _FindBoom(), rec, warn_on_decision_mismatch=True,
        )
    # "the disagreement check itself failed" is fine (diagnostic); an
    # actual "audit disagreement:" claim would not be, since nothing here
    # confirmed one.
    assert not any(
        "audit disagreement" in r.getMessage() for r in caplog.records
    )
