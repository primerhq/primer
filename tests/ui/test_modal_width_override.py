"""The Modal component supports a width override so wide content (the
collection document browser) is not crushed by the default 420px .modal
cap. The collection browse modal opts into a wide modal; without the
override the fixed-width left tree column starved the content pane to a
few pixels (one word per line)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SHARED = (UI / "components" / "shared.jsx").read_text(encoding="utf-8")
KNOWLEDGE = (UI / "components" / "knowledge.jsx").read_text(encoding="utf-8")


def test_modal_destructures_width_prop() -> None:
    # Modal accepts a width prop alongside its existing props.
    assert "danger, width })" in SHARED


def test_modal_applies_width_to_modal_element() -> None:
    # The desktop .modal element takes the inline width override so the
    # CSS `width: 420px` cap can be widened by callers. (Format-robust: the
    # .modal div gained aria-modal/tabIndex for the focus-trap, so the attrs
    # are no longer on one line — assert the width-style expression itself.)
    assert 'className="modal"' in SHARED
    assert "style={width ? { width } : undefined}" in SHARED


def test_collection_browse_modal_opts_into_wide() -> None:
    # The collection doc browser passes a wide width to its Modal.
    assert 'width="min(92vw, 1280px)"' in KNOWLEDGE


def test_bundle_transpiles_with_modal_width() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
