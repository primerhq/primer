"""Unit tests for matrix.model.yield_ — the yielding-tool primitives.

Verifies the dataclasses' shape, JSON round-trip, and the
YieldToWorker exception construction. No I/O, no DB, no server.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from matrix.model.yield_ import (
    ToolContext,
    YieldCancelled,
    YieldTimeout,
    YieldToWorker,
    Yielded,
)


# ===========================================================================
# Yielded
# ===========================================================================


class TestYielded:
    def test_minimal_construction(self):
        y = Yielded(tool_name="sleep", event_key="timer:tc-1")
        assert y.tool_name == "sleep"
        assert y.event_key == "timer:tc-1"
        assert y.timeout is None
        assert y.resume_metadata == {}

    def test_full_construction(self):
        y = Yielded(
            tool_name="ask_user",
            event_key="ask_user:sess-1:tc-1",
            timeout=300.0,
            resume_metadata={"prompt": "Are you sure?"},
        )
        assert y.timeout == 300.0
        assert y.resume_metadata == {"prompt": "Are you sure?"}

    def test_frozen_dataclass_is_hashable(self):
        # Frozen dataclasses with hashable members are hashable; this
        # matters for use as dict keys / in sets when the worker
        # tracks active yields.
        y1 = Yielded(tool_name="sleep", event_key="timer:tc-1")
        y2 = Yielded(tool_name="sleep", event_key="timer:tc-1")
        # Same fields → equal (frozen dataclass __eq__).
        assert y1 == y2
        # But not hashable because resume_metadata is a dict (unhashable);
        # this is by design — the runtime stores Yielded by reference,
        # not in a set.
        with pytest.raises(TypeError):
            hash(y1)

    def test_json_roundtrip_minimal(self):
        original = Yielded(tool_name="sleep", event_key="timer:tc-1")
        round_tripped = Yielded.from_jsonable(original.to_jsonable())
        assert round_tripped == original

    def test_json_roundtrip_full(self):
        original = Yielded(
            tool_name="watch_files",
            event_key="watch:sess-1:tc-1",
            timeout=60.0,
            resume_metadata={
                "paths": ["a.txt", "b.txt"],
                "batch_window_ms": 250,
            },
        )
        round_tripped = Yielded.from_jsonable(original.to_jsonable())
        assert round_tripped == original

    def test_json_roundtrip_through_real_json(self):
        # In production the blob goes through postgres JSONB which
        # serialises to a JSON string and back, so the round-tripped
        # Yielded always owns fresh sub-objects. Verify this by
        # round-tripping through json.dumps/json.loads.
        import json
        original = Yielded(
            tool_name="watch_files",
            event_key="watch:sess-1:tc-1",
            timeout=60.0,
            resume_metadata={"paths": ["a.txt", "b.txt"]},
        )
        as_json = json.dumps(original.to_jsonable())
        rebuilt = Yielded.from_jsonable(json.loads(as_json))
        assert rebuilt == original
        # The rebuilt list is a fresh object — mutating it cannot
        # leak back to the original.
        rebuilt.resume_metadata["paths"].append("c.txt")
        assert original.resume_metadata["paths"] == ["a.txt", "b.txt"]

    def test_json_roundtrip_preserves_none_timeout(self):
        # None timeout is the "use global cap" signal; it must
        # survive the round-trip explicitly (not get coerced to a
        # default float).
        original = Yielded(tool_name="x", event_key="x:1", timeout=None)
        assert Yielded.from_jsonable(original.to_jsonable()).timeout is None


# ===========================================================================
# YieldTimeout
# ===========================================================================


class TestYieldTimeout:
    def test_carries_elapsed(self):
        t = YieldTimeout(elapsed_seconds=42.5)
        assert t.elapsed_seconds == 42.5

    def test_frozen(self):
        t = YieldTimeout(elapsed_seconds=1.0)
        # Pydantic-equivalent: frozen dataclass forbids attribute set.
        with pytest.raises(Exception):  # noqa: BLE001 — dataclass raises FrozenInstanceError
            t.elapsed_seconds = 2.0  # type: ignore[misc]


# ===========================================================================
# YieldCancelled
# ===========================================================================


class TestYieldCancelled:
    def test_with_reason(self):
        when = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
        c = YieldCancelled(
            reason="operator changed mind",
            cancelled_at=when,
            elapsed_seconds=120.0,
        )
        assert c.reason == "operator changed mind"
        assert c.cancelled_at == when
        assert c.elapsed_seconds == 120.0

    def test_without_reason(self):
        c = YieldCancelled(
            reason=None,
            cancelled_at=datetime.now(timezone.utc),
            elapsed_seconds=0.0,
        )
        assert c.reason is None


# ===========================================================================
# ToolContext
# ===========================================================================


class TestToolContext:
    def test_initial_call_no_parked_at(self):
        # On the initial call, the tool has never been parked yet —
        # parked_at is None. Tools use this to distinguish "first
        # invocation" from "resume invocation" if they ever needed
        # to (most don't — the resume hook is separate).
        ctx = ToolContext(
            tool_call_id="tc-1",
            session_id="sess-1",
            workspace_id="ws-1",
        )
        assert ctx.parked_at is None

    def test_resume_with_parked_at(self):
        ctx = ToolContext(
            tool_call_id="tc-1",
            session_id="sess-1",
            workspace_id="ws-1",
            parked_at=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        )
        assert ctx.parked_at is not None

    def test_chat_only_workspace_id_none(self):
        # Spec §4.4: chat-only invocations (M6) have workspace_id=None.
        ctx = ToolContext(
            tool_call_id="tc-1",
            session_id="chat-1",
            workspace_id=None,
        )
        assert ctx.workspace_id is None


# ===========================================================================
# YieldToWorker
# ===========================================================================


class TestYieldToWorker:
    def test_carries_yielded_and_tool_call_id(self):
        y = Yielded(tool_name="sleep", event_key="timer:tc-1")
        exc = YieldToWorker(y, tool_call_id="tc-1")
        assert exc.yielded is y
        assert exc.tool_call_id == "tc-1"

    def test_message_is_diagnostic(self):
        # The exception's __str__ should include the tool name +
        # event key + tool_call_id so a stray escape into the
        # error envelope path is human-readable.
        y = Yielded(tool_name="ask_user", event_key="ask_user:s:tc")
        exc = YieldToWorker(y, tool_call_id="tc")
        msg = str(exc)
        assert "ask_user" in msg
        assert "ask_user:s:tc" in msg
        assert "tc" in msg

    def test_is_exception_subclass(self):
        # Worker code uses bare `except Exception` in some places;
        # YieldToWorker MUST be catchable there (it's a control-flow
        # signal, not a true error, but it does need to escape the
        # LLM loop normally).
        y = Yielded(tool_name="x", event_key="x:1")
        with pytest.raises(Exception):  # noqa: BLE001
            raise YieldToWorker(y, tool_call_id="tc")
