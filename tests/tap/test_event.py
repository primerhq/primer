"""Tests for primer.tap.event — TapEvent primitive + record mapping."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.tap.event import TapEvent, TapEventClass, record_to_tap_event


# ---------------------------------------------------------------------------
# TapEventClass coverage
# ---------------------------------------------------------------------------


class TestTapEventClass:
    def test_mirrors_session_message_kind_values(self) -> None:
        """Every SessionMessageKind value must appear in TapEventClass."""
        smk_values = {k.value for k in SessionMessageKind}
        tec_values = {c.value for c in TapEventClass}
        assert smk_values.issubset(tec_values)

    def test_graph_transition_is_shared_kind(self) -> None:
        """``graph_transition`` is now a first-class SessionMessageKind that
        also mirrors into TapEventClass (the graph runtime emits these node
        enter/exit records into the session log; spec §2.6 / plan Task 3.1)."""
        assert TapEventClass.GRAPH_TRANSITION == "graph_transition"
        assert SessionMessageKind.GRAPH_TRANSITION == "graph_transition"
        # It IS a SessionMessageKind value now, and the mirror still holds.
        assert TapEventClass.GRAPH_TRANSITION.value in {
            k.value for k in SessionMessageKind
        }
        assert (
            TapEventClass(SessionMessageKind.GRAPH_TRANSITION.value)
            is TapEventClass.GRAPH_TRANSITION
        )

    def test_all_session_message_kind_map_to_tap_event_class(self) -> None:
        for kind in SessionMessageKind:
            tec = TapEventClass(kind.value)
            assert tec.value == kind.value


# ---------------------------------------------------------------------------
# record_to_tap_event mapping
# ---------------------------------------------------------------------------


FIXED_TS = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _make_record(kind: SessionMessageKind, seq: int = 1) -> SessionMessageRecord:
    return SessionMessageRecord(
        seq=seq,
        kind=kind,
        payload={"key": "value", "kind_hint": kind.value},
        created_at=FIXED_TS,
    )


@pytest.mark.parametrize("kind", list(SessionMessageKind))
def test_record_to_tap_event_maps_each_kind(kind: SessionMessageKind) -> None:
    record = _make_record(kind)
    event = record_to_tap_event(
        record,
        workspace_id="ws-1",
        session_id="sess-1",
        agent_id="agent-1",
        graph_id="graph-1",
        cursor="cursor-abc",
    )

    # class_ maps 1:1 from kind
    assert event.class_ == TapEventClass(kind.value)
    assert event.class_.value == kind.value

    # payload is carried through
    assert event.payload == record.payload

    # seq is copied from the record so the event is self-describing
    assert event.seq == record.seq

    # timestamp matches
    assert event.ts == record.created_at

    # injected ids are set
    assert event.workspace_id == "ws-1"
    assert event.session_id == "sess-1"
    assert event.agent_id == "agent-1"
    assert event.graph_id == "graph-1"
    assert event.cursor == "cursor-abc"


def test_record_to_tap_event_tolerates_compaction_marker() -> None:
    """record_to_tap_event must MAP the new COMPACTION_MARKER kind rather than
    crash on it (the enum stays 1:1 with SessionMessageKind). The tap reader
    then skips it from the activity stream (see primer/tap/reader.py)."""
    record = _make_record(SessionMessageKind.COMPACTION_MARKER, seq=7)
    event = record_to_tap_event(
        record,
        workspace_id="ws-1",
        session_id="sess-1",
        agent_id="agent-1",
        graph_id=None,
        cursor="cur-cm",
    )
    assert event.class_ == TapEventClass.COMPACTION_MARKER
    assert event.class_.value == "compaction_marker"
    assert event.seq == 7


def test_compaction_marker_is_skipped_by_tap_reader_parse() -> None:
    """The reader's line parser drops a compaction_marker line (returns None)
    so it is never surfaced as activity and cannot advance the drain cursor
    onto a colliding real-record seq."""
    import json

    from primer.tap.reader import _parse_record

    marker_line = json.dumps(
        {
            "seq": 4,
            "kind": "compaction_marker",
            "payload": {"summary": "s", "replaced_to_seq": 3},
            "created_at": FIXED_TS.isoformat(),
        }
    ).encode()
    assert _parse_record(marker_line) is None

    # A normal record still parses.
    real_line = json.dumps(
        {
            "seq": 5,
            "kind": "assistant_token",
            "payload": {"text": "hi"},
            "created_at": FIXED_TS.isoformat(),
        }
    ).encode()
    parsed = _parse_record(real_line)
    assert parsed is not None and parsed.seq == 5


def test_record_to_tap_event_none_optional_ids() -> None:
    record = _make_record(SessionMessageKind.DONE)
    event = record_to_tap_event(
        record,
        workspace_id="ws-x",
        session_id="sess-x",
        agent_id=None,
        graph_id=None,
        cursor="c-0",
    )
    assert event.agent_id is None
    assert event.graph_id is None


# ---------------------------------------------------------------------------
# JSON serialisation — "class" key
# ---------------------------------------------------------------------------


def test_tap_event_serialises_class_field_as_class_key() -> None:
    """model_dump(by_alias=True) must yield JSON key 'class', not 'class_'."""
    event = TapEvent(
        cursor="cur-1",
        seq=1,
        workspace_id="ws-1",
        session_id="sess-1",
        agent_id=None,
        graph_id=None,
        class_=TapEventClass.DONE,
        ts=FIXED_TS,
        payload={},
    )
    dumped = event.model_dump(by_alias=True)
    assert "class" in dumped
    assert "class_" not in dumped
    assert dumped["class"] == "done"


def test_tap_event_json_roundtrip() -> None:
    """model_dump_json round-trips through JSON preserving 'class' key."""
    event = TapEvent(
        cursor="cur-2",
        seq=2,
        workspace_id="ws-2",
        session_id="sess-2",
        agent_id="a",
        graph_id="g",
        class_=TapEventClass.TOOL_CALL,
        ts=FIXED_TS,
        payload={"x": 1},
    )
    raw = event.model_dump_json(by_alias=True)
    data = json.loads(raw)
    assert data["class"] == "tool_call"
    assert "class_" not in data


# ---------------------------------------------------------------------------
# DONE record with usage envelope
# ---------------------------------------------------------------------------


def test_done_record_with_usage_passes_through_to_tap_event() -> None:
    """A DONE SessionMessageRecord whose payload contains a ``usage`` dict
    (populated by translate_stream_event when a Usage event preceded Done)
    is carried through unchanged by record_to_tap_event so the tap layer
    delivers the full usage envelope to consumers."""
    usage_payload = {
        "stop_reason": "stop",
        "raw_reason": "stop",
        "usage": {"input_tokens": 150, "output_tokens": 60},
    }
    record = SessionMessageRecord(
        seq=5,
        kind=SessionMessageKind.DONE,
        payload=usage_payload,
        created_at=FIXED_TS,
    )
    event = record_to_tap_event(
        record,
        workspace_id="ws-u",
        session_id="sess-u",
        agent_id=None,
        graph_id=None,
        cursor="cur-u",
    )

    assert event.class_ == TapEventClass.DONE
    assert event.payload["usage"]["input_tokens"] == 150
    assert event.payload["usage"]["output_tokens"] == 60
    assert event.payload["stop_reason"] == "stop"

    # Verify the wire representation carries usage through JSON serialisation.
    wire = json.loads(event.model_dump_json(by_alias=True))
    assert wire["payload"]["usage"]["input_tokens"] == 150
    assert wire["payload"]["usage"]["output_tokens"] == 60


# ---------------------------------------------------------------------------
# node_id passthrough (F1a: per-graph-node attribution)
# ---------------------------------------------------------------------------


def test_record_node_id_passes_through_to_tap_event() -> None:
    """A record carrying node_id maps it onto the TapEvent."""
    record = SessionMessageRecord(
        seq=7,
        kind=SessionMessageKind.ASSISTANT_TOKEN,
        payload={"text": "hi"},
        created_at=FIXED_TS,
        node_id="node-7",
    )
    event = record_to_tap_event(
        record,
        workspace_id="ws",
        session_id="s",
        agent_id="a",
        graph_id="g",
        cursor="c",
    )
    assert event.node_id == "node-7"
    wire = json.loads(event.model_dump_json(by_alias=True))
    assert wire["node_id"] == "node-7"


def test_record_node_id_defaults_none() -> None:
    """A record without node_id yields a TapEvent with node_id None (agent path)."""
    record = SessionMessageRecord(
        seq=8,
        kind=SessionMessageKind.DONE,
        payload={},
        created_at=FIXED_TS,
    )
    assert record.node_id is None
    event = record_to_tap_event(
        record,
        workspace_id="ws",
        session_id="s",
        agent_id=None,
        graph_id=None,
        cursor="c",
    )
    assert event.node_id is None
