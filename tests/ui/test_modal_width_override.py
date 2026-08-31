"""The Modal component supports a width override so wide content is not
crushed by the default 420px .modal cap. The override exists because the
collection document browser (then a modal) had its content pane starved
to a few pixels by the fixed-width left tree column; that browser is now
a full page, but the override stays for any caller that needs it."""

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


def test_collection_doc_browser_is_not_modal_confined() -> None:
    """The document browser is a full-page view, not a width-capped modal.

    It used to be a Modal that needed the width override to stop the fixed
    left tree column starving the content pane. The collections rebuild
    made it a page instead, which answers the same problem without the
    override; the modals that remain are narrow forms.
    """
    assert "KN_CollectionDetail" in KNOWLEDGE
    assert 'className="col kn-tree"' in KNOWLEDGE


def test_bundle_transpiles_with_modal_width() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
