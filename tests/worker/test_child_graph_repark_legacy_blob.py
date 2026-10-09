"""A re-park written BEFORE the child's checkpoint was carried to the top level still records the gate that was answered (C-033 round 4, pin from the review).

``repark_continuation`` used to write the re-park of an agent session's child graph with NO top-level ``graph_checkpoint``; it now carries the child's advanced
checkpoint, as the first park does. A row parked by a build before that change is still waiting when the new build deploys, and it has the old shape: the
checkpoint only inside the innermost ``GraphFrame``. The resume-time record is looked up in THAT checkpoint (the one the resume reads), so it covers the old shape;
a lookup in the top-level checkpoint would find nothing and return the primary's projection, naming T1 while T2 ran.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.worker import session_resume_coordinator as src
from tests.conftest import _FakeStorageProvider
from tests.worker.test_child_graph_repark_three_gates import APPROVED, G, SID, _scenario


async def _legacy_row(monkeypatch):
    """The re-park of the three-gates scenario, rewritten into the pre-change shape: no top-level checkpoint, the first park's ``tool_call_id``."""
    trace = await _scenario(monkeypatch, {})
    row = trace["_reparked_row"]
    state = dict(row.parked_state)
    assert state.pop("graph_checkpoint", None), "the scenario's re-park carries the child's checkpoint at the top level"
    state["tool_call_id"] = "u-T1"
    assert state["frames"][-1]["kind"] == "graph" and state["frames"][-1]["checkpoint"], "the innermost frame still carries the child's checkpoint"
    other = trace["clicked"]                                        # the OTHER tool_call gate: not the primary the blob's yielded names
    state["resume_event_key"] = f"tool_approval:{SID}:u-{other}"
    return row.model_copy(update={"parked_state": state}), other


@pytest.mark.asyncio
async def test_the_answered_gate_of_a_legacy_repark_is_found_in_the_innermost_frames_checkpoint(monkeypatch) -> None:
    row, other = await _legacy_row(monkeypatch)

    chosen = src._blob_of_the_answered_gate(row.parked_state)

    assert chosen["yielded"]["event_key"] == f"tool_approval:{SID}:u-{other}"
    assert chosen["tool_call_id"] == f"u-{other}"
    assert chosen["yielded"]["resume_metadata"]["gate_id"] == G[other], "its own metadata, not the primary's"


@pytest.mark.asyncio
async def test_the_record_of_a_legacy_repark_names_the_gate_that_was_answered(monkeypatch) -> None:
    row, other = await _legacy_row(monkeypatch)
    sp = _FakeStorageProvider()

    await src.write_approval_record_for_session(SimpleNamespace(_storage=sp), session=row, blob=row.parked_state, payload=APPROVED)

    records = (await sp.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=10))).items
    assert [r.gate_event_key.split("@")[0] for r in records] == [f"tool_approval:{SID}:u-{other}"], [r.gate_event_key for r in records]
    assert [r.arguments for r in records] == [{"path": other.lower()}], "the record describes the answered gate's call"


@pytest.mark.asyncio
async def test_a_legacy_blob_whose_fired_key_names_no_child_entry_is_left_as_it_was(monkeypatch) -> None:
    row, _other = await _legacy_row(monkeypatch)
    state = dict(row.parked_state)
    state["resume_event_key"] = f"tool_approval:{SID}:no-such-gate"

    assert src._blob_of_the_answered_gate(state) == state
