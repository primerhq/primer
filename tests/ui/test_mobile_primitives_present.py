"""Catch a page-level component drifting back to a desktop-only
layout by checking that every page in the mobile sweep references
useViewport at least once."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "ui" / "components"

MOBILE_AWARE_PAGES = [
    "auth.jsx",
    "sessions-list.jsx",
    "workspaces.jsx",
    "workspaces/providers.jsx",
    "workspaces/templates.jsx",
    "agents.jsx",
    "graphs.jsx",
    "knowledge.jsx",
    "internal-collections.jsx",
    "semantic-search.jsx",
    "toolsets.jsx",
    "provider-catalog.jsx",
    "channels.jsx",
    "harnesses.jsx",
    "harness_form.jsx",
    "workers.jsx",
    "health.jsx",
]

# uiv2 Wave 3 (a-14 fold): approvals.jsx dropped OUT of this sweep, not
# just off the useViewport hook - its own mobile-specific CardList/
# BottomSheet approve/deny panel was deleted along with the rest of
# the records-sheet (that capability lives on the Inbox rail and
# session-detail's NV_DecisionCard/ApprovalBanner now, both already
# mobile-aware in their own right). What's left in approvals.jsx (a
# config-hint banner + AP_NewPolicyModal, a <Modal>) needs no isMobile
# branch of its own - Modal already handles narrow viewports, same
# "self-reflowing, no page-level branch needed" shape as provider-
# catalog.jsx's grid below.



# Pages that adapt to narrow viewports without a JS useViewport branch -
# touch-target sizing (auth.jsx) or a self-reflowing CSS grid
# (provider-catalog.jsx's .pc-card-grid, platform wave P1a item 3 - a card
# grid everywhere needs no isMobile/CardList branch to adapt) are real,
# checkable alternate signals, not a silent gap.
_TOUCH_TARGET_ONLY_PAGES = {"auth.jsx", "provider-catalog.jsx"}


def test_every_page_consumes_use_viewport() -> None:
    missing = []
    for rel in MOBILE_AWARE_PAGES:
        p = ROOT / rel
        assert p.exists(), f"file missing: {p}"
        if "useViewport" not in p.read_text(encoding="utf-8"):
            if rel in _TOUCH_TARGET_ONLY_PAGES:
                if "touch-target" in p.read_text(encoding="utf-8"):
                    continue
            missing.append(rel)
    assert missing == [], f"pages without useViewport: {missing}"


def test_shared_modal_uses_use_viewport() -> None:
    src = (ROOT / "shared.jsx").read_text(encoding="utf-8")
    assert "useViewport" in src
    assert "sheet-overlay" in src
