"""Task D1 of docs/superpowers/plans/2026-07-05-chat-refactor.md — R2: fold
the attach control into the chatbox. <Composer>
(ui/components/chat/composer.jsx) used to render a standalone paperclip
button as the leftmost item in the input row (a separate bottom-left
control). This task drops that and renders the attach icon **at the right
end of the input box itself** — anchored inside the textarea's own wrapper,
adjacent to the Send/Stop control, not a standalone left column. Drag-and-drop
and paste onto the textarea now also trigger the same `onAttach` callback
<Conversation> already wires to `handleFilesPicked` (unchanged — the 8 MiB
cap in conversation.jsx is untouched by this task, which only modifies
composer.jsx).

Static-source + transpile-build checks only (the ui/ suite convention, e.g.
test_composer_schema_shells.py) — no DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHAT_DIR = UI / "components" / "chat"
COMPOSER = CHAT_DIR / "composer.jsx"
CONVERSATION = CHAT_DIR / "conversation.jsx"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_attach_control_is_anchored_inside_the_input_row_not_a_left_column() -> None:
    src = _src(COMPOSER)
    assert 'data-testid="chat-attach-btn"' in src

    # The old layout rendered the attach button BEFORE the textarea (a
    # standalone left column). The new layout renders it at the right end
    # of the input box — inside/after the textarea's own markup — so it
    # reads adjacent to Send/Stop within the same row, not ahead of it.
    textarea_idx = src.index("<textarea")
    attach_idx = src.index('data-testid="chat-attach-btn"')
    send_idx = src.index('data-testid="chat-send-btn"')
    stop_idx = src.index('data-testid="chat-stop-btn"')

    assert attach_idx > textarea_idx, (
        "attach button must no longer precede the textarea as a standalone left column"
    )
    assert attach_idx < send_idx and attach_idx < stop_idx, (
        "attach button must stay adjacent to the Send/Stop control in the same row"
    )


def test_attach_button_is_positioned_at_the_right_end_of_the_input_box() -> None:
    src = _src(COMPOSER)
    # The icon is overlaid on the textarea's own box (position: relative
    # wrapper + position: absolute icon anchored to the right), not just
    # another flex sibling in the row.
    assert 'position: "relative"' in src
    assert 'position: "absolute"' in src
    assert "right:" in src


def test_paste_and_drop_onto_the_textarea_trigger_attach() -> None:
    src = _src(COMPOSER)
    assert "onPaste" in src
    assert "onDrop" in src
    assert "onAttach" in src


def test_the_8mib_cap_constant_survives_the_refactor() -> None:
    # This task only modifies composer.jsx (Files: composer.jsx per the
    # plan) — handleFilesPicked + MAX_ATTACHMENT_BYTES stay in
    # conversation.jsx and are passed down as the `onAttach` prop, so the
    # size cap must still be intact there.
    src = _src(CONVERSATION)
    assert "MAX_ATTACHMENT_BYTES" in src
    assert "8 * 1024 * 1024" in src


def test_attachment_chips_are_still_reused() -> None:
    src = _src(COMPOSER)
    assert "CT_AttachmentChip" in src


def test_composer_still_pure_no_data_fetching_or_ws() -> None:
    src = _src(COMPOSER)
    assert "new WebSocket(" not in src
    assert "apiFetch" not in src


def test_bundle_transpiles_with_composer_attach_changes() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chat/composer.jsx === */" in text
