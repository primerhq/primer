"""Structural guards for three console UX fixes (branch
fix/studio-theme-dropdown-rail):

  1. The topbar user-menu dropdown paints on a REAL surface token
     (var(--bg-1)) instead of the undefined var(--surface) — which resolved to
     nothing, so the menu painted transparent and text overlapped the page.
  2. Theme + density have a SINGLE owner: the global `tweaks` store
     (foundation/tweaks.js), applied to <html> by app.jsx. The Studio no longer
     keeps its own theme/density or stamps <html data-theme>/data-density — that
     second owner reverted the theme on navigate and desynced the graph canvas'
     <html data-theme> observer. The Studio toggles (⌘K palette) route through
     setTweak instead.
  3. The collapsed debug rail is an obviously-clickable button: the WHOLE 40px
     strip is the click target with a theme-legible hover highlight and a
     prominent << (chevrons-left) + vertical Debug label.

Static-source checks only (no React rendering), matching the tests/ui suite
convention (see test_studio_shell.py / test_studio_debug_sidebar.py).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHROME = UI / "components" / "chrome.jsx"
STUDIO = UI / "components" / "studio.jsx"
ACTIVITY = UI / "components" / "studio-activity.jsx"
PALETTE = UI / "components" / "studio-palette.jsx"
TERMINAL = UI / "components" / "studio-terminal.jsx"
STYLES = UI / "styles.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


# ---------------------------------------------------------------------------
# Bug 1 — user-menu dropdown surface token
# ---------------------------------------------------------------------------


def test_user_menu_dropdown_uses_bg1_not_undefined_surface_var() -> None:
    src = _read(CHROME)
    # --surface is defined NOWHERE in styles.css, so the old value resolved to
    # transparent. The dropdown must paint on a real token — the same var the
    # Studio's own popovers (.st-ws-menu) use.
    assert "var(--surface)" not in src, "dropdown must not reference the undefined --surface"
    # The bg-1 background sits on the absolutely-positioned menu, just above the
    # "Signed in as" label.
    idx = src.index("Signed in as")
    window = src[idx - 400:idx]
    assert 'background: "var(--bg-1)"' in window


def test_surface_token_is_not_defined_in_styles() -> None:
    # The root cause: there is no --surface custom property to resolve against.
    assert "--surface" not in _read(STYLES)


# ---------------------------------------------------------------------------
# Bug 2 — theme/density owned solely by the global tweaks store
# ---------------------------------------------------------------------------


def test_studio_does_not_stamp_html_theme_or_density_itself() -> None:
    src = _read(STUDIO)
    # app.jsx is the ONLY writer of <html data-theme>/data-density; the Studio
    # no longer sets (or restores) them.
    assert 'setAttribute("data-theme"' not in src
    assert 'setAttribute("data-density"' not in src


def test_studio_subscribes_to_global_tweaks() -> None:
    src = _read(STUDIO)
    assert "var [tweaks, setTweak] = window.useTweaks();" in src


def test_studio_toggles_route_through_set_tweak() -> None:
    src = _read(STUDIO)
    assert 'setTweak("theme", tweaks.theme === "dark" ? "light" : "dark")' in src
    assert 'setTweak("density", tweaks.density === "comfortable" ? "compact" : "comfortable")' in src
    # The toggles keep their names + exports so studio-palette.jsx keeps working.
    assert "toggleTheme: toggleTheme," in src
    assert "toggleDensity: toggleDensity," in src


def test_studio_root_does_not_hardcode_theme_or_density_attrs() -> None:
    src = _read(STUDIO)
    # Tokens resolve from <html data-theme>/data-density (app.jsx); there are 0
    # scoped .st-*[data-theme] selectors, so the st-root needs no attrs.
    assert "data-theme={s.theme}" not in src
    assert "data-density={s.density}" not in src


def test_studio_default_state_drops_theme_and_density() -> None:
    default_state = _slice(_read(STUDIO), "function ST_defaultState(", "function useStudioState(")
    assert 'theme: "dark"' not in default_state
    assert 'density: "comfortable"' not in default_state


def test_studio_persist_keys_drop_theme_and_density() -> None:
    persist = _slice(_read(STUDIO), "var ST_PERSIST_KEYS = [", "];")
    assert '"theme"' not in persist
    assert '"density"' not in persist


def test_palette_theme_and_density_commands_still_call_studio_toggles() -> None:
    src = _read(PALETTE)
    assert "studio.toggleTheme();" in src
    assert "studio.toggleDensity();" in src


def test_terminal_live_theme_trigger_reads_global_tweak_not_studio_state() -> None:
    src = _read(TERMINAL)
    # The per-terminal repaint effect keys off a `theme` change signal; that
    # signal is now the global tweak, not the removed studio state.theme.
    assert "theme={s.theme}" not in src
    assert "var theme = window.useTweaks()[0].theme;" in src
    assert "theme={theme}" in src


# ---------------------------------------------------------------------------
# Bug 3 — collapsed debug rail reads as an obvious button
# ---------------------------------------------------------------------------


def _activity_fn() -> str:
    return _slice(_read(ACTIVITY), "function StudioActivity(", "window.StudioActivity = StudioActivity;")


def test_collapsed_rail_whole_strip_is_the_click_target() -> None:
    fn = _activity_fn()
    # Full-height button + pointer cursor => the entire 40px strip is clickable,
    # not just a small cap at the top.
    assert 'height: collapsed ? "100%" : 34' in fn
    assert 'cursor: "pointer"' in fn
    # Prominent << (chevrons-left) as the collapsed expand indicator.
    assert 'Icon name={collapsed ? "chevrons-left" : "chevrons-right"}' in fn


def test_collapsed_rail_has_a_hover_highlight_via_css_class() -> None:
    fn = _activity_fn()
    # The button carries the st-debug-toggle class (+ is-rail when collapsed) so
    # the hover fill lives in CSS and can win over the base background.
    assert 'className={"st-debug-toggle" + (collapsed ? " is-rail" : "")}' in fn
    css = _read(STYLES)
    assert ".st-debug-toggle:hover" in css
    assert ".st-debug-toggle.is-rail:hover" in css
    # No inline background on the button — an inline background would out-specify
    # the :hover rule and kill the affordance.
    assert 'background: "transparent"' not in fn


def test_collapsed_rail_label_uses_a_legible_token() -> None:
    fn = _activity_fn()
    # The vertical Debug label must use a mid/high-contrast token legible in both
    # themes (--text-2), not a near-background token.
    label = _slice(fn, 'data-testid="debug-sidebar-rail-label"', "Debug")
    assert 'color: "var(--text-2)"' in label
    assert 'color: "var(--text-3)"' not in label


# ---------------------------------------------------------------------------
# The whole bundle still transpiles with all three edits.
# ---------------------------------------------------------------------------


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/studio.jsx === */" in text
    assert "/* === components/studio-activity.jsx === */" in text
    assert "/* === components/chrome.jsx === */" in text
