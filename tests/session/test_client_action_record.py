"""client_action records: translation, tap class, reader pass-through."""

from __future__ import annotations

from datetime import datetime, timezone

from primer.model.chat import ExtendedEvent, _ClientAction
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session.persistence import _CoalesceState, translate_stream_event
from primer.tap.event import TapEventClass, record_to_tap_event
from primer.tap.reader import _parse_record

FIXED_TS = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


def test_both_enums_carry_client_action() -> None:
    assert SessionMessageKind.CLIENT_ACTION == "client_action"
    assert TapEventClass.CLIENT_ACTION == "client_action"
    assert TapEventClass(SessionMessageKind.CLIENT_ACTION.value) is (
        TapEventClass.CLIENT_ACTION
    )


def test_translator_emits_one_client_action_record() -> None:
    ev = ExtendedEvent(
        extended=_ClientAction(
            call_id="tc-1",
            name="client__open_file",
            arguments={"path": "a.txt", "line": 3},
        )
    )
    rec = translate_stream_event(ev, _CoalesceState())
    assert isinstance(rec, SessionMessageRecord)
    assert rec.kind == SessionMessageKind.CLIENT_ACTION
    assert rec.payload == {
        "call_id": "tc-1",
        "name": "client__open_file",
        "arguments": {"path": "a.txt", "line": 3},
    }
    assert rec.node_id is None


def test_record_maps_to_the_tap_event_class() -> None:
    rec = SessionMessageRecord(
        seq=5,
        kind=SessionMessageKind.CLIENT_ACTION,
        payload={"call_id": "tc-1", "name": "client__open_file", "arguments": {}},
        created_at=FIXED_TS,
    )
    ev = record_to_tap_event(
        rec,
        workspace_id="ws-1",
        session_id="s-1",
        agent_id="ag-1",
        graph_id=None,
        cursor="c",
    )
    assert ev.class_ is TapEventClass.CLIENT_ACTION
    assert ev.seq == 5
    assert ev.payload["name"] == "client__open_file"


def test_reader_does_not_skip_it() -> None:
    rec = SessionMessageRecord(
        seq=5,
        kind=SessionMessageKind.CLIENT_ACTION,
        payload={"call_id": "tc-1", "name": "client__open_file", "arguments": {}},
        created_at=FIXED_TS,
    )
    parsed = _parse_record(rec.model_dump_json().encode())
    assert parsed is not None
    assert parsed.kind == SessionMessageKind.CLIENT_ACTION
