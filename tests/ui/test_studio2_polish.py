"""Static checks for Studio2 trial polish (plan task 12)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_menus_generated_from_registry() -> None:
    src = _read(UI / "components/studio2/s2-shell.jsx")
    assert "S2_Commands.list()" in src, "menus must derive from the registry"
    for menu in ["File:", "View:", "Go:", "Run:"]:
        assert menu in src


def test_status_bar_live_counts_and_ctx() -> None:
    src = _read(UI / "components/studio2/s2-shell.jsx")
    assert "running" in src and "waiting" in src
    assert "nRunning" in src and "nWaiting" in src


def test_dev_doc_exists_and_is_linked() -> None:
    doc = ROOT / "docs/dev/subsystems/ui-studio2.md"
    assert doc.exists()
    body = _read(doc)
    assert "S2_Docs" in body and "legacy" in body
    assert chr(0x2014) not in body, "no em-dash in committed docs"
    assert "studio2" in _read(ROOT / "docs/dev/subsystems/ui-pages.md")
