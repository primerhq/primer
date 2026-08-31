"""S7 section 5: the llm_call record kind and its tap mirror."""

from __future__ import annotations

from datetime import datetime, timezone

from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.tap.event import TapEventClass, record_to_tap_event


def test_kind_exists_with_the_wire_value():
    assert SessionMessageKind.LLM_CALL.value == "llm_call"


def test_tap_class_mirrors_the_kind():
    assert TapEventClass(SessionMessageKind.LLM_CALL.value) is TapEventClass.LLM_CALL


def test_record_maps_through_the_tap():
    rec = SessionMessageRecord(
        seq=4,
        kind=SessionMessageKind.LLM_CALL,
        payload={
            "profile_id": "prof-1",
            "provider_id": "prov-1",
            "model": "m-1",
            "input_tokens": 11,
            "output_tokens": 7,
            "duration_ms": 250,
            "status": "ok",
        },
        created_at=datetime.now(timezone.utc),
        node_id="n-1",
    )
    ev = record_to_tap_event(
        rec,
        workspace_id="ws-1",
        session_id="sess-1",
        agent_id="ag-1",
        graph_id=None,
        cursor="c",
    )
    assert ev.class_ is TapEventClass.LLM_CALL
    assert ev.node_id == "n-1"
    assert ev.payload["profile_id"] == "prof-1"
