"""The flip of a parked row refuses a wake that names a different gate than the one pending (console review C-033 round 2, PR 4).

``durably_mark_session_resumable`` is the one place a decision's wake becomes ``resumable`` (the bus listener, the event dispatcher's flip sink and the
REST handlers all go through it). A wake carries ``__yield_gate_id__``, the id of the gate that was resolved when the decision was made; when it is set,
the pending entry that waits on the wake's event key has an id and the two differ, the flip is refused (False, nothing written, counted and logged). A wake
with no id, and a pending entry with none, are judged by the event key alone, as before. The key is primer-internal (``__yield_`` prefix): the resume
classification strips it, so no hook or approval classifier sees it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

import primer.observability.metrics as metrics
from primer.session.yields import durably_mark_session_resumable
from tests.api.test_gate_fence import G1, G2, _approval_session, _ask_user_session, _graph_two_gate_session
from tests.conftest import _FakeStorageProvider

WAKE_KEY = "__yield_gate_id__"
APPROVAL_KEY = "tool_approval:s:call_0"


@pytest.fixture(autouse=True)
def _fresh_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


def _refused() -> float:
    return metrics.session_wake_gate_refused_total._value.get()


async def _flip(session, event_key: str, payload: dict):
    from primer.model.workspace_session import WorkspaceSession

    storage = _FakeStorageProvider().get_storage(WorkspaceSession)
    await storage.create(session)
    did = await durably_mark_session_resumable(session, event_key=event_key, payload=payload, session_storage=storage, engine=None)
    return did, await storage.get(session.id)


@pytest.mark.asyncio
async def test_a_wake_naming_the_pending_gate_flips_it() -> None:
    did, row = await _flip(_approval_session(session_id="s", tool_call_id="call_0", gate_id=G1), APPROVAL_KEY, {"decision": "approved", WAKE_KEY: G1})

    assert did is True and row.parked_status == "resumable"
    assert _refused() == 0


@pytest.mark.asyncio
async def test_a_wake_naming_another_gate_is_refused_and_writes_nothing(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        did, row = await _flip(_approval_session(session_id="s", tool_call_id="call_0", gate_id=G2), APPROVAL_KEY, {"decision": "approved", WAKE_KEY: G1})

    assert did is False
    assert row.parked_status == "parked" and "resume_event_payload" not in (row.parked_state or {})
    assert _refused() == 1
    assert any("s" in r.getMessage() and "gate" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


@pytest.mark.asyncio
async def test_a_wake_with_no_gate_id_is_judged_by_the_event_key_alone() -> None:
    did, row = await _flip(_approval_session(session_id="s", tool_call_id="call_0", gate_id=G2), APPROVAL_KEY, {"decision": "approved"})

    assert did is True and row.parked_status == "resumable"


@pytest.mark.asyncio
async def test_a_pending_gate_with_no_id_is_judged_by_the_event_key_alone() -> None:
    """A park from before gates had ids: nothing to compare the wake's id with."""
    did, row = await _flip(_approval_session(session_id="s", tool_call_id="call_0", gate_id=None), APPROVAL_KEY, {"decision": "approved", WAKE_KEY: G1})

    assert did is True and row.parked_status == "resumable"


@pytest.mark.asyncio
async def test_an_ask_user_wake_is_judged_the_same_way() -> None:
    key = "ask_user:s:call_0"
    refused, row = await _flip(_ask_user_session(session_id="s", tool_call_id="call_0", gate_id=G2), key, {"response": "EUR", WAKE_KEY: G1})
    assert refused is False and row.parked_status == "parked"

    did, row = await _flip(_ask_user_session(session_id="s2", tool_call_id="call_0", gate_id=G1), key.replace(":s:", ":s2:"), {"response": "EUR", WAKE_KEY: G1})
    assert did is True and row.parked_status == "resumable"


@pytest.mark.asyncio
async def test_a_graph_wake_is_judged_against_the_sibling_that_waits_on_its_key() -> None:
    """Two gates of one superstep share a raw id; the key tells them apart, and so is the id compared against the entry for THAT key."""
    session = _graph_two_gate_session(session_id="s", same_raw_id=True)
    second_key = "tool_approval:s:worker[1]:dup"

    refused, row = await _flip(session, second_key, {"decision": "approved", WAKE_KEY: G1})
    assert refused is False and row.parked_status == "parked", "G1's decision was delivered on the second sibling's key"

    did, row = await _flip(_graph_two_gate_session(session_id="s", same_raw_id=True), second_key, {"decision": "approved", WAKE_KEY: G2})
    assert did is True and row.parked_status == "resumable"


def test_the_gate_key_is_stripped_from_a_real_reply_before_a_hook_sees_it() -> None:
    from primer.worker.yield_runtime import classify_marker_payload

    out = classify_marker_payload({"response": "EUR", WAKE_KEY: G1}, parked_at=datetime.now(UTC)).payload

    assert out == {"response": "EUR"}


def test_the_wake_key_is_a_primer_internal_name() -> None:
    from primer.model.yield_ import WAKE_GATE_ID_KEY

    assert WAKE_GATE_ID_KEY == WAKE_KEY and WAKE_GATE_ID_KEY.startswith("__yield_")
