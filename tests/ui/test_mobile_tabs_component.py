"""Static check: MobileTabs strip with tabs/active/onSelect props."""
from __future__ import annotations
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "ui" / "components" / "shared" / "mobile-tabs.jsx"
)

def test_file_exists() -> None:
    assert SRC.exists()

def test_mobile_tabs_defined() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "function MobileTabs" in src or "const MobileTabs" in src

def test_props_tabs_active_on_select() -> None:
    src = SRC.read_text(encoding="utf-8")
    for prop in ("tabs", "active", "onSelect"):
        assert prop in src, f"missing {prop} prop"

def test_role_tablist_for_a11y() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "tablist" in src
    assert "tab" in src

def test_exported_to_window() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "window.MobileTabs" in src
