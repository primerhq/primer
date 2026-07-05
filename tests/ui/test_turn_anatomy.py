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
CONVERSATION = CHAT_DIR / "conversation.jsx"
TRANSCRIPT = CHAT_DIR / "transcript.jsx"
COMPOSER = CHAT_DIR / "composer.jsx"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_optimistic_echo_builds_a_pending_client_id_row_on_send() -> None:
    src = _src(CONVERSATION)
    assert "pending: true" in src, "the synthetic echo row must be marked `pending: true`"
    assert "clientId" in src, "the optimistic row needs a client id to key/dedupe safely"
    # Pushed from onSubmitComposer, only after a confirmed send (keeps the
    # existing "keep text on failed send" behavior — no send, no echo).
    assert "const sent = sendMessage(text, atts);" in src
    assert "if (sent) {" in src


def test_optimistic_echo_is_reconciled_in_place_not_appended_as_a_duplicate() -> None:
    src = _src(CONVERSATION)
    # Reconciliation happens in the WS onmessage persisted-row branch:
    # find the pending placeholder (same text; oldest pending as
    # fallback) and swap it for the real row rather than pushing a
    # second row — storage stays truth, seq-dedup is untouched.
    assert 'p.pending && p.kind === "user_message"' in src
    assert "next[pendingIdx] = msg;" in src


def test_stop_control_posts_the_cancel_endpoint_and_swallows_409() -> None:
    src = _src(CONVERSATION)
    assert "`/chats/${encodeURIComponent(cid)}/cancel`" in src
    assert 'await apiFetch("POST",' in src
    assert 'err?.status === 409' in src
    assert "running={turnInFlight}" in src
    assert "onStop={handleStop}" in src


def test_composer_running_prop_is_driven_by_turn_status_not_hardcoded() -> None:
    src = _src(CONVERSATION)
    assert 'chatRow?.turn_status === "claimable" || chatRow?.turn_status === "running"' in src
    # The old inert placeholders from Task B4 must be gone.
    assert "onStop={() => {}}" not in src
    assert "running={false}" not in src


def test_transcript_labels_the_live_state_by_running_tool() -> None:
    src = _src(TRANSCRIPT)
    assert 'lastRow.kind === "tool_call"' in src
    assert "`running ${runningToolName}" in src
    assert "<CT_ThinkingBubble label={thinkingLabel} />" in src


def test_bundle_transpiles_with_turn_anatomy_changes() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chat/conversation.jsx === */" in text
    assert "/* === components/chat/transcript.jsx === */" in text
    assert "/* === components/chat/composer.jsx === */" in text
