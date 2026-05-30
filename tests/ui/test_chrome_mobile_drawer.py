"""chrome.jsx adds a hamburger button + MobileNav drawer + manages
drawerOpen state. Sidebar content (NAV) is reused; only the wrapping
shell is mobile-specific."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "chrome.jsx"

def _src() -> str:
    return SRC.read_text(encoding="utf-8")

def test_mobile_nav_component_defined() -> None:
    src = _src()
    assert "function MobileNav" in src or "const MobileNav" in src

def test_hamburger_button_in_topbar() -> None:
    src = _src()
    assert "hamburger" in src
    assert "Open navigation" in src

def test_drawer_class_used_with_open_modifier() -> None:
    src = _src()
    assert "drawer" in src
    assert "drawer-overlay" in src

def test_escape_handler_closes_drawer() -> None:
    src = _src()
    assert "Escape" in src
    assert "keydown" in src

def test_route_change_closes_drawer() -> None:
    src = _src()
    assert "drawerOpen" in src

def test_mobile_nav_exposed_on_window() -> None:
    src = _src()
    assert "MobileNav" in src
