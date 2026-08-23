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
    for group in ('"Verbs"', '"Sessions"', '"Files"', '"Entities"'):
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


def test_cross_workspace_session_rows_navigate_by_url():
    m = re.search(r'workspace_id !== con\.wid[\s\S]{0,300}', SRC)
    assert m and "SH_buildUrl" in m.group(0)


def test_transient_state_stays_out_of_the_url():
    for banned in ("paletteOpen", "q=", "query="):
        assert banned not in "".join(
            re.findall(r"SH_buildUrl\(\{([\s\S]*?)\}\)", SRC)
        ), banned
