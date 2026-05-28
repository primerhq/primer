"""Tests for matrix.model.thread."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from primer.model.chat import TextPart, ToolCallPart, ToolResultPart
from primer.model.thread import Thread, ThreadMessage


# ---- Thread -----------------------------------------------------------------


class TestThread:
    def test_construction(self) -> None:
        t = Thread(
            id="thread-001",
            agent_id="researcher",
            title="Find slow tests",
            created_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
            last_activity_at=datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
        )
        assert t.id == "thread-001"
        assert t.agent_id == "researcher"
        assert t.title == "Find slow tests"

    def test_title_optional(self) -> None:
        t = Thread(
            id="t",
            agent_id="a",
            created_at=datetime.now(timezone.utc),
            last_activity_at=datetime.now(timezone.utc),
        )
        assert t.title is None

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Thread(
                id="",
                agent_id="a",
                created_at=datetime.now(timezone.utc),
                last_activity_at=datetime.now(timezone.utc),
            )

    def test_empty_agent_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Thread(
                id="t",
                agent_id="",
                created_at=datetime.now(timezone.utc),
                last_activity_at=datetime.now(timezone.utc),
            )

    def test_round_trip_through_json(self) -> None:
        original = Thread(
            id="t-1",
            agent_id="a-1",
            title="hello",
            created_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
            last_activity_at=datetime(2026, 5, 3, 1, tzinfo=timezone.utc),
        )
        parsed = Thread.model_validate_json(original.model_dump_json())
        assert parsed == original


class TestThreadMessage:
    def test_construction(self) -> None:
        m = ThreadMessage(
            id="tmsg-1",
            thread_id="t-1",
            role="user",
            parts=[TextPart(text="hello")],
            created_at=datetime.now(timezone.utc),
            sequence=0,
        )
        assert m.role == "user"
        assert isinstance(m.parts[0], TextPart)
        assert m.sequence == 0

    def test_negative_sequence_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ThreadMessage(
                id="m",
                thread_id="t",
                role="user",
                parts=[TextPart(text="x")],
                created_at=datetime.now(timezone.utc),
                sequence=-1,
            )

    def test_empty_parts_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ThreadMessage(
                id="m",
                thread_id="t",
                role="user",
                parts=[],
                created_at=datetime.now(timezone.utc),
                sequence=0,
            )

    def test_invalid_role_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ThreadMessage(
                id="m",
                thread_id="t",
                role="moderator",  # type: ignore[arg-type]
                parts=[TextPart(text="x")],
                created_at=datetime.now(timezone.utc),
                sequence=0,
            )

    def test_empty_thread_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ThreadMessage(
                id="m",
                thread_id="",
                role="user",
                parts=[TextPart(text="x")],
                created_at=datetime.now(timezone.utc),
                sequence=0,
            )

    def test_round_trip_with_text_part(self) -> None:
        m = ThreadMessage(
            id="m",
            thread_id="t",
            role="user",
            parts=[TextPart(text="hello")],
            created_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
            sequence=0,
        )
        parsed = ThreadMessage.model_validate_json(m.model_dump_json())
        assert parsed == m

    def test_round_trip_with_tool_call_and_result(self) -> None:
        m_call = ThreadMessage(
            id="ma",
            thread_id="t",
            role="assistant",
            parts=[ToolCallPart(id="c-1", name="search", arguments={"q": "X"})],
            created_at=datetime.now(timezone.utc),
            sequence=1,
        )
        m_result = ThreadMessage(
            id="mr",
            thread_id="t",
            role="tool",
            parts=[ToolResultPart(id="c-1", output="hit", error=False)],
            created_at=datetime.now(timezone.utc),
            sequence=2,
        )
        for original in (m_call, m_result):
            parsed = ThreadMessage.model_validate_json(original.model_dump_json())
            assert parsed == original
