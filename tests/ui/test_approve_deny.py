"""Task C4 of docs/superpowers/plans/2026-07-05-chat-refactor.md — inline
Approve/Deny on a gated (approval-mode) tool call. A chat-surface approval
gate (primer/chat/executor.py::soft_yield, tool_name == "_approval") appends
an assistant prompt ("Approve? (yes/no)") and stamps
`chat.pending_tool_call = {mode: "approval", ...}` on the chat row; the gate
blocks further turn activity so that prompt is always the LAST timeline row
while the gate holds. <Transcript> renders inline Approve/Deny right after
the message list when `pendingToolCall.mode === "approval"`; Approve/Deny
send the literal tokens "yes"/"no" through the SAME `sendMessage`
<Conversation> already uses for composer sends (no protocol change —
`resume_pending` parses those tokens).

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_tool_rendering.py / test_turn_anatomy.py) — no DOM/browser
harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHAT_DIR = UI / "components" / "chat"
CONVERSATION = CHAT_DIR / "conversation.jsx"
TRANSCRIPT = CHAT_DIR / "transcript.jsx"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_conversation_threads_send_message_into_transcript() -> None:
    src = _src(CONVERSATION)
    assert "sendMessage={sendMessage}" in src


def test_transcript_renders_gate_only_in_approval_mode() -> None:
    src = _src(TRANSCRIPT)
    assert 'pendingToolCall.mode === "approval"' in src
    assert "gateAwaitingApproval" in src
    assert "<CT_ApprovalGate sendMessage={sendMessage} />" in src


def test_approve_and_deny_send_the_conventional_yes_no_tokens() -> None:
    src = _src(TRANSCRIPT)
    # No protocol change: plain conversational "yes"/"no" through the
    # existing sendMessage, same one <Conversation> uses for composer sends.
    assert 'decide("no")' in src
    assert 'decide("yes")' in src
    assert 'sendMessage(text)' in src
    assert "data-testid=\"chat-gate-approve\"" in src
    assert "data-testid=\"chat-gate-deny\"" in src


def test_gate_buttons_disable_while_a_send_is_in_flight() -> None:
    src = _src(TRANSCRIPT)
    assert "disabled={sending}" in src
    assert "setSending(true)" in src
    # A failed enqueue (WS not open) re-enables so the operator can retry —
    # the gate otherwise only vanishes once <Conversation>'s polled
    # pending_tool_call goes back to null.
    assert "if (!enqueued) setSending(false)" in src


def test_bundle_transpiles_with_approve_deny_changes() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chat/conversation.jsx === */" in text
    assert "/* === components/chat/transcript.jsx === */" in text
