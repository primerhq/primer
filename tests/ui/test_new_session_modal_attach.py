"""The Studio "New session" create form was enlarged from a small positioned
overlay into a proper centered Modal (reusing the shared ``Modal`` from
shared.jsx) with a LARGE multi-line instructions box so a detailed prompt can
be pasted.

These are structural (source-text) checks, matching the style of the other
new-session-form tests in this suite.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SHARED_FORM = UI / "components" / "new-session-form.jsx"


def _src() -> str:
    return SHARED_FORM.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Enlarged into a proper centered Modal
# ---------------------------------------------------------------------------


def test_form_renders_via_shared_modal() -> None:
    src = _src()
    # The form body is rendered inside the shared <Modal> (centered overlay
    # with Escape / backdrop-click / Cancel close), titled "New session".
    assert "<Modal" in src
    assert 'title="New session"' in src
    # The old positioned "inline" overlay chrome is gone.
    assert 'if (variant === "inline")' not in src
    assert 'position: "absolute"' not in src


def test_modal_is_comfortably_wide() -> None:
    src = _src()
    # A width override opts out of the default 420px .modal cap so the form is
    # wide enough to paste a detailed prompt.
    assert 'width="min(94vw, 640px)"' in src


def test_instructions_textarea_is_large() -> None:
    src = _src()
    # The Initial instructions box is multi-line, tall, and resizable.
    assert 'data-testid="new-session-instructions"' in src
    assert "rows={8}" in src
    assert 'resize: "vertical"' in src


def test_form_keeps_testid_and_name_field() -> None:
    src = _src()
    # Testids preserved through the enlargement.
    assert 'data-testid="new-session-form"' in src
    assert 'data-testid="new-session-name"' in src


def test_bundle_transpiles_with_enlarged_form() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    assert "/* === components/new-session-form.jsx === */" in body.decode("utf-8")
