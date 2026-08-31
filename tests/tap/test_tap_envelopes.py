"""Tap envelopes: usage, compaction and queued-steer state (S1 P4 T24).

Envelopes are DERIVED frames, not records. They are built from the
session row plus the log, never advance the tap cursor, and carry state
rather than deltas, so a reconnecting client re-derives the current
snapshot instead of replaying a historical one.
"""

import json
from datetime import UTC, datetime

from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
)
from primer.tap.event import TapEventClass


class TestClassInvariants:
    def test_every_record_kind_still_has_a_tap_class(self):
        """The subset invariant that CI caught once already."""
        smk = {k.value for k in SessionMessageKind}
        tec = {c.value for c in TapEventClass}
        assert smk.issubset(tec)

    def test_tap_only_classes_are_not_record_kinds(self):
        """Derived frames must never look like something the log wrote."""
        smk = {k.value for k in SessionMessageKind}
        assert TapEventClass.USAGE.value not in smk
        assert TapEventClass.PENDING_STEER.value not in smk


class TestAgentMarkerPassthrough:
    """Amendment m6: record and tap event are informationally identical,
    so the mapper stays 1:1 and this task adds no code for it."""

    def test_agent_marker_maps_one_to_one_with_its_payload(self):
        from primer.tap.event import record_to_tap_event

        payload = {
            "from_binding": {"kind": "agent", "agent_id": "a"},
            "to_binding": {"kind": "agent", "agent_id": "b"},
            "actor": "user",
            "binding_epoch": 3,
        }
        rec = SessionMessageRecord(
            seq=7, kind=SessionMessageKind.AGENT_MARKER,
            payload=payload, created_at=datetime.now(UTC),
        )
        ev = record_to_tap_event(
            rec, workspace_id="w", session_id="s",
            agent_id="a", graph_id=None, cursor="7",
        )
        assert ev.class_ is TapEventClass.AGENT_MARKER
        assert ev.payload["binding_epoch"] == 3
        assert ev.payload["to_binding"]["agent_id"] == "b"


class TestDerivedFrames:
    def _log(self):
        def rec(seq, kind, **payload):
            return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                               "created_at": "2026-08-17T00:00:00+00:00"})

        return [
            rec(1, "user_input", text="hi"),
            rec(2, "done", usage={"input_tokens": 100, "output_tokens": 10}),
        ]

    def test_usage_frame_carries_the_folded_totals(self):
        from primer.api.routers.tap import build_usage_frame

        frame = build_usage_frame(self._log())
        assert frame["turns"] == 1
        assert frame["total_input_tokens"] == 100
        assert frame["last_output_tokens"] == 10

    def test_usage_frame_is_idempotent(self):
        """Same input, same frame: a client may render it twice."""
        from primer.api.routers.tap import build_usage_frame

        assert build_usage_frame(self._log()) == build_usage_frame(self._log())

    def test_compaction_frame_derives_from_the_newest_visible_marker(self):
        """Reusing the existing class sidesteps the reader's deliberate
        skip of compaction_marker records."""
        from primer.api.routers.tap import build_compaction_frame

        def rec(seq, kind, **payload):
            return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                               "created_at": "2026-08-17T00:00:00+00:00"})

        assert build_compaction_frame([rec(1, "user_input")]) is None

        frame = build_compaction_frame([
            rec(1, "user_input"),
            rec(2, "compaction_marker", summary="folded", replaced_to_seq=1),
        ])
        assert frame["summary"] == "folded"
        assert frame["replaced_to_seq"] == 1
        assert frame["marker_seq"] == 2

    def test_pending_frame_lists_unrealized_steers_with_their_parts(self):
        from primer.api.routers.tap import build_pending_steer_frame
        from primer.model.workspace_session import PendingSessionMessage

        now = datetime.now(UTC)
        rows = [PendingSessionMessage(
            id="s:pending:1", session_id="s",
            parts=[{"type": "text", "text": "follow up"}],
            enqueued_at=now, created_at=now,
        )]
        frame = build_pending_steer_frame(rows)
        assert frame["count"] == 1
        assert frame["items"][0]["parts"][0]["text"] == "follow up"

    def test_pending_frame_is_empty_not_absent_when_the_queue_drains(self):
        """State, not deltas: the client needs to see it go to zero."""
        from primer.api.routers.tap import build_pending_steer_frame

        frame = build_pending_steer_frame([])
        assert frame["count"] == 0
        assert frame["items"] == []
