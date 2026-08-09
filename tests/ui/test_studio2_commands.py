"""Static checks for the Studio2 command registry + palette (plan task 2)."""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_registry_api_surface() -> None:
    src = _read("components/studio2/s2-commands.jsx")
    for needle in ["register(", "list()", "run(", "window.S2_Commands"]:
        assert needle in src


def test_palette_two_modes_and_export() -> None:
    src = _read("components/studio2/s2-palette.jsx")
    assert '"cmd"' in src and '"open"' in src
    assert "window.S2_openPalette" in src
    assert "S2_QuickIndex" in src


def test_palette_shows_shortcuts_as_kbd_chips() -> None:
    # Design-pack rule 13.
    assert "s2-kbd" in _read("components/studio2/s2-palette.jsx")


def test_shell_wires_keys() -> None:
    src = _read("components/studio2/s2-shell.jsx")
    assert "S2_openPalette" in src
    assert '"Escape"' in src and "blur()" in src, "Esc must blur inputs"
    assert '"g"' in src, "g-chord entry point"
    assert "S2_Palette" in src


def test_scripts_ordered_before_shell() -> None:
    src = _read("index.html")
    a = src.index("s2-commands.jsx")
    b = src.index("s2-palette.jsx")
    c = src.index("s2-shell.jsx")
    assert a < c and b < c
