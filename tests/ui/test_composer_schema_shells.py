"""Task B4 of docs/superpowers/plans/2026-07-05-chat-refactor.md —
extract two shells out of <Conversation> (ui/components/chat/conversation.jsx):

* <Composer> (ui/components/chat/composer.jsx) — the input surface
  (textarea + attachment strip + send control), moved verbatim out of
  the inline JSX that used to live in <Conversation>. Wires the
  Send/Stop context-aware control (`running ? Stop : Send`) and the
  `disabled || schemaInvalid` send-gate (a hook for Task F2). Slash
  commands / mention autocomplete land in Task D1-D3 — the shell only
  needs to accept the props, not implement the behavior yet.
* <SchemaPanel> (ui/components/chat/schema-panel.jsx) — a collapsible
  right panel shell with a [Builder|JSON] tab strip + placeholder
  body, collapsed by default. Builder/JSON bodies land in Task F2.

Static-source + transpile-build checks only (the ui/ suite convention,
e.g. test_conversation_extracted.py / test_transcript_extracted.py) —
no DOM/browser harness.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHAT_DIR = UI / "components" / "chat"
COMPOSER = CHAT_DIR / "composer.jsx"
SCHEMA_PANEL = CHAT_DIR / "schema-panel.jsx"
CONVERSATION = CHAT_DIR / "conversation.jsx"
CHATS = UI / "components" / "chats.jsx"
INDEX = UI / "index.html"


def _order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


# ---------------------------------------------------------------------------
# <Composer>
# ---------------------------------------------------------------------------


def test_composer_module_exists_and_exports() -> None:
    assert COMPOSER.exists(), "ui/components/chat/composer.jsx is missing"
    src = COMPOSER.read_text(encoding="utf-8")
    assert "function Composer(" in src
    assert "window.Composer = Composer;" in src


def test_composer_accepts_the_documented_props() -> None:
    src = COMPOSER.read_text(encoding="utf-8")
    for prop in (
        "value",
        "onChange",
        "onSend",
        "onStop",
        "running",
        "disabled",
        "attachments",
        "onAttach",
        "onRemoveAttachment",
        "slashCommands",
        "mentionSources",
        "schemaInvalid",
    ):
        assert prop in src, f"<Composer> must accept `{prop}`"


def test_composer_wires_send_stop_context_aware_control() -> None:
    src = COMPOSER.read_text(encoding="utf-8")
    assert "onStop" in src, "<Composer> must reference a Stop affordance (onStop)"
    assert "running" in src
    # The context-aware control branches on `running` to swap Send <-> Stop.
    assert "running ?" in src or "running &&" in src or "running===" in src.replace(" ", "")


def test_composer_disabled_or_schema_invalid_gates_send() -> None:
    src = COMPOSER.read_text(encoding="utf-8")
    assert "schemaInvalid" in src
    # Some combination of disabled/schemaInvalid feeds the Send control's
    # disabled state (exact expression is an implementation detail).
    assert "disabled" in src


def test_composer_contains_textarea_and_attachment_strip() -> None:
    src = COMPOSER.read_text(encoding="utf-8")
    assert "<textarea" in src
    assert "onAttach" in src
    assert "onRemoveAttachment" in src


def test_composer_is_pure_no_data_fetching_or_ws() -> None:
    src = COMPOSER.read_text(encoding="utf-8")
    assert "new WebSocket(" not in src
    assert "apiFetch" not in src


# ---------------------------------------------------------------------------
# <SchemaPanel>
# ---------------------------------------------------------------------------


def test_schema_panel_module_exists_and_exports() -> None:
    assert SCHEMA_PANEL.exists(), "ui/components/chat/schema-panel.jsx is missing"
    src = SCHEMA_PANEL.read_text(encoding="utf-8")
    assert "function SchemaPanel(" in src
    assert "window.SchemaPanel = SchemaPanel;" in src


def test_schema_panel_accepts_the_documented_props() -> None:
    src = SCHEMA_PANEL.read_text(encoding="utf-8")
    for prop in (
        "value",
        "onChange",
        "persistent",
        "onPersistentChange",
        "valid",
        "onValidityChange",
        "collapsed",
        "onToggle",
    ):
        assert prop in src, f"<SchemaPanel> must accept `{prop}`"


def test_schema_panel_has_builder_json_tab_strip() -> None:
    src = SCHEMA_PANEL.read_text(encoding="utf-8")
    assert "Builder" in src
    assert "JSON" in src


def test_schema_panel_collapsed_by_default() -> None:
    src = SCHEMA_PANEL.read_text(encoding="utf-8")
    assert "collapsed = true" in src or "collapsed: true" in src, (
        "<SchemaPanel> must default to collapsed"
    )
    assert "onToggle" in src


def test_schema_panel_is_pure_no_data_fetching_or_ws() -> None:
    src = SCHEMA_PANEL.read_text(encoding="utf-8")
    assert "new WebSocket(" not in src
    assert "apiFetch" not in src


# ---------------------------------------------------------------------------
# <Conversation> wiring
# ---------------------------------------------------------------------------


def test_conversation_renders_composer() -> None:
    src = CONVERSATION.read_text(encoding="utf-8")
    assert "<Composer" in src, "<Conversation> must mount <Composer> as its input surface"


def test_conversation_no_longer_owns_the_raw_composer_jsx() -> None:
    # Behavior moved, not duplicated — the raw textarea/attach controls
    # must be defined exactly once, in composer.jsx now.
    src = CONVERSATION.read_text(encoding="utf-8")
    assert "<textarea" not in src
    assert 'data-testid="chat-attach-btn"' not in src


def test_conversation_accepts_schema_panel_sibling() -> None:
    src = CONVERSATION.read_text(encoding="utf-8")
    assert "<SchemaPanel" in src, "<Conversation> should mount <SchemaPanel> behind showSchemaPanel"
    assert "showSchemaPanel" in src


# ---------------------------------------------------------------------------
# Registration + transpile
# ---------------------------------------------------------------------------


def test_new_chat_scripts_registered_before_chats_jsx() -> None:
    order = _order()
    assert "components/chat/composer.jsx" in order
    assert "components/chat/schema-panel.jsx" in order
    assert order.index("components/chat/composer.jsx") < order.index("components/chats.jsx")
    assert order.index("components/chat/schema-panel.jsx") < order.index("components/chats.jsx")


def test_bundle_transpiles_with_composer_and_schema_panel_files() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/chat/composer.jsx === */" in text
    assert "/* === components/chat/schema-panel.jsx === */" in text
