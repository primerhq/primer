"""FB9 — native confirm()/prompt() replaced by a themed shared Modal dialog.

shared.jsx now exposes confirmDialog()/promptDialog() (promise-based) rendered by
a single <ConfirmHost/> mounted at app root. The dirty-tab close (studio-center),
the four trigger confirmations (triggers), and the save-predicate prompt
(predicate-builder) route through it. These checks assert the helper exists, is
mounted, and that no native dialogs remain at those call sites.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SHARED = UI / "components" / "shared.jsx"
APP = UI / "app.jsx"
CENTER = UI / "components" / "studio-center.jsx"
TRIGGERS = UI / "components" / "triggers.jsx"
PREDICATE = UI / "components" / "predicate-builder.jsx"


def test_shared_defines_and_exports_dialog_helpers() -> None:
    src = SHARED.read_text(encoding="utf-8")
    assert "function confirmDialog(" in src
    assert "function promptDialog(" in src
    assert "function ConfirmHost(" in src
    # Exported on window so every component file can reach them in the flat scope.
    for name in ("confirmDialog", "promptDialog", "ConfirmHost"):
        assert name in src.split("Object.assign(window, {")[1], name


def test_dialog_host_uses_shared_modal_with_testids() -> None:
    src = SHARED.read_text(encoding="utf-8")
    assert "<Modal" in src
    for testid in ("dialog-confirm", "dialog-cancel", "dialog-input"):
        assert f'data-testid="{testid}"' in src, testid


def test_app_mounts_confirm_host() -> None:
    assert "<ConfirmHost />" in APP.read_text(encoding="utf-8")


def test_studio_center_uses_confirm_dialog() -> None:
    src = CENTER.read_text(encoding="utf-8")
    assert "window.confirm" not in src
    assert "confirmDialog({" in src


def test_triggers_use_confirm_dialog_not_native() -> None:
    src = TRIGGERS.read_text(encoding="utf-8")
    assert "window.confirm" not in src
    # All four confirmations now await the shared dialog.
    assert src.count("await confirmDialog({") == 4


def test_predicate_builder_uses_prompt_dialog() -> None:
    src = PREDICATE.read_text(encoding="utf-8")
    assert "window.prompt" not in src
    assert "promptDialog({" in src


def test_bundle_transpiles_with_dialog_helpers() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "function confirmDialog(" in text
    assert "function promptDialog(" in text
