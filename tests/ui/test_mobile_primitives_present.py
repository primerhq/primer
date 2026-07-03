"""Catch a page-level component drifting back to a desktop-only
layout by checking that every page in the mobile sweep references
useViewport at least once."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "ui" / "components"

MOBILE_AWARE_PAGES = [
    "auth.jsx",
    "dashboard.jsx",
    "sessions-list.jsx",
    "workspaces.jsx",
    "workspaces/providers.jsx",
    "workspaces/templates.jsx",
    "agents.jsx",
    "graphs.jsx",
    "chats.jsx",
    "knowledge.jsx",
    "internal-collections.jsx",
    "semantic-search.jsx",
    "toolsets.jsx",
    "providers.jsx",
    "approvals.jsx",
    "channels.jsx",
    "harnesses.jsx",
    "harness_form.jsx",
    "workers.jsx",
    "health.jsx",
]


def test_every_page_consumes_use_viewport() -> None:
    missing = []
    for rel in MOBILE_AWARE_PAGES:
        p = ROOT / rel
        assert p.exists(), f"file missing: {p}"
        if "useViewport" not in p.read_text(encoding="utf-8"):
            # auth uses touch-target only; tolerate either signal.
            if rel == "auth.jsx":
                if "touch-target" in p.read_text(encoding="utf-8"):
                    continue
            missing.append(rel)
    assert missing == [], f"pages without useViewport: {missing}"


def test_chrome_uses_mobile_nav() -> None:
    src = (ROOT / "chrome.jsx").read_text(encoding="utf-8")
    assert "MobileNav" in src
    assert "hamburger" in src


def test_shared_modal_uses_use_viewport() -> None:
    src = (ROOT / "shared.jsx").read_text(encoding="utf-8")
    assert "useViewport" in src
    assert "sheet-overlay" in src
