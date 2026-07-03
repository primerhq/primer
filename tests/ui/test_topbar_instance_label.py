"""FB4 — the top-bar instance suffix was a hardcoded '· localhost:8765'.
tweaks.instanceLabel existed but was never read. Assert the label is now
derived from the tweak (with a host fallback), not a literal."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHROME = UI / "components" / "chrome.jsx"
TWEAKS = UI / "foundation" / "tweaks.js"


def test_chrome_reads_instance_label_tweak() -> None:
    src = CHROME.read_text(encoding="utf-8")
    assert "tweaks.instanceLabel" in src
    assert "instanceText" in src


def test_chrome_drops_hardcoded_instance_literal() -> None:
    src = CHROME.read_text(encoding="utf-8")
    # The old literal JSX text node must be gone; the value now flows from state.
    assert '"instance">· localhost:8765</div>' not in src
    assert 'className="instance"' in src


def test_chrome_falls_back_to_host_when_unset() -> None:
    src = CHROME.read_text(encoding="utf-8")
    assert "window.location.host" in src


def test_instance_label_tweak_still_defined() -> None:
    assert "instanceLabel" in TWEAKS.read_text(encoding="utf-8")


def test_topbar_instance_testid_present() -> None:
    assert 'data-testid="topbar-instance"' in CHROME.read_text(encoding="utf-8")
