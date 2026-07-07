"""Fix 1 + Fix 2 (fix/studio-debug-rail-polish, PR #119) — the /chats
ChatDetail desktop header.

  Fix 1 — all chat status (connection + turn/lifecycle) lives on the LEFT
          CT_ConnectionStatus inside the transcript; the desktop header's
          right side holds BUTTONS ONLY. The old right-side wsBadge + the
          standalone active/ended lifecycle pill are gone from the desktop
          header (they still render in the mobile kebab sheet — untouched).
  Fix 2 — the three cluster controls are green: circular icon buttons for the
          agent trigger + schema toggle, and a green pill for compact that
          carries the live token count. The shared <TokenMeter> component is
          NOT modified — the desktop cluster renders a purpose-built button.

Static-source + transpile-build checks only (the ui/ suite convention) — no
DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHATS = UI / "components" / "chats.jsx"
STYLES = UI / "styles.css"


def _chats() -> str:
    return CHATS.read_text(encoding="utf-8")


def _styles() -> str:
    return STYLES.read_text(encoding="utf-8")


def _desktop_header() -> str:
    # The desktop header is the `else` branch of ChatDetail's isMobile
    # ternary: `<div className="panel-h">` up to the <Conversation> mount.
    src = _chats()
    start = src.index('<div className="panel-h">')
    end = src.index("<Conversation\n", start)
    return src[start:end]


def _after_conversation() -> str:
    src = _chats()
    return src[src.index("<Conversation\n"):]


# ---------------------------------------------------------------------------
# Fix 1 — the desktop header right side is buttons only.
# ---------------------------------------------------------------------------


def test_desktop_header_no_longer_renders_ws_badge() -> None:
    assert "{wsBadge}" not in _desktop_header(), (
        "Fix 1: the desktop header right side must not render the wsBadge — "
        "connection status lives on the left CT_ConnectionStatus now"
    )


def test_desktop_header_no_longer_renders_lifecycle_status_pill() -> None:
    # The standalone active/ended lifecycle pill folded into the left state
    # pill; the desktop header must not render it anymore.
    assert 'chatStatus === "active"' not in _desktop_header()


def test_mobile_kebab_still_renders_ws_badge_and_lifecycle_pill() -> None:
    # Mobile is untouched — the wsBadge + lifecycle pill still render in the
    # BottomSheet kebab below the <Conversation> mount.
    after = _after_conversation()
    assert "{wsBadge}" in after
    assert 'chatStatus === "active"' in after


# ---------------------------------------------------------------------------
# Fix 2 — three green cluster controls.
# ---------------------------------------------------------------------------


def test_agent_trigger_is_a_green_circular_icon_button() -> None:
    src = _chats()
    # The trigger converted from the old text chip to a `.chat-cbtn` circular
    # icon button using the `agent` icon.
    assert 'data-testid="chat-agent-trigger"' in src
    assert 'className="chat-cbtn"' in src
    assert '<Icon name="agent"' in src
    # The old text-chip trigger ("agent <id> ▾") is gone.
    assert 'agent <span className="mono">{currentAgentId}</span>' not in src


def test_schema_toggle_is_a_green_circular_icon_button() -> None:
    header = _desktop_header()
    assert 'data-testid="chat-schema-panel-toggle"' in header
    # Green circular button, pressed/active when the panel is showing.
    assert '"chat-cbtn" + (showSchemaPanel ? " active" : "")' in header
    assert '<Icon name="settings"' in header
    # The old text chip label is gone from the toggle.
    assert "schema\n" not in header


def test_compact_button_is_a_green_pill_carrying_the_token_count() -> None:
    src = _chats()
    header = _desktop_header()
    assert 'data-testid="chat-compact-btn"' in header
    assert "chat-cbtn chat-cbtn-compact" in header
    assert '<Icon name="compress"' in header
    # The button carries the live token count: input / context (pct%).
    assert "compactInput.toLocaleString()" in src
    assert "compactCtx.toLocaleString()" in src
    assert "compactPct" in src


def test_compact_button_disable_and_action_logic_preserved() -> None:
    src = _chats()
    # Disabled when the chat is ended, a compaction is in flight, or the
    # socket is not open (mirrors the old TokenMeter compactDisabled logic).
    assert (
        'chatStatus === "ended" || convStatus.compactInFlight'
        ' || convStatus.wsState !== "open"'
    ) in src
    # Clicking triggers compaction.
    assert "convStatus.requestCompact()" in src


def test_shared_token_meter_component_untouched_and_still_used_by_mobile() -> None:
    # Fix 2 must NOT change the shared <TokenMeter> — the mobile kebab sheet
    # still renders it (only the desktop cluster swapped to a bespoke button).
    assert "<window.TokenMeter" in _after_conversation()


# ---------------------------------------------------------------------------
# CSS — the green button styles live at the end of ui/styles.css.
# ---------------------------------------------------------------------------


def test_green_button_css_present() -> None:
    css = _styles()
    assert ".chat-cbtn {" in css
    assert ".chat-cbtn-compact {" in css
    # Circular icon buttons + green accent.
    assert "border-radius: 50%" in css
    assert "var(--green)" in css


# ---------------------------------------------------------------------------
# Transpile.
# ---------------------------------------------------------------------------


def test_bundle_still_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    assert "/* === components/chats.jsx === */" in body.decode("utf-8")
