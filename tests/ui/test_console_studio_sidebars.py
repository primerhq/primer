"""Studio sidebars (wiring plan P2 T6, retargeted for the uiv2 R2 cutover
US-011a): context-menu management, files tree + history, empty states as
prompts.

NV_sessionBands (the old band sort this file used to pin) was dead code
by the time of the 2026-08-29 UI review (F5) - bands retired with the
rail itself, and it mis-bucketed parked_status into "In progress" besides
- removed from nv-studio.jsx, and its order-pinning test removed here
with it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "ui" / "components" / "console"
STUDIO = (CONSOLE / "nv-studio.jsx").read_text(encoding="utf-8")
# RETARGET (uiv2 R2 cutover, US-011a): nv-sessions-sidebar.jsx retired;
# NV_Rail (nv-rail.jsx) is the session-carrying sidebar now.
RAIL = (CONSOLE / "nv-rail.jsx").read_text(encoding="utf-8")
FILES = (CONSOLE / "nv-files-sidebar.jsx").read_text(encoding="utf-8")


def test_attention_band_comes_from_pending_yields():
    assert "SH_api.pendingYields" in RAIL
    assert "attentionSids" in RAIL


def test_session_rows_carry_identity_and_attention_dot():
    """RETARGET: the rail's rows carry the agent glyph and an attention
    dot, same as the old sidebar - but not a live per-row status color;
    the rail has no useWorkspaceTapListener wiring (unlike the retired
    nv-sessions-sidebar.jsx), so live status only shows once a session is
    opened as a doc. Flagged to the lead as a possible gap, not silently
    reintroduced here."""
    assert "NV_identity" in RAIL
    assert "nv-dot-attention" in RAIL


def test_session_context_menu_manages_the_row():
    for label in ('"Open"', '"Rename"', '"Interrupt"', '"Park"',
                  '"End"', '"Delete"'):
        assert label in RAIL, label
    assert "confirmDialog" in RAIL, "delete confirms"
    assert "deleteSession" in RAIL


def test_files_tree_manages_and_uploads():
    for label in ('"Rename"', '"Delete"', '"Download"', '"Copy Path"'):
        assert label in FILES, label
    assert "FileReader" in FILES and "onDrop" in FILES
    assert "fileDownloadUrl" in FILES


def test_history_opens_diff_docs():
    assert "commitLog" in FILES
    m = re.search(r'"nv-commit:"[\s\S]{0,300}', FILES)
    assert m and '"diff"' in m.group(0)


def test_empty_states_are_prompts_with_one_action():
    """RETARGET: the rail's empty Inbox is a plain message with no CTA
    now (the "start a session" prompt lives in the center's empty-doc
    state instead, alongside the new Ctrl+K affordance - two actions,
    not one, a genuine UX change from the old sidebar's single button)."""
    assert "New session" in STUDIO
    assert "New file" in FILES


def test_glyphs_are_never_human():
    # The identity set is geometric paths; no avatar imagery.
    assert "img" not in STUDIO.lower() or "<img" not in STUDIO
    for agent in ("operator", "builder", "planner", "explorer",
                  "tool-runner"):
        assert agent in STUDIO, agent
