"""The attention surfaces: rail badge, in-shell toast, triage verbs.

Three tiers routed by consequence, interrupts spent sparingly, triage
verbs on every item, and keyboard-first triage through the palette.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
RAIL = UI / "components" / "shell" / "sh-rail.jsx"
HOST = UI / "components" / "shell" / "sh-doc-host.jsx"


def _rail() -> str:
    return RAIL.read_text(encoding="utf-8")


def test_the_list_is_built_from_the_pure_model() -> None:
    src = _rail()
    assert "SH_toAttentionItems" in src
    assert "SH_applyTriage" in src


def test_the_feed_is_s1_plus_approvals_never_s6() -> None:
    """Amendment m10, asserted where the data is actually fetched."""
    src = _rail()
    assert "SH_api.pendingYields" in src
    assert "SH_api.approvalRecords" in src
    for banned in ("triggers", "channels", "subscriptions"):
        assert banned not in src, banned


def test_the_tap_invalidates_the_feed_live() -> None:
    """Pinned decision 7 names the three frame classes."""
    src = _rail()
    assert "useWorkspaceTapListener" in src
    for cls in ("yielded", "resumed", "done"):
        assert f'"{cls}"' in src, cls


def test_only_interrupts_toast_and_ambient_only_badges() -> None:
    src = _rail()
    assert 'data-testid={"attention-toast:"' in src
    assert re.search(r'tier\s*===\s*"interrupt"', src)
    assert 'data-testid="rail-badge:attention"' in src
    assert "playSound" not in src and "Audio(" not in src


def test_a_digest_row_is_a_collapsed_rollup_not_a_toast() -> None:
    src = _rail()
    assert "<details" in src
    assert re.search(r'tier\s*===\s*"digest"', src)


def test_every_item_carries_the_three_triage_verbs() -> None:
    src = _rail()
    for verb in ("attention.resolve", "attention.snooze", "attention.mute"):
        assert verb in src, verb


def test_triage_is_keyboard_first_through_the_palette() -> None:
    src = HOST.read_text(encoding="utf-8")
    for verb in ("attention.next", "attention.resolve", "attention.snooze",
                 "attention.mute"):
        assert verb in src, verb
    for label in ("Resolve Attention", "Snooze Attention", "Mute Session"):
        assert label in src, label


def test_triage_persistence_is_per_account() -> None:
    src = _rail()
    assert "SH_triageKey" in src
    assert "localStorage" in src
