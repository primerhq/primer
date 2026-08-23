"""The attention surfaces, as the three-view console draws them.

The designer model (2026-08-23 handoff) replaced S8's toast/triage
layer: attention is the "Needs you" BAND at the top of the sessions
sidebar (with the attention dot), the decision/ask cards INLINE in the
transcript at the pause point, and the System dashboard's
cross-workspace "needs a human" panel. No sounds, ever.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SIDEBAR = UI / "components" / "console" / "nv-sessions-sidebar.jsx"
STUDIO = UI / "components" / "console" / "nv-studio.jsx"
SESSION_DOC = UI / "components" / "console" / "nv-session-doc.jsx"
SYSTEM = UI / "components" / "console" / "nv-system.jsx"


def test_the_list_is_built_from_the_pure_model() -> None:
    # Band ordering is a pure function (unit-tested on its own); the
    # sidebar renders FROM it rather than sorting inline.
    assert "NV_sessionBands" in SIDEBAR.read_text(encoding="utf-8")
    src = STUDIO.read_text(encoding="utf-8")
    assert "function NV_sessionBands" in src
    assert '"attention"' in src and '"running"' in src


def test_the_feed_is_s1_plus_approvals_never_s6() -> None:
    """Amendment m10, asserted where the data is actually fetched."""
    src = SIDEBAR.read_text(encoding="utf-8")
    assert "SH_api.pendingYields" in src
    for banned in ("triggers", "channels", "subscriptions"):
        assert banned not in src, banned


def test_the_tap_invalidates_the_feed_live() -> None:
    """Pinned decision 7 names the three frame classes."""
    src = SIDEBAR.read_text(encoding="utf-8")
    assert "useWorkspaceTapListener" in src
    for cls in ("yielded", "resumed", "done"):
        assert f'"{cls}"' in src, cls


def test_attention_is_a_band_and_a_dot_never_a_sound() -> None:
    src = SIDEBAR.read_text(encoding="utf-8")
    assert 'data-testid={"nv-band:" + band.id}' in src
    assert "nv-dot-attention" in src
    assert "playSound" not in src and "Audio(" not in src


def test_decisions_render_inline_from_the_shared_model() -> None:
    """The same pure model feeds the sidebar's band membership and the
    transcript's inline cards, so the two can never disagree."""
    src = SESSION_DOC.read_text(encoding="utf-8")
    assert "SH_toAttentionItems" in src
    assert "NV_DecisionCard" in src and "NV_AskCard" in src
    assert re.search(r'item\.kind === "approval"', src)


def test_cross_workspace_attention_lives_on_the_dashboard() -> None:
    src = SYSTEM.read_text(encoding="utf-8")
    assert "NV_AttentionEverywhere" in src
    assert "pendingYields" in src
    assert "Needs a human" in src
