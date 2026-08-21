"""Task F3 of docs/superpowers/plans/2026-07-05-chat-refactor.md —
R4: a small rewind icon at the end of each user message
(ui/components/shared/transcript.jsx), gated by the compaction boundary
<Conversation> computes and passes down (ui/components/chat/
conversation.jsx), confirming before POSTing A7's truncation endpoint.

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_transcript_timeline.py / test_turn_anatomy.py) — no DOM/
browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHAT_DIR = UI / "components" / "chat"
SHARED_DIR = UI / "components" / "shared"
CONVERSATION = CHAT_DIR / "conversation.jsx"
TRANSCRIPT = SHARED_DIR / "transcript.jsx"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_rewind_affordance_exists_on_user_messages() -> None:
    src = _src(TRANSCRIPT)
    assert "function CT_RewindButton(" in src
    assert 'data-testid="chat-rewind-btn"' in src
    # Only rendered for user rows, never agent/tool/marker rows.
    assert "const canRewind = isUser && !isPending" in src
    assert "<CT_RewindButton" in src


def test_rewind_confirms_before_acting() -> None:
    src = _src(TRANSCRIPT)
    # confirmDialog, not the browser's confirm: a native modal cannot be
    # styled or tested, and a headless browser dismisses it on the app's
    # behalf, so the action behind it silently does nothing.
    assert "window.confirmDialog(" in src
    assert "window.confirm(" not in src
    assert "if (!ok) return;" in src


def test_rewind_disabled_while_a_turn_is_running() -> None:
    # Mirrors A7's own 409-while-running guard exactly (turn_status ===
    # "running") rather than the broader claimable-or-running
    # turnInFlight used for Send/Stop.
    src = _src(TRANSCRIPT)
    assert 'const rewindDisabled = turnStatus === "running";' in src
    assert "disabled={rewindDisabled}" in src
    assert "if (disabled || typeof onRewind !== \"function\") return;" in src


