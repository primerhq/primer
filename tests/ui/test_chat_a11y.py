"""Task G1 of docs/superpowers/plans/2026-07-05-chat-refactor.md — Phase G:

* a11y: the transcript scroll region is `role="log"` + `aria-live="polite"`
  so streaming assistant text + new tool rows are announced; tool rows stay
  keyboard-operable (CT_ExpandableToolRow + the E1 inline previews already
  carry role="button"/tabIndex — locked in here so a future chip can't
  regress it silently).
* Connection legibility: a `turn_status` (idle/claimable/running) pill
  rendered alongside a WS-state pill, inline in <Transcript> itself, so the
  indicator travels with the component wherever it's embedded (not just the
  /chats page host's own header badge).
* Queue-on-reconnect: the composer buffers ("queues") a send while the
  socket is momentarily not open instead of hard-rejecting it; the queued
  frame flushes once the socket reopens.

Static-source + transpile-build checks only (the ui/ suite convention, e.g.
test_rewind_ui.py / test_turn_anatomy.py) — no DOM/browser harness.
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


# ---------------------------------------------------------------------------
# a11y: transcript is a live region; tool rows stay keyboard-operable.
# ---------------------------------------------------------------------------


def test_transcript_is_role_log_and_aria_live_polite() -> None:
    src = _src(TRANSCRIPT)
    assert 'role="log"' in src
    assert 'aria-live="polite"' in src


def test_tool_rows_and_media_chips_are_keyboard_operable() -> None:
    src = _src(TRANSCRIPT)
    # CT_ExpandableToolRow (tool_call/tool_result rows).
    assert 'role={hasExpand ? "button" : undefined}' in src
    assert 'tabIndex={hasExpand ? 0 : undefined}' in src
    # The E1 inline media chips (CT_ImagePreview / CT_PdfPreview) already
    # extend the same pattern to non-<button> clickable elements.
    assert src.count('role="button"') >= 2
    assert src.count('tabIndex={0}') >= 2
    assert 'onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ")' in src


# ---------------------------------------------------------------------------
# Connection legibility: turn_status indicator alongside the WS-state pill.
# ---------------------------------------------------------------------------


def test_connection_status_indicator_exists_and_is_wired() -> None:
    src = _src(TRANSCRIPT)
    assert "function CT_ConnectionStatus(" in src
    # Fix 1: the state pill also folds in the chat lifecycle, so <Transcript>
    # forwards `chatStatus` alongside wsState/turnStatus.
    assert (
        "<CT_ConnectionStatus wsState={wsState} turnStatus={turnStatus} chatStatus={chatStatus} />"
        in src
    )
    assert 'data-testid="chat-connection-status"' in src
    assert 'data-testid="chat-turn-status"' in src


def test_connection_status_state_pill_shows_ended_when_chat_ended() -> None:
    # Fix 1: the single state pill reads "ended" (pill-ended) when the chat
    # lifecycle is ended, otherwise the live turn status. `chatStatus` is an
    # optional prop so a bare Studio embed (no chatStatus) still shows turn.
    src = _src(TRANSCRIPT)
    assert "function CT_ConnectionStatus({ wsState, turnStatus, chatStatus })" in src
    assert 'const ended = chatStatus === "ended";' in src
    assert 'const stateLabel = ended ? "ended" : turn;' in src
    assert '"pill pill-ended"' in src


def test_connection_status_covers_every_turn_status_value() -> None:
    # primer/model/chats.py: Chat.turn_status is Literal["idle",
    # "claimable", "running"] — all three must map to a distinct pill so
    # this reads as "a real indicator", not a single static label.
    src = _src(TRANSCRIPT)
    status_idx = src.index("function CT_ConnectionStatus(")
    snippet = src[status_idx:status_idx + 1200]
    assert '"running"' in snippet
    assert '"claimable"' in snippet
    assert "turnStatus || \"idle\"" in snippet


# ---------------------------------------------------------------------------
# Queue-on-reconnect: buffer instead of hard-reject; flush on reopen.
# ---------------------------------------------------------------------------


def test_composer_accepts_ws_state_and_renders_a_queue_hint() -> None:
    src = _src(COMPOSER)
    assert "wsState" in src
    assert 'data-testid="chat-queue-hint"' in src


def test_send_is_never_hard_disabled_by_connection_state() -> None:
    # sendDisabled must stay driven only by disabled/schemaInvalid/empty
    # draft — connection state is surfaced via the hint above, never by
    # disabling Send itself (that's the whole point of "queues, does not
    # hard-disable").
    src = _src(COMPOSER)
    send_gate_line = next(line for line in src.splitlines() if "const sendDisabled" in line)
    assert "wsState" not in send_gate_line
    assert "wsNotOpen" not in send_gate_line


