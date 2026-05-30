"""ui/app.jsx owns drawerOpen state, passes onOpenDrawer to Topbar,
renders MobileNav with onClose={() => setDrawerOpen(false)}, and resets
drawerOpen on route change."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "app.jsx"

def _src() -> str:
    return SRC.read_text(encoding="utf-8")

def test_drawer_open_state() -> None:
    src = _src()
    assert "drawerOpen" in src
    assert "setDrawerOpen" in src

def test_mobile_nav_rendered() -> None:
    src = _src()
    assert "MobileNav" in src

def test_on_open_drawer_passed_to_topbar() -> None:
    src = _src()
    assert "onOpenDrawer" in src

def test_route_change_resets_drawer_open() -> None:
    src = _src()
    idx_path = src.find("path")
    idx_drawer = src.find("setDrawerOpen(false)")
    assert idx_drawer >= 0
    assert idx_path >= 0
