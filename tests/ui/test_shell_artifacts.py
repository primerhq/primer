"""Inline artifacts + the chat header (revamp spec section 4, decision 4).

Session artifacts expand IN PLACE from their tool chips; tabs are an
explicit escalation on the block; Maximize opens a lightbox that Esc
closes and that never reaches the URL. The header carries a renamable
title, the workspace chip, and one calm overflow menu.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (
    ROOT / "ui" / "components" / "shell" / "sh-session-doc.jsx"
).read_text(encoding="utf-8")


def test_write_chips_expand_in_place_not_into_tabs():
    m = re.search(r"function chip\(row\)[\s\S]{0,700}", SRC)
    assert m, "chip renderer missing"
    assert "openDoc" not in m.group(0), (
        "a chip click toggles the inline artifact; Open as Tab is the"
        " explicit escalation on the block"
    )
    assert "setExpanded" in m.group(0)


def test_artifact_block_caps_and_escalates():
    assert "SH_ARTIFACT_PREVIEW_LINES = 200" in SRC
    assert 'data-testid="shell-artifact-open-tab"' in SRC
    assert 'data-testid="shell-artifact-maximize"' in SRC


def test_lightbox_esc_closes_and_stays_out_of_the_url():
    m = re.search(r"function SH_Lightbox[\s\S]{0,600}", SRC)
    assert m and '"Escape"' in m.group(0)
    assert "SH_buildUrl" not in m.group(0)


def test_header_title_renames_via_patch():
    assert 'data-testid="shell-session-title"' in SRC
    assert "renameSession" in SRC
    assert 'data-testid="shell-session-workspace"' in SRC


def test_header_verbs_live_in_one_overflow_menu():
    m = re.search(r'data-testid="shell-session-menu"[\s\S]{0,700}', SRC)
    assert m and 'forSurface("tab-menu")' in m.group(0)
