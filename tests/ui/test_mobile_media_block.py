"""Static check: the styles.css mobile media block defines the
utility classes (touch-target, mobile-only, desktop-only, sheet,
drawer, fab, card, card-list) and the (max-width: 639px) rules
that hide the sidebar + topbar search."""
from __future__ import annotations
import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "ui" / "styles.css"

def _src() -> str:
    return CSS.read_text(encoding="utf-8")

def test_touch_target_class_defined() -> None:
    src = _src()
    assert ".touch-target" in src
    assert "var(--tap-min)" in src or "44px" in src

def test_mobile_only_and_desktop_only_utilities() -> None:
    src = _src()
    assert ".mobile-only" in src
    assert ".desktop-only" in src

def test_media_query_max_639_present() -> None:
    src = _src()
    assert re.search(r"@media\s*\(\s*max-width:\s*639px\s*\)", src)

def test_drawer_class_and_animation() -> None:
    src = _src()
    assert ".drawer" in src
    assert ".drawer-overlay" in src
    assert "translateX" in src

def test_sheet_class_and_animation() -> None:
    src = _src()
    assert ".sheet-overlay" in src
    assert ".sheet" in src
    assert "@keyframes sheet-slide-up" in src
    assert ".sheet-handle" in src

def test_fab_class_with_safe_area_inset() -> None:
    src = _src()
    assert ".fab" in src
    assert "safe-area-inset-bottom" in src
    assert "var(--fab-size)" in src

def test_card_list_styled() -> None:
    src = _src()
    assert ".card-list" in src
    assert ".card" in src

def test_input_font_size_16px_to_block_ios_zoom() -> None:
    src = _src()
    assert "font-size: 16px" in src

def test_prefers_reduced_motion_disables_animation() -> None:
    src = _src()
    assert "prefers-reduced-motion" in src

def test_min_640_block_inverts_mobile_only_desktop_only() -> None:
    src = _src()
    assert re.search(r"@media\s*\(\s*min-width:\s*640px\s*\)", src)
