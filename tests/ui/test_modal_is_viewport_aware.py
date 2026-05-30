"""shared.jsx Modal branches on useViewport().isMobile and renders
.sheet-overlay on mobile, .modal-overlay on desktop."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "shared.jsx"

def _src() -> str:
    return SRC.read_text(encoding="utf-8")

def test_modal_reads_use_viewport() -> None:
    assert "useViewport" in _src()

def test_modal_branches_on_is_mobile() -> None:
    assert "isMobile" in _src()

def test_modal_renders_sheet_overlay_when_mobile() -> None:
    src = _src()
    assert "sheet-overlay" in src
    assert "sheet-handle" in src

def test_modal_still_renders_modal_overlay_on_desktop() -> None:
    assert "modal-overlay" in _src()

def test_modal_api_unchanged_title_on_close_children_footer_danger() -> None:
    src = _src()
    for prop in ("title", "onClose", "children", "footer", "danger"):
        assert prop in src, f"missing Modal prop {prop}"
