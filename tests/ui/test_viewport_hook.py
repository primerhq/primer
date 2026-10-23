"""Static checks for the useViewport() hook contract.

The hook lives in ui/foundation/viewport.js and is exposed on
window.primerApi.useViewport. The breakpoints are 640px (mobile/tablet
boundary) and 1024px (tablet/desktop boundary). A
``?force-desktop=1`` query-string overrides the band and persists to
localStorage.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._mini_react import mini_react_context

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


def test_a_width_change_before_the_effect_is_observed_when_the_listener_attaches() -> None:
    """A resize between render and the passive effect must not leave the hook in the old band."""
    prelude = r"""
window.innerWidth = 1366;
var LISTENERS = {};
window.addEventListener = function (type, fn) { (LISTENERS[type] = LISTENERS[type] || []).push(fn); };
window.removeEventListener = function (type, fn) {
  LISTENERS[type] = (LISTENERS[type] || []).filter(function (candidate) { return candidate !== fn; });
};
window.requestAnimationFrame = function (cb) { cb(); return 1; };
window.cancelAnimationFrame = function () {};
var localStorage = { getItem: function () { return null; }, setItem: function () {} };
window.location = { href: "http://x/console/" };
function URL() { this.searchParams = { get: function () { return null; } }; }
var SEEN = [];
var renders = 0;
function Probe() {
  var viewport = window.primerApi.useViewport();
  SEEN.push(viewport.isMobile);
  renders += 1;
  if (renders === 1) {
    window.innerWidth = 390;
    (LISTENERS.resize || []).forEach(function (fn) { fn(); });
  }
  return null;
}
"""
    ctx = mini_react_context(SRC.read_text(encoding="utf-8"), prelude)
    try:
        ctx.eval("MR.mount(Probe, {})")
        result = json.loads(
            ctx.eval(
                "JSON.stringify({ seen: SEEN, width: window.innerWidth, "
                "listeners: (LISTENERS.resize || []).length })"
            )
        )
        assert result["width"] == 390 and result["listeners"] == 1
        assert result["seen"][-1] is True
    finally:
        ctx.close()
