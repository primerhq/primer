"""compaction_marker is a valid ChatMessageKind and round-trips JSON."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from primer.model.chats import ChatMessage


def _now() -> datetime:
    return datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)


class TestCompactionMarkerKind:
    def test_kind_literal_includes_compaction_marker(self) -> None:
        msg = ChatMessage(
            id=ChatMessage.make_id("chat-1", 1),
            chat_id="chat-1",
            seq=1,
            kind="compaction_marker",
            payload={"summary": "test", "tokens_before": 10, "tokens_after": 2},
            created_at=_now(),
        )
        assert msg.kind == "compaction_marker"

    def test_round_trip_via_json(self) -> None:
        msg = ChatMessage(
            id=ChatMessage.make_id("chat-1", 2),
            chat_id="chat-1",
            seq=2,
            kind="compaction_marker",
            payload={"summary": "s"},
            created_at=_now(),
        )
        revived = ChatMessage.model_validate_json(msg.model_dump_json())
        assert revived.kind == "compaction_marker"
        assert revived.payload == {"summary": "s"}
        assert revived.chat_id == "chat-1"
        assert revived.seq == 2

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatMessage(
                id=ChatMessage.make_id("chat-1", 3),
                chat_id="chat-1",
                seq=3,
                kind="not_a_real_kind",  # type: ignore[arg-type]
                payload={},
                created_at=_now(),
            )
