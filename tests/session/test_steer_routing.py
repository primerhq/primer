"""Steer routing and title derivation (S1 P1, plan Task 5).

Spec: docs/superpowers/ux-revamp/02-s1-design.md section 4 (M5 rule).
"""

import json
from datetime import UTC, datetime

from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.steer_routing import ROUTE_PENDING, ROUTE_WAKE, route_steer
from primer.session.title import derive_session_title


def _row(**overrides) -> WorkspaceSession:
    base = {
        "id": "s",
        "workspace_id": "w",
        "binding": AgentSessionBinding(agent_id="a"),
        "status": SessionStatus.WAITING,
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return WorkspaceSession(**base)


class TestRouteSteer:
    def test_running_turn_routes_to_pending(self):
        row = _row(status=SessionStatus.RUNNING, turn_status="running")
        assert route_steer(row, raw_lines=[]) == ROUTE_PENDING

    def test_claimable_turn_routes_to_pending(self):
        row = _row(turn_status="claimable")
        assert route_steer(row, raw_lines=[]) == ROUTE_PENDING

    def test_parked_routes_to_pending(self):
        """A parked session is waiting on a human, not finished."""
        row = _row(turn_status="idle", parked_status="parked")
        assert route_steer(row, raw_lines=[]) == ROUTE_PENDING

    def test_idle_routes_to_wake(self):
        assert route_steer(_row(turn_status="idle"), raw_lines=[]) == ROUTE_WAKE

    def test_running_status_alone_does_not_defer(self):
        """status is coarser than turn_status: RUNNING sits idle between
        turns, so deferring on it would delay steers that should run."""
        row = _row(status=SessionStatus.RUNNING, turn_status="idle")
        assert route_steer(row, raw_lines=[]) == ROUTE_WAKE

    def test_idle_row_with_open_log_turn_routes_to_pending(self):
        """Callers holding the log get the extra check."""
        row = _row(status=SessionStatus.WAITING, turn_status="idle")
        lines = [json.dumps({"seq": 1, "kind": "user_input", "payload": {},
                             "created_at": "2026-08-16T00:00:00+00:00"})]
        assert route_steer(row, raw_lines=lines) == ROUTE_PENDING

    def test_raw_lines_is_optional(self):
        """The hot API path routes on the row alone."""
        assert route_steer(_row(turn_status="idle")) == ROUTE_WAKE

    def test_cursor_excludes_already_drained_turns(self):
        """Records before the checkpoint belong to finished turns."""
        row = _row(turn_status="idle", next_unprocessed_seq=5)
        lines = [json.dumps({"seq": 1, "kind": "user_input", "payload": {},
                             "created_at": "2026-08-16T00:00:00+00:00"})]
        assert route_steer(row, raw_lines=lines) == ROUTE_WAKE


class TestDeriveSessionTitle:
    """Ported verbatim from _derive_chat_title (chat/executor.py:132)."""

    class _Part:
        def __init__(self, text):
            self.text = text

    def test_title_from_first_text_part(self):
        title = derive_session_title([self._Part("Fix the flaky login test")])
        assert title == "Fix the flaky login test"

    def test_whitespace_is_collapsed(self):
        assert derive_session_title([self._Part("a\n\n  b")]) == "a b"

    def test_long_title_trims_on_a_word_boundary_with_ellipsis(self):
        text = "word " * 40
        title = derive_session_title([self._Part(text)])
        assert len(title) <= 80
        assert title.endswith("…")
        assert not title.endswith("wor…")  # never snaps a word in half

    def test_skips_empty_and_non_text_parts(self):
        parts = [self._Part(None), self._Part("   "), self._Part("real")]
        assert derive_session_title(parts) == "real"

    def test_binary_only_turn_falls_back(self):
        assert derive_session_title([self._Part(None)]) == "[attachment]"
        assert derive_session_title([]) == "[attachment]"
