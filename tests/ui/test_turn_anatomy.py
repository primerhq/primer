"""Task C2 of docs/superpowers/plans/2026-07-05-chat-refactor.md —
optimistic echo, a tool-labeled live "thinking" state, and the
composer's Send/Stop control wired to the REST cancel endpoint (A6).

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_transcript_timeline.py / test_conversation_extracted.py) — no
DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHAT_DIR = UI / "components" / "chat"
SHARED_DIR = UI / "components" / "shared"
CONVERSATION = CHAT_DIR / "conversation.jsx"
TRANSCRIPT = SHARED_DIR / "transcript.jsx"
COMPOSER = SHARED_DIR / "composer.jsx"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_transcript_labels_the_live_state_by_running_tool() -> None:
    src = _src(TRANSCRIPT)
    assert 'lastRow.kind === "tool_call"' in src
    assert "`running ${runningToolName}" in src
    assert "<CT_ThinkingBubble label={thinkingLabel} />" in src


