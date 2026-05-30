"""Static check: Fab takes icon/label/onClick and uses .fab class."""
from __future__ import annotations
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "ui" / "components" / "shared" / "floating-action.jsx"
)

def test_file_exists() -> None:
    assert SRC.exists()

def test_fab_defined() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "function Fab" in src or "const Fab" in src

def test_props_icon_label_on_click() -> None:
    src = SRC.read_text(encoding="utf-8")
    for prop in ("icon", "label", "onClick"):
        assert prop in src, f"missing {prop} prop"

def test_aria_label_uses_label_prop() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "aria-label" in src

def test_fab_class_used() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert '"fab"' in src or "'fab'" in src or "fab touch-target" in src

def test_exported_to_window() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "window.Fab" in src
