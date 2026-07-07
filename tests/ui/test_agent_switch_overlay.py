"""C3/C4 (fix/studio-ux, PR #114) — the /chats agent selector overlay.

Replaces <CT_AgentSwitcher>'s old cramped inline `.popover` (a 300px
absolutely-positioned box with a paged Prev/Next list) with a centered,
command-palette-style modal modelled on StudioCommandPalette / QuickOpen
(ui/components/studio-palette.jsx):

  C3 — dimmed backdrop, ~520px card, large autofocused search, a
        scrollable id+description results list with a "current" marker,
        ↑/↓/Enter/Esc keyboard nav, click-backdrop-to-close. The header
        trigger button stays (shows the current agent) but now OPENS the
        overlay. The switch mutation (POST /chats/{id}/agent) + its
        success/error toasts, the search filter, `disabled` when the chat
        is ended, and the current-agent guard are all preserved.
  C4 — a ⌘/Ctrl+Shift+A shortcut (⌘K is the global search — no collision)
        opens the overlay with the search focused while a chat is open.
        Listener added on mount / removed on unmount, preventDefault on
        match, single `open` source of truth, shortcut shown in the
        trigger's title.

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_studio_palette.py / test_agent_selector_top_right.py) — no
DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHATS = UI / "components" / "chats.jsx"
STYLES = UI / "styles.css"


def _chats_src() -> str:
    return CHATS.read_text(encoding="utf-8")


def _styles_src() -> str:
    return STYLES.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The old inline popover is gone; a centered overlay replaces it.
# ---------------------------------------------------------------------------


def test_old_inline_paged_popover_is_gone() -> None:
    src = _chats_src()
    # The old popover paged its list with Prev/Next + a `page + 1 / pages`
    # counter. None of that survives the overlay rewrite.
    assert "Prev" not in src
    assert "page + 1" not in src
    # The old absolutely-positioned box carried the literal `popover` class;
    # only the prose comment describing what it *replaced* may mention it.
    assert 'className="popover"' not in src


def test_overlay_backdrop_and_card_present() -> None:
    src = _chats_src()
    assert 'data-testid="agent-overlay"' in src
    assert "agent-overlay-backdrop" in src
    assert "agent-overlay-card" in src


def test_overlay_search_input_autofocused() -> None:
    src = _chats_src()
    assert 'data-testid="agent-overlay-search"' in src
    # autofocus: a ref on the input + a focus() call when the overlay opens.
    assert "inputRef" in src
    assert "inputRef.current.focus()" in src


def test_overlay_close_control_and_backdrop_click_closes() -> None:
    src = _chats_src()
    assert 'data-testid="agent-overlay-close"' in src
    # Clicking the backdrop closes; clicking the card does not bubble to it.
    assert 'className="agent-overlay-backdrop" data-testid="agent-overlay" onClick={closeOverlay}' in src
    assert "onClick={(e) => e.stopPropagation()}" in src


def test_overlay_list_shows_id_and_description_with_current_marker() -> None:
    src = _chats_src()
    assert 'data-testid="agent-overlay-item"' in src
    assert "agent-overlay-item-id" in src
    assert "agent-overlay-item-desc" in src
    # a.description drives the secondary line; a "current" marker flags the
    # active agent.
    assert "a.description" in src
    assert "agent-overlay-current" in src


# ---------------------------------------------------------------------------
# Keyboard navigation inside the overlay (↑/↓/Enter/Esc).
# ---------------------------------------------------------------------------


def test_overlay_keyboard_navigation() -> None:
    src = _chats_src()
    assert 'e.key === "ArrowDown"' in src
    assert 'e.key === "ArrowUp"' in src
    assert 'e.key === "Enter"' in src
    assert 'e.key === "Escape"' in src


# ---------------------------------------------------------------------------
# C4 — ⌘/Ctrl+Shift+A global shortcut.
# ---------------------------------------------------------------------------


def test_shortcut_combo_is_cmd_ctrl_shift_a() -> None:
    src = _chats_src()
    # metaKey || ctrlKey, plus shiftKey, plus the "A" key — and NOT ⌘K
    # (⌘K is the global search; the combo must not fire on a bare cmd+a).
    assert "(e.metaKey || e.ctrlKey) && e.shiftKey" in src
    assert 'e.code === "KeyA"' in src


def test_shortcut_listener_added_on_mount_removed_on_unmount() -> None:
    src = _chats_src()
    assert 'window.addEventListener("keydown", onKey)' in src
    assert 'window.removeEventListener("keydown", onKey)' in src


def test_shortcut_prevent_defaults_on_match() -> None:
    src = _chats_src()
    # The matched combo must preventDefault so the browser doesn't select-all.
    assert "e.preventDefault();" in src


def test_shortcut_shown_in_trigger_title() -> None:
    src = _chats_src()
    assert "Switch agent" in src
    assert "Shift + A" in src


def test_open_is_single_source_of_truth() -> None:
    src = _chats_src()
    # One `open` state drives both the trigger button and the shortcut.
    assert "const [open, setOpen] = React.useState(false);" in src
    # Trigger opens it; the shortcut sets the same state.
    assert "const openOverlay = () => { if (!disabled) setOpen(true); };" in src


# ---------------------------------------------------------------------------
# Preserved behavior: search filter, switch mutation + toasts, guards.
# ---------------------------------------------------------------------------


def test_search_filter_preserved() -> None:
    src = _chats_src()
    # Filters over id + description, case-insensitively.
    assert "a.id" in src and "a.description" in src
    assert "toLowerCase().includes(q.toLowerCase())" in src


def test_switch_mutation_and_toasts_preserved() -> None:
    src = _chats_src()
    assert 'apiFetch("POST", `/chats/${chatId}/agent`, { agent_id: agentId })' in src
    assert 'title: "Agent switched"' in src
    assert '"Switch failed"' in src


def test_current_agent_guard_preserved() -> None:
    src = _chats_src()
    # choose() no-ops on the current agent (and while a switch is in flight).
    assert "a.id === currentAgentId" in src


def test_disabled_when_chat_ended() -> None:
    src = _chats_src()
    # The trigger is disabled and the open helper is guarded when the chat
    # is ended; ChatDetail passes disabled={chatStatus === "ended"}.
    assert "disabled={disabled}" in src
    assert "if (!disabled) setOpen(true)" in src
    assert 'disabled={chatStatus === "ended"}' in src


# ---------------------------------------------------------------------------
# CSS: the overlay + cluster styles live at the end of ui/styles.css.
# ---------------------------------------------------------------------------


def test_overlay_and_cluster_css_present() -> None:
    css = _styles_src()
    for selector in (
        ".chat-header-cluster",
        ".agent-overlay-backdrop",
        ".agent-overlay-card",
        ".agent-overlay-input",
        ".agent-overlay-item",
        ".agent-overlay-current",
    ):
        assert selector in css, f"missing CSS for `{selector}`"


# ---------------------------------------------------------------------------
# Transpile.
# ---------------------------------------------------------------------------


def test_bundle_still_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chats.jsx === */" in text
