"""ReasoningDelta is persisted for display but never replayed as prompt.

Feeding a model its own prior reasoning back as context is either rejected
outright (providers that scope thinking blocks to the turn that produced
them) or degrades the next answer, so ``reasoning`` rows are written to
storage and skipped when the prompt is rebuilt.
"""

from __future__ import annotations

import pytest

from primer.model.chats import ChatMessageKind


def test_reasoning_is_a_recognised_chat_message_kind() -> None:
    assert "reasoning" in ChatMessageKind.__args__


class TestExecutorPersistsReasoning:
    def test_reasoning_delta_is_handled_not_ignored(self) -> None:
        """Pins the branch: ReasoningDelta used to fall through to the
        silently-ignored set alongside StreamStart / MediaDelta."""
        from pathlib import Path

        src = Path("primer/chat/executor.py").read_text(encoding="utf-8")
        assert "if isinstance(event, ReasoningDelta):" in src
        assert 'kind="reasoning"' in src
        # ...and is no longer listed among the ignored events.
        ignored = src.split("silently ignored", 1)[0][-400:]
        assert "ReasoningDelta" not in ignored.split("if isinstance(event, ReasoningDelta)")[-1]

    def test_history_rebuild_skips_reasoning(self) -> None:
        from pathlib import Path

        src = Path("primer/chat/executor.py").read_text(encoding="utf-8")
        assert 'if kind == "reasoning":' in src
        branch = src.split('if kind == "reasoning":', 1)[1][:600]
        assert "continue" in branch, "reasoning must not re-enter the prompt"


class TestWireShape:
    @pytest.mark.parametrize("field", ["delta"])
    def test_payload_carries_the_delta(self, field: str) -> None:
        from pathlib import Path

        src = Path("primer/chat/executor.py").read_text(encoding="utf-8")
        block = src.split("if isinstance(event, ReasoningDelta):", 1)[1][:400]
        assert f'"{field}": event.text' in block
