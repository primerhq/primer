"""Default compaction prompt content checks."""

from __future__ import annotations

from primer.agent.prompts import DEFAULT_COMPACTION_PROMPT


class TestDefaultCompactionPrompt:
    def test_mentions_user_goal(self) -> None:
        assert "user's stated goal" in DEFAULT_COMPACTION_PROMPT

    def test_mentions_pending_tool_calls(self) -> None:
        lowered = DEFAULT_COMPACTION_PROMPT.lower()
        assert "tool" in lowered and "pending" in lowered

    def test_no_headers_or_bullets_directive(self) -> None:
        assert "no headers" in DEFAULT_COMPACTION_PROMPT
