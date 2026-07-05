"""Task F3 of docs/superpowers/plans/2026-07-05-chat-refactor.md —
R4: a small rewind icon at the end of each user message
(ui/components/chat/transcript.jsx), gated by the compaction boundary
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
CONVERSATION = CHAT_DIR / "conversation.jsx"
TRANSCRIPT = CHAT_DIR / "transcript.jsx"


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
    assert "window.confirm(" in src
    assert "if (!ok) return;" in src


def test_rewind_is_gated_by_the_compaction_boundary() -> None:
    # Not rendered at/behind the last compaction_marker seq — the boundary
    # is computed in <Conversation> (highest compaction_marker seq) and
    # passed down as `compactionBoundarySeq`, then compared against the
    # STRICT ">" the plan calls for (a rewind AT the marker is illegal too
    # — mirrors A7's own `seq <= marker seq` 422 guard).
    conv_src = _src(CONVERSATION)
    assert "compactionBoundarySeq" in conv_src
    assert 'm.kind === "compaction_marker"' in conv_src

    transcript_src = _src(TRANSCRIPT)
    assert "compactionBoundarySeq" in transcript_src
    assert "m.seq > (compactionBoundarySeq || 0)" in transcript_src


def test_rewind_posts_the_rewind_endpoint_with_the_message_seq() -> None:
    src = _src(CONVERSATION)
    assert "`/chats/${encodeURIComponent(cid)}/rewind`" in src
    assert 'apiFetch("POST", `/chats/${encodeURIComponent(cid)}/rewind`, { seq })' in src
    # Success truncates the local message list to the kept seq (or a
    # refetch) — this implementation truncates, per the plan's "(or
    # refetch)" allowance.
    assert "truncated_to_seq" in src
    assert "setMessages((prev) => prev.filter(" in src


def test_rewind_disabled_while_a_turn_is_running() -> None:
    # Mirrors A7's own 409-while-running guard exactly (turn_status ===
    # "running") rather than the broader claimable-or-running
    # turnInFlight used for Send/Stop.
    src = _src(TRANSCRIPT)
    assert 'const rewindDisabled = turnStatus === "running";' in src
    assert "disabled={rewindDisabled}" in src
    assert "if (disabled || typeof onRewind !== \"function\") return;" in src


def test_conversation_wires_on_rewind_and_boundary_into_transcript() -> None:
    src = _src(CONVERSATION)
    assert "onRewind={handleRewind}" in src
    assert "compactionBoundarySeq={compactionBoundarySeq}" in src


def test_bundle_transpiles_with_rewind_changes() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chat/conversation.jsx === */" in text
    assert "/* === components/chat/transcript.jsx === */" in text
