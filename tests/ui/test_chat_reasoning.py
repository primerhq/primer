"""Reasoning is streamed, persisted, and rendered as a collapsed bar.

Reasoning is usually far longer than the answer it precedes and is rarely
what the operator came to read, so it folds away -- but it must be visible
that the model reasoned at all.

Static-source checks, matching the rest of the ui/ suite.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPT = ROOT / "ui" / "components" / "chat" / "transcript.jsx"
HELPERS = ROOT / "ui" / "components" / "chat" / "use-transcript.js"


class TestCoalescing:
    def test_reasoning_rows_coalesce_into_one_block(self) -> None:
        src = HELPERS.read_text(encoding="utf-8")
        assert 'kind: "reasoning_block"' in src
        assert 'if (m.kind === "reasoning")' in src

    def test_reasoning_is_a_separate_run_from_assistant_text(self) -> None:
        """Merging them would put thinking text inside the answer bubble."""
        src = HELPERS.read_text(encoding="utf-8")
        assert "flushReasoning" in src
        # An open assistant buffer is flushed when reasoning starts, since
        # reasoning precedes the answer it explains.
        reasoning_branch = src.split('if (m.kind === "reasoning")', 1)[1][:400]
        assert "flushBuffer();" in reasoning_branch

    def test_both_runs_flush_at_the_end(self) -> None:
        src = HELPERS.read_text(encoding="utf-8")
        tail = src.split("for (const m of messages)", 1)[1]
        assert "flushReasoning();" in tail and "flushBuffer();" in tail

    def test_whitespace_only_reasoning_is_dropped(self) -> None:
        src = HELPERS.read_text(encoding="utf-8")
        block = src.split("const flushReasoning", 1)[1][:250]
        assert "trim().length > 0" in block


class TestRendering:
    def test_reasoning_block_has_a_render_branch(self) -> None:
        src = TRANSCRIPT.read_text(encoding="utf-8")
        assert 'kind === "reasoning_block"' in src
        assert "CT_ReasoningBlock" in src

    def test_collapsed_by_default(self) -> None:
        src = TRANSCRIPT.read_text(encoding="utf-8")
        block = src.split("function CT_ReasoningBlock", 1)[1][:400]
        assert "React.useState(false)" in block, "must start folded"

    def test_toggle_is_accessible(self) -> None:
        src = TRANSCRIPT.read_text(encoding="utf-8")
        block = src.split("function CT_ReasoningBlock", 1)[1][:1600]
        assert "aria-expanded={open}" in block
        assert 'data-testid="chat-reasoning-toggle"' in block
        assert 'type="button"' in block

    def test_rendered_as_pre_not_markdown(self) -> None:
        """Reasoning is a raw think-aloud stream; markdown would mangle it."""
        block = TRANSCRIPT.read_text(encoding="utf-8").split(
            "function CT_ReasoningBlock", 1
        )[1][:1600]
        assert "<pre" in block
        assert "CT_MarkdownBody" not in block

    def test_empty_reasoning_renders_nothing(self) -> None:
        block = TRANSCRIPT.read_text(encoding="utf-8").split(
            "function CT_ReasoningBlock", 1
        )[1][:400]
        assert "if (!text.trim()) return null;" in block
