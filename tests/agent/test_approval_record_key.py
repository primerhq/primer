"""A gate's audit record is deduplicated on the GATE, not on the raw provider tool_call_id (console review C-033 PR 2, ticket 01a11f52-9d98).

``ToolApprovalRecord.gate_event_key`` carries a unique index so the respond-time write and the resume-time fallback write of ONE gate collapse into
one row. The key was the park's event key, which is built from the provider's tool_call_id, and a provider repeats that id across rounds: the second
approval of ``call_0`` in one session collided with the first and its audit record was silently dropped (append-only, so the trail then claimed the
first decision for both). The key is now ``<event_key>@<gate_id>`` for a park that has a gate id (``record_from_parked_blob`` derives it from the gate's
own ``resume_metadata``, the one place every writer goes through), so two gates write two records and a retried write for one gate is still a no-op.
A park from before gates had ids keeps ``event_key`` as its key.
"""

from __future__ import annotations

import pytest

from primer.agent.approval_record import record_from_parked_blob, write_approval_record
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.storage.sqlite import SqliteStorageProvider

EVENT_KEY = "tool_approval:s-1:call_0"
G1 = "a" * 32
G2 = "b" * 32


def _metadata(gate_id: str | None) -> dict:
    metadata = {
        "policy_id": "p1", "approval_type": "required", "gate_reason": "always-on",
        "original_call": {"id": "call_0", "name": "delete_workspace", "arguments": {"id": "ws-1"}},
    }
    if gate_id is not None:
        metadata["gate_id"] = gate_id
    return metadata


def _respond_time_blob(gate_id: str | None) -> dict:
    """What the respond route and the channel inbox hand the builder: the resolved gate's metadata projected into a ``yielded``."""
    return {"tool_call_id": "call_0", "yielded": {"resume_metadata": _metadata(gate_id)}}


def _parked_blob(gate_id: str | None) -> dict:
    """What the session resume fallback hands it: the park's own blob."""
    return {"tool_call_id": "call_0", "yielded": {"tool_name": "_approval", "event_key": EVENT_KEY, "resume_metadata": _metadata(gate_id)}}


def _record(blob: dict, decision: str = "approved", event_key: str | None = EVENT_KEY) -> ToolApprovalRecord:
    return record_from_parked_blob(blob=blob, decision=decision, reason=None, session_id="s-1", gate_event_key=event_key)


@pytest.fixture
async def storage(tmp_path):
    """A real SQLite store: it creates the unique index on gate_event_key that the in-memory test double does not enforce."""
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "records.sqlite"))
    await provider.initialize()
    try:
        yield provider.get_storage(ToolApprovalRecord)
    finally:
        await provider.aclose()


async def _all(storage) -> list[ToolApprovalRecord]:
    return list((await storage.list(OffsetPage(offset=0, length=50))).items)


def test_the_key_names_the_gate_when_the_park_has_an_id():
    assert _record(_respond_time_blob(G1)).gate_event_key == f"{EVENT_KEY}@{G1}"


def test_a_park_from_before_gates_had_ids_keeps_the_event_key():
    assert _record(_respond_time_blob(None)).gate_event_key == EVENT_KEY


def test_no_event_key_means_no_key_whatever_the_gate_id():
    assert _record(_respond_time_blob(G1), event_key=None).gate_event_key is None


def test_the_respond_time_and_resume_time_shapes_of_one_gate_agree_on_the_key():
    assert _record(_respond_time_blob(G1)).gate_event_key == _record(_parked_blob(G1)).gate_event_key


@pytest.mark.asyncio
async def test_two_approvals_of_one_raw_tool_call_id_write_two_records(storage):
    """Round 1 and round 3 park on the same raw id under different gates: both decisions are on the record, each under its own gate."""
    await write_approval_record(storage, _record(_respond_time_blob(G1), "approved"))
    await write_approval_record(storage, _record(_respond_time_blob(G2), "rejected"))

    records = await _all(storage)
    assert sorted((r.gate_event_key, r.decision) for r in records) == [(f"{EVENT_KEY}@{G1}", "approved"), (f"{EVENT_KEY}@{G2}", "rejected")]


@pytest.mark.asyncio
async def test_a_retried_write_for_the_same_gate_is_still_a_no_op(storage):
    await write_approval_record(storage, _record(_respond_time_blob(G1), "approved"))
    await write_approval_record(storage, _record(_respond_time_blob(G1), "approved"))

    assert len(await _all(storage)) == 1


@pytest.mark.asyncio
async def test_the_resume_time_fallback_collapses_into_the_respond_time_record_of_the_same_gate(storage):
    await write_approval_record(storage, _record(_respond_time_blob(G1), "approved"))
    await write_approval_record(storage, _record(_parked_blob(G1), "approved"), warn_on_decision_mismatch=True)

    assert len(await _all(storage)) == 1


@pytest.mark.asyncio
async def test_a_park_without_a_gate_id_still_dedupes_on_the_event_key(storage):
    """As before gates had ids: one raw id, one record."""
    await write_approval_record(storage, _record(_respond_time_blob(None), "approved"))
    await write_approval_record(storage, _record(_parked_blob(None), "approved"))

    records = await _all(storage)
    assert [r.gate_event_key for r in records] == [EVENT_KEY]
