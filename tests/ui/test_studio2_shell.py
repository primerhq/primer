"""Static source checks for the Studio2 trial shell (plan task 1)."""

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_router_has_studio2_route() -> None:
    src = _read("foundation/router.js")
    assert '"/studio2"' in src, "routes table must contain /studio2"


def test_index_html_loads_shell_script() -> None:
    src = _read("index.html")
    assert 'src="components/studio2/s2-shell.jsx"' in src


def test_root_gate_renders_shell_for_studio2() -> None:
    src = _read("app.jsx")
    assert "S2_RootGate" in src, "mount must go through the root gate"
    assert "window.S2_Shell" in src


def test_shell_has_seven_regions() -> None:
    src = _read("components/studio2/s2-shell.jsx")
    for cls in ["s2-menubar", "s2-rail", "s2-nav", "s2-center",
                "s2-right", "s2-status", "s2-term"]:
        assert cls in src, f"shell must render region {cls}"
    assert "window.S2_Shell = S2_Shell" in src


def test_chrome_links_trial() -> None:
    assert "Studio (trial)" in _read("components/chrome.jsx")


def test_shell_uses_tokens_not_literals() -> None:
    # Design-pack rule 1: zero raw color literals in components; every
    # color rides a var(--*) token. Hash-route strings are not colors.
    src = _read("components/studio2/s2-shell.jsx")
    assert "oklch(" not in src
    depaths = src.replace("#/studio2", "").replace("#/", "")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", depaths), (
        "raw hex color literal found; use var(--*) tokens"
    )


def test_shell_has_focus_ring_and_kbd() -> None:
    # Design-pack rules 12-13: visible focus ring, kbd chips.
    src = _read("components/studio2/s2-shell.jsx")
    assert ":focus-visible" in src
    assert "s2-kbd" in src


def test_mobile_gate_present() -> None:
    src = _read("components/studio2/s2-shell.jsx")
    assert "s2-gate" in src and "desktop-only" in src


def test_bundle_transpiles_every_studio2_file() -> None:
    # The hard gate: the whole console bundle (incl. every studio2 file
    # on disk) transpiles, and each file actually made it into the
    # bundle - which also catches a forgotten index.html script tag.
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    for f in sorted((UI / "components" / "studio2").glob("*.jsx")):
        assert f"/* === components/studio2/{f.name} === */" in text, (
            f"{f.name} missing from the bundle - is its index.html "
            "script tag present?"
        )
    assert "S2_RootGate" in text
