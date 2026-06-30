"""Tests for primer.tap.selector — TapSelector predicate handling."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.storage import FieldRef, Op, Predicate, Value
from primer.tap.event import TapEvent, TapEventClass
from primer.tap.selector import TapSelector, event_matches, session_predicate_for_storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _make_event(**overrides) -> TapEvent:
    defaults = dict(
        cursor="c1",
        seq=1,
        workspace_id="ws-1",
        session_id="sess-abc",
        agent_id="agent-1",
        graph_id="graph-1",
        class_=TapEventClass.TOOL_CALL,
        ts=_TS,
        payload={"tool": "bash", "exit_code": 0},
    )
    defaults.update(overrides)
    return TapEvent(**defaults)


# ---------------------------------------------------------------------------
# event_matches — selector.events is None → matches everything
# ---------------------------------------------------------------------------


class TestEventMatchesNoFilter:
    def test_none_selector_events_matches_all(self) -> None:
        sel = TapSelector(events=None)
        assert event_matches(sel, _make_event()) is True

    def test_none_selector_events_matches_tool_call(self) -> None:
        sel = TapSelector(events=None)
        assert event_matches(sel, _make_event(class_=TapEventClass.TOOL_CALL)) is True

    def test_none_selector_events_matches_graph_transition(self) -> None:
        sel = TapSelector(events=None)
        assert event_matches(sel, _make_event(class_=TapEventClass.GRAPH_TRANSITION)) is True


# ---------------------------------------------------------------------------
# event_matches — IN operator on class_
# ---------------------------------------------------------------------------


class TestEventMatchesIn:
    def _class_in_predicate(self, *classes: str) -> Predicate:
        return Predicate(
            left=FieldRef(name="class"),
            op=Op.IN,
            right=Value(value=list(classes)),
        )

    def test_class_in_list_matches(self) -> None:
        pred = self._class_in_predicate("tool_call", "graph_transition")
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(class_=TapEventClass.TOOL_CALL)) is True

    def test_class_in_list_matches_second_element(self) -> None:
        pred = self._class_in_predicate("tool_call", "graph_transition")
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(class_=TapEventClass.GRAPH_TRANSITION)) is True

    def test_class_in_list_no_match(self) -> None:
        pred = self._class_in_predicate("tool_call", "graph_transition")
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(class_=TapEventClass.USER_INPUT)) is False

    def test_class_alias_resolves(self) -> None:
        """FieldRef(name='class_') also resolves to TapEvent.class_."""
        pred = Predicate(
            left=FieldRef(name="class_"),
            op=Op.IN,
            right=Value(value=["tool_call"]),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(class_=TapEventClass.TOOL_CALL)) is True


# ---------------------------------------------------------------------------
# event_matches — EQ operator on session_id
# ---------------------------------------------------------------------------


class TestEventMatchesEq:
    def test_session_id_eq_match(self) -> None:
        pred = Predicate(
            left=FieldRef(name="session_id"),
            op=Op.EQ,
            right=Value(value="sess-abc"),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(session_id="sess-abc")) is True

    def test_session_id_eq_no_match(self) -> None:
        pred = Predicate(
            left=FieldRef(name="session_id"),
            op=Op.EQ,
            right=Value(value="sess-xyz"),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(session_id="sess-abc")) is False

    def test_ne_operator(self) -> None:
        pred = Predicate(
            left=FieldRef(name="session_id"),
            op=Op.NE,
            right=Value(value="sess-xyz"),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(session_id="sess-abc")) is True


# ---------------------------------------------------------------------------
# event_matches — node_id (F1a: per-graph-node attribution)
# ---------------------------------------------------------------------------


class TestEventMatchesNodeId:
    def _node_eq(self, nid: str) -> Predicate:
        return Predicate(
            left=FieldRef(name="node_id"),
            op=Op.EQ,
            right=Value(value=nid),
        )

    def test_node_id_eq_matches(self) -> None:
        sel = TapSelector(events=self._node_eq("node-a"))
        assert event_matches(sel, _make_event(node_id="node-a")) is True

    def test_node_id_eq_rejects_other_node(self) -> None:
        sel = TapSelector(events=self._node_eq("node-a"))
        assert event_matches(sel, _make_event(node_id="node-b")) is False

    def test_node_id_is_null_for_agent_only_event(self) -> None:
        pred = Predicate(
            left=FieldRef(name="node_id"),
            op=Op.IS_NULL,
            right=Value(value=None),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(node_id=None)) is True
        assert event_matches(sel, _make_event(node_id="node-a")) is False


# ---------------------------------------------------------------------------
# event_matches — AND / OR combinations
# ---------------------------------------------------------------------------


class TestEventMatchesLogical:
    def _session_eq(self, sid: str) -> Predicate:
        return Predicate(
            left=FieldRef(name="session_id"),
            op=Op.EQ,
            right=Value(value=sid),
        )

    def _class_eq(self, cls: str) -> Predicate:
        return Predicate(
            left=FieldRef(name="class"),
            op=Op.EQ,
            right=Value(value=cls),
        )

    def test_and_both_true(self) -> None:
        pred = Predicate(
            left=self._session_eq("sess-abc"),
            op=Op.AND,
            right=self._class_eq("tool_call"),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(session_id="sess-abc", class_=TapEventClass.TOOL_CALL)) is True

    def test_and_left_false(self) -> None:
        pred = Predicate(
            left=self._session_eq("sess-xyz"),
            op=Op.AND,
            right=self._class_eq("tool_call"),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(session_id="sess-abc", class_=TapEventClass.TOOL_CALL)) is False

    def test_and_right_false(self) -> None:
        pred = Predicate(
            left=self._session_eq("sess-abc"),
            op=Op.AND,
            right=self._class_eq("user_input"),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(session_id="sess-abc", class_=TapEventClass.TOOL_CALL)) is False

    def test_or_both_false(self) -> None:
        pred = Predicate(
            left=self._session_eq("sess-xyz"),
            op=Op.OR,
            right=self._class_eq("user_input"),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(session_id="sess-abc", class_=TapEventClass.TOOL_CALL)) is False

    def test_or_one_true(self) -> None:
        pred = Predicate(
            left=self._session_eq("sess-abc"),
            op=Op.OR,
            right=self._class_eq("user_input"),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(session_id="sess-abc", class_=TapEventClass.TOOL_CALL)) is True

    def test_nested_and_or(self) -> None:
        # (session_id=sess-abc AND class=tool_call) OR class=graph_transition
        inner = Predicate(
            left=self._session_eq("sess-abc"),
            op=Op.AND,
            right=self._class_eq("tool_call"),
        )
        pred = Predicate(
            left=inner,
            op=Op.OR,
            right=self._class_eq("graph_transition"),
        )
        sel = TapSelector(events=pred)
        # matches via inner AND
        assert event_matches(sel, _make_event(session_id="sess-abc", class_=TapEventClass.TOOL_CALL)) is True
        # matches via right OR
        assert event_matches(sel, _make_event(session_id="sess-xyz", class_=TapEventClass.GRAPH_TRANSITION)) is True
        # no match
        assert event_matches(sel, _make_event(session_id="sess-xyz", class_=TapEventClass.TOOL_CALL)) is False


# ---------------------------------------------------------------------------
# event_matches — payload dotted path resolution
# ---------------------------------------------------------------------------


class TestEventMatchesPayload:
    def test_payload_key_eq(self) -> None:
        pred = Predicate(
            left=FieldRef(name="payload.tool"),
            op=Op.EQ,
            right=Value(value="bash"),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(payload={"tool": "bash"})) is True

    def test_payload_key_eq_no_match(self) -> None:
        pred = Predicate(
            left=FieldRef(name="payload.tool"),
            op=Op.EQ,
            right=Value(value="python"),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(payload={"tool": "bash"})) is False

    def test_payload_missing_key_is_none(self) -> None:
        pred = Predicate(
            left=FieldRef(name="payload.missing"),
            op=Op.IS_NULL,
            right=Value(value=None),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(payload={})) is True


# ---------------------------------------------------------------------------
# event_matches — unknown field raises ValueError
# ---------------------------------------------------------------------------


class TestEventMatchesUnknownField:
    def test_unknown_field_raises(self) -> None:
        pred = Predicate(
            left=FieldRef(name="nonexistent_field"),
            op=Op.EQ,
            right=Value(value="x"),
        )
        sel = TapSelector(events=pred)
        with pytest.raises(ValueError, match="nonexistent_field"):
            event_matches(sel, _make_event())


# ---------------------------------------------------------------------------
# IS_NULL / IS_NOT_NULL
# ---------------------------------------------------------------------------


class TestEventMatchesNullOps:
    def test_is_null_agent_id_none(self) -> None:
        pred = Predicate(
            left=FieldRef(name="agent_id"),
            op=Op.IS_NULL,
            right=Value(value=None),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(agent_id=None)) is True

    def test_is_null_agent_id_not_none(self) -> None:
        pred = Predicate(
            left=FieldRef(name="agent_id"),
            op=Op.IS_NULL,
            right=Value(value=None),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(agent_id="agent-1")) is False

    def test_is_not_null_agent_id(self) -> None:
        pred = Predicate(
            left=FieldRef(name="agent_id"),
            op=Op.IS_NOT_NULL,
            right=Value(value=None),
        )
        sel = TapSelector(events=pred)
        assert event_matches(sel, _make_event(agent_id="agent-1")) is True


# ---------------------------------------------------------------------------
# session_predicate_for_storage
# ---------------------------------------------------------------------------


class TestSessionPredicateForStorage:
    def test_no_user_sessions_predicate(self) -> None:
        """When selector.sessions is None, returns just workspace_id == wid."""
        sel = TapSelector(sessions=None)
        pred = session_predicate_for_storage("ws-1", sel)
        # Should be a simple EQ on workspace_id
        assert isinstance(pred, Predicate)
        assert pred.op == Op.EQ
        assert isinstance(pred.left, FieldRef)
        assert pred.left.name == "workspace_id"
        assert isinstance(pred.right, Value)
        assert pred.right.value == "ws-1"

    def test_with_user_sessions_predicate_ands(self) -> None:
        """When selector.sessions is set, result ANDs workspace_id with user pred."""
        user_pred = Predicate(
            left=FieldRef(name="status"),
            op=Op.EQ,
            right=Value(value="running"),
        )
        sel = TapSelector(sessions=user_pred)
        pred = session_predicate_for_storage("ws-1", sel)

        assert isinstance(pred, Predicate)
        assert pred.op == Op.AND

        # One side is workspace_id EQ, the other is the user predicate.
        # Pydantic BaseModel is not hashable, so compare by equality not set membership.
        ws_pred = Predicate(
            left=FieldRef(name="workspace_id"),
            op=Op.EQ,
            right=Value(value="ws-1"),
        )
        sides = [pred.left, pred.right]
        assert any(s == ws_pred for s in sides)
        assert any(s == user_pred for s in sides)

    def test_workspace_id_eq_present_with_user_pred(self) -> None:
        """workspace_id constraint is always scoped even when sessions is set."""
        user_pred = Predicate(
            left=FieldRef(name="agent_id"),
            op=Op.EQ,
            right=Value(value="agent-42"),
        )
        sel = TapSelector(sessions=user_pred)
        pred = session_predicate_for_storage("ws-99", sel)

        assert pred.op == Op.AND
        ws_side = pred.left if isinstance(pred.left, Predicate) and pred.left.op == Op.EQ else pred.right
        assert isinstance(ws_side, Predicate)
        assert isinstance(ws_side.left, FieldRef)
        assert ws_side.left.name == "workspace_id"
        assert isinstance(ws_side.right, Value)
        assert ws_side.right.value == "ws-99"
