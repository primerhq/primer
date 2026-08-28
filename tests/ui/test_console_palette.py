"""The universal search bar (wiring plan P1 T5): mixed result kinds
over one verb registry, keyboard selection spanning every group,
transient state never in the URL.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (
    ROOT / "ui" / "components" / "console" / "nv-palette.jsx"
).read_text(encoding="utf-8")
API = (
    ROOT / "ui" / "components" / "shell" / "sh-api.jsx"
).read_text(encoding="utf-8")


def test_mixed_result_kinds():
    # Group order per implementer-notes.md 1.3: Verbs, Sessions, Files,
    # Platform (the group was named "Entities" before the uiv2 R1 delta).
    for group in ('"Verbs"', '"Sessions"', '"Files"', '"Platform"'):
        assert group in SRC, group
    assert "SH_rankVerbs" in SRC
    assert "allSessions" in SRC


def test_entity_lists_exist_on_the_seam():
    assert re.search(r"agents: function \(signal\)", API)
    assert re.search(r"graphs: function \(signal\)", API)


def test_keyboard_spans_all_groups():
    assert "ArrowDown" in SRC and "ArrowUp" in SRC
    assert "flat[selIdx].run()" in SRC, (
        "Enter runs from the flattened cross-group list"
    )


def test_cross_workspace_session_rows_use_the_combined_navigation():
    """F2/F3 (2026-08-29 UI review): this used to raw-assign
    location.hash (its own navigation outside con's markPush bookkeeping,
    and a preview-only open) - now it routes through con.openInWorkspace
    (one history entry) and promotes, matching the rail's own rows."""
    m = re.search(r'workspace_id !== con\.wid[\s\S]{0,300}', SRC)
    assert m
    assert "con.openInWorkspace(s.workspace_id" in m.group(0)
    assert "SH_buildUrl" not in m.group(0)
    assert 'con.promoteDoc("session:" + s.session_id)' in SRC


def test_transient_state_stays_out_of_the_url():
    for banned in ("paletteOpen", "q=", "query="):
        assert banned not in "".join(
            re.findall(r"SH_buildUrl\(\{([\s\S]*?)\}\)", SRC)
        ), banned
