"""FB1 regression guard — the global ⌘K command palette must not be shadowed.

`chrome.jsx` declares the app-global `CommandPalette` (top-bar Search + ⌘K on
every non-Studio page). `studio-palette.jsx` used to *also* declare a top-level
`CommandPalette`; because the bundle concatenates every file into one flat scope
with no IIFE and const→var rewriting makes redeclarations silent (last-wins), the
later-loaded Studio declaration shadowed chrome's and killed the global palette.

These checks assert the two palette components now have DISTINCT names across the
whole transpiled bundle so neither can shadow the other.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


def _bundle_text() -> str:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    return body.decode("utf-8")


def test_exactly_one_command_palette_declaration_in_bundle() -> None:
    text = _bundle_text()
    # chrome.jsx owns the single app-global `CommandPalette`. Note that
    # "function CommandPalette(" is NOT a substring of
    # "function StudioCommandPalette(", so this count isolates chrome's decl.
    assert text.count("function CommandPalette(") == 1, (
        "expected exactly one app-global CommandPalette declaration "
        "(chrome.jsx); a duplicate would silently shadow it in the flat "
        "bundle scope"
    )


def test_studio_palette_declared_under_distinct_name() -> None:
    text = _bundle_text()
    assert "function StudioCommandPalette(" in text
    assert "window.StudioCommandPalette = StudioCommandPalette;" in text


def test_both_palettes_exported_to_window() -> None:
    text = _bundle_text()
    # chrome exports CommandPalette via Object.assign(window, {...}); studio
    # exports StudioCommandPalette directly. Both names must be reachable.
    assert "CommandPalette" in text and "StudioCommandPalette" in text


def test_app_renders_chrome_command_palette() -> None:
    app = (UI / "app.jsx").read_text(encoding="utf-8")
    # app.jsx renders the app-global palette (chrome's), not the Studio one.
    assert "<CommandPalette" in app
    assert "<StudioCommandPalette" not in app


def test_studio_renders_studio_command_palette() -> None:
    studio = (UI / "components" / "studio.jsx").read_text(encoding="utf-8")
    assert "<StudioCommandPalette" in studio
    assert "<CommandPalette" not in studio
