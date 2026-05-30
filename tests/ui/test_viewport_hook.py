"""Static checks for the useViewport() hook contract.

The hook lives in ui/foundation/viewport.js and is exposed on
window.primerApi.useViewport. The breakpoints are 640px (mobile/tablet
boundary) and 1024px (tablet/desktop boundary). A
``?force-desktop=1`` query-string overrides the band and persists to
localStorage.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "foundation" / "viewport.js"


def test_file_exists() -> None:
    assert SRC.exists(), f"expected {SRC} to exist"


def test_exports_use_viewport_on_primer_api() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "primerApi" in src
    assert "useViewport" in src


def test_breakpoint_constants_present() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "639" in src or "640" in src
    assert "1023" in src or "1024" in src


def test_force_desktop_escape_hatch_present() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "force-desktop" in src
    assert "localStorage" in src


def test_resize_listener_uses_request_animation_frame() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "requestAnimationFrame" in src
    assert "addEventListener" in src
    assert "resize" in src


def test_returns_shape_contains_band_flags_and_width() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "isMobile" in src
    assert "isTablet" in src
    assert "isDesktop" in src
    assert "width" in src
