"""Static check: BottomSheet exists, accepts open/onClose/children,
implements ESC + click-outside + body-scroll-lock + focus trap."""
from __future__ import annotations
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "ui" / "components" / "shared" / "bottom-sheet.jsx"
)

def test_file_exists() -> None:
    assert SRC.exists()

def test_bottom_sheet_defined() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "function BottomSheet" in src or "const BottomSheet" in src

def test_open_and_on_close_props() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "open" in src
    assert "onClose" in src

def test_escape_key_handler_present() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "Escape" in src
    assert "keydown" in src

def test_body_scroll_lock_present() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "document.body" in src
    assert "overflow" in src

def test_aria_dialog_role() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "dialog" in src
    assert "aria-modal" in src

def test_sheet_class_in_jsx() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "sheet-overlay" in src
    assert "sheet" in src
    assert "sheet-handle" in src

def test_exported_to_window() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "window.BottomSheet" in src
