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
