"""Unit tests for the WS envelope encoders (spec §6.4).

Pure-function tests against ``_compaction_envelope`` /
``_usage_envelope`` / ``_message_to_wire`` — no FastAPI test client,
no WebSocket connection. The end-to-end WS verification belongs to
the e2e journey suite (T14.1).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.api.routers.chats import (
    _compaction_envelope,
    _message_to_wire,
    _usage_envelope,
)
from primer.chat.usage_cache import reset_cache, set_usage
from primer.model.chats import ChatMessage


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_cache()


class TestCompactionEnvelope:
    def test_translates_marker_row_to_compaction_envelope(self) -> None:
        row = ChatMessage(
            id=ChatMessage.make_id("c1", 48),
            chat_id="c1",
            seq=48,
            kind="compaction_marker",
            payload={
                "summary": "test summary",
                "tokens_before": 7820,
                "tokens_after": 1180,
                "replaced_from_seq": 1,
                "replaced_to_seq": 47,
                "model": "gpt-4o",
                "compaction_prompt_source": "default",
                "created_at": "2026-05-30T14:30:00Z",
            },
            created_at=datetime.now(timezone.utc),
        )
        env = _compaction_envelope(row)
        assert env == {
            "kind": "compaction",
            "seq": 48,
            "summary": "test summary",
            "tokens_before": 7820,
            "tokens_after": 1180,
            "replaced_from_seq": 1,
            "replaced_to_seq": 47,
        }

    def test_missing_payload_keys_default_to_safe_values(self) -> None:
        row = ChatMessage(
            id=ChatMessage.make_id("c1", 9),
            chat_id="c1",
            seq=9,
            kind="compaction_marker",
            payload={},
            created_at=datetime.now(timezone.utc),
        )
        env = _compaction_envelope(row)
        assert env["summary"] == ""
        assert env["tokens_before"] == 0
        assert env["tokens_after"] == 0
        assert env["replaced_from_seq"] is None
        assert env["replaced_to_seq"] is None


class TestUsageEnvelope:
    def test_zero_when_nothing_cached(self) -> None:
        env = _usage_envelope("c1", context_length=10_000)
        assert env["kind"] == "usage"
        assert env["seq"] is None
        assert env["input_tokens"] == 0
        assert env["output_tokens"] == 0
        assert env["context_length"] == 10_000
        assert env["used_pct"] == 0.0

    def test_reflects_cached_tokens(self) -> None:
        set_usage("c1", input_tokens=1234, output_tokens=56)
        env = _usage_envelope("c1", context_length=10_000)
        assert env["input_tokens"] == 1234
        assert env["output_tokens"] == 56
        assert env["used_pct"] == pytest.approx(0.1234)

    def test_zero_context_length_safe(self) -> None:
        set_usage("c1", input_tokens=100, output_tokens=10)
        env = _usage_envelope("c1", context_length=0)
        assert env["used_pct"] == 0.0  # no divide-by-zero


class TestMessageToWireRouting:
    def test_routes_compaction_marker_to_compaction_envelope(self) -> None:
        row = ChatMessage(
            id=ChatMessage.make_id("c1", 5),
            chat_id="c1",
            seq=5,
            kind="compaction_marker",
            payload={
                "summary": "rolled up",
                "tokens_before": 100,
                "tokens_after": 10,
                "replaced_from_seq": 1,
                "replaced_to_seq": 4,
            },
            created_at=datetime.now(timezone.utc),
        )
        wire = _message_to_wire(row)
        assert wire["kind"] == "compaction"
        assert wire["seq"] == 5
        assert wire["summary"] == "rolled up"

    def test_passes_non_compaction_rows_through(self) -> None:
        row = ChatMessage(
            id=ChatMessage.make_id("c1", 7),
            chat_id="c1",
            seq=7,
            kind="assistant_token",
            payload={"delta": "hello"},
            created_at=datetime.now(timezone.utc),
        )
        wire = _message_to_wire(row)
        assert wire == {"kind": "assistant_token", "seq": 7, "delta": "hello"}
