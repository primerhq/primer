"""Studio sidebars (wiring plan P2 T6): band ordering, context-menu
management, files tree + history, empty states as prompts.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "ui" / "components" / "console"
STUDIO = (CONSOLE / "nv-studio.jsx").read_text(encoding="utf-8")
SESS = (CONSOLE / "nv-sessions-sidebar.jsx").read_text(encoding="utf-8")
FILES = (CONSOLE / "nv-files-sidebar.jsx").read_text(encoding="utf-8")


def test_band_order_is_attention_running_idle_ended():
    m = re.search(
        r"return \[bands\.attention, bands\.running, bands\.idle,"
        r" bands\.ended\]", STUDIO)
    assert m, "the band order is the spec's sort, pinned literally"


def test_attention_band_comes_from_pending_yields():
    assert "pendingYields" in SESS
    assert "attentionSids" in SESS


def test_session_rows_carry_identity_and_live_status():
    assert "NV_identity" in SESS
    assert "SH_statusFromTap" in SESS
    assert 'data-attention=' in SESS


def test_session_context_menu_manages_the_row():
    for label in ('"Open"', '"Rename"', '"Interrupt"', '"Park"',
                  '"End"', '"Delete"'):
        assert label in SESS, label
    assert "confirmDialog" in SESS, "delete confirms"
    assert "deleteSession" in SESS


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
    assert "Start a session" in SESS
    assert "New file" in FILES


def test_glyphs_are_never_human():
    # The identity set is geometric paths; no avatar imagery.
    assert "img" not in STUDIO.lower() or "<img" not in STUDIO
    for agent in ("operator", "builder", "planner", "explorer",
                  "tool-runner"):
        assert agent in STUDIO, agent
