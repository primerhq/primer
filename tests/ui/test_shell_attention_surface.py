"""The attention surfaces, as the three-view console draws them.

The designer model (2026-08-23 handoff) replaced S8's toast/triage
layer: attention is the decision/ask cards INLINE in the transcript at
the pause point, and the System dashboard's cross-workspace "needs a
human" panel. No sounds, ever.

RETARGET (uiv2 R2 cutover, US-011a): nv-sessions-sidebar.jsx and its
"Needs you" BAND rendering are retired. The rail (nv-rail.jsx) replaced
bands with a cross-workspace Inbox (still built from the same
SH_api.pendingYields feed, still marked with the attention dot, never a
sound) plus a per-workspace session tree.

RESTORED (US-011f): the retirement initially shipped the rail with no
useWorkspaceTapListener wiring at all, a real reactivity regression
against the retired sidebar (flagged rather than silently dropped from
coverage). The rail now refetches live on the SAME three tap-frame
classes, scoped to the currently-selected workspace (the retired
sidebar's own scope - the rail's Inbox is cross-workspace, but
useWorkspaceTapListener is one EventSource per workspace, so opening
one per every known workspace just for this rail would be a much
bigger change than restoring the property that regressed). Other
workspaces' attention changes still arrive via the Inbox's own poll,
unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
RAIL = UI / "components" / "console" / "nv-rail.jsx"
STUDIO = UI / "components" / "console" / "nv-studio.jsx"
SESSION_DOC = UI / "components" / "console" / "nv-session-doc.jsx"
SYSTEM = UI / "components" / "console" / "nv-system.jsx"


def test_the_feed_is_s1_plus_approvals_never_s6() -> None:
    """Amendment m10, asserted where the data is actually fetched."""
    src = RAIL.read_text(encoding="utf-8")
    assert "SH_api.pendingYields" in src
    for banned in ("triggers", "channels", "subscriptions"):
        assert banned not in src, banned


def test_the_tap_invalidates_the_feed_live() -> None:
    """US-011f: restores the retired sidebar's test_the_tap_invalidates_
    the_feed_live (git show 214e2d57 -- this file) against the rail
    instead. Pinned decision 7 names the three frame classes; the
    debounce window is additive liveness, not a replacement for them."""
    src = RAIL.read_text(encoding="utf-8")
    assert "useWorkspaceTapListener" in src
    for cls in ("yielded", "resumed", "done"):
        assert f'"{cls}"' in src, cls
    # The poll stays as the fallback layer (same pattern as the rest of
    # the shell) - this is additive, not a replacement.
    assert "pollMs: 10000" in src or "pollMs: 5000" in src


def test_attention_is_an_inbox_and_a_dot_never_a_sound() -> None:
    src = RAIL.read_text(encoding="utf-8")
    assert 'data-testid={"nv-rail-inbox-row:" + it.session_id}' in src
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
