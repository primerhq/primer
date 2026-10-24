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

# Pages built on the platform kit (Platform revamp Plan 01): their cards are a PK_CardGrid (.pk-grid, an auto-fill grid that drops to one column on a
# phone) and their forms open in PK_FormOverlay (the shared Modal, a bottom sheet on a phone), so the kit reflows them and the page needs no isMobile
# branch of its own. The signal is the kit's card grid. workspaces/templates.jsx joined when Plan 08 Task 15 removed its old CardList/Fab list.
_KIT_REFLOW_PAGES = {"workspaces/templates.jsx"}


def test_every_page_consumes_use_viewport() -> None:
    missing = []
    for rel in MOBILE_AWARE_PAGES:
        p = ROOT / rel
        assert p.exists(), f"file missing: {p}"
        src = p.read_text(encoding="utf-8")
        if rel in _KIT_REFLOW_PAGES:
            if "<PK_CardGrid" in src:
                continue
            missing.append(rel)
            continue
        if "useViewport" not in src:
            if rel in _TOUCH_TARGET_ONLY_PAGES:
                if "touch-target" in src:
                    continue
            missing.append(rel)
    assert missing == [], f"pages without useViewport (or, on a kit page, without PK_CardGrid): {missing}"


def test_a_kit_page_has_no_phone_branch_of_its_own() -> None:
    """A kit page that also branched on useViewport would draw a second phone layout next to the kit's."""
    for rel in _KIT_REFLOW_PAGES:
        assert rel in MOBILE_AWARE_PAGES, rel
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "useViewport" not in src and "CardList" not in src, rel


def test_shared_modal_uses_use_viewport() -> None:
    src = (ROOT / "shared.jsx").read_text(encoding="utf-8")
    assert "useViewport" in src
    assert "sheet-overlay" in src
