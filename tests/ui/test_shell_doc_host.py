"""Tabs, split groups and the registry-rendered tab menu.

Also the point where every overlay earns its verb: an overlay with no
verb is an orphaned surface, which is exactly the risk section 9 names.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-doc-host.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_every_overlay_gets_a_verb() -> None:
    src = _src()
    assert "SH_OVERLAYS" in src
    assert '"overlay.open." +' in src
    man = json.loads(
        (UI / "fixtures" / "shell" / "manifest.json").read_text(encoding="utf-8")
    )
    # The labels are generated, so the label map must cover every overlay.
    m = re.search(r"var SH_OVERLAY_LABELS = \{([\s\S]*?)\n\};", src)
    assert m, "overlay labels must be a literal map"
    # Keys are bare identifiers where the name allows one and quoted
    # otherwise, which is the same thing to JS.
    labelled = set(re.findall(r'"?([\w-]+)"?:\s*"', m.group(1)))
    assert labelled == set(man["overlays"])


def test_overlay_labels_pass_the_registration_lint() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval((UI / "foundation" / "shell-verbs.js").read_text(encoding="utf-8"))
    src = _src()
    m = re.search(r"var SH_OVERLAY_LABELS = \{([\s\S]*?)\n\};", src)
    for label in re.findall(r':\s*"([^"]+)"', m.group(1)):
        assert ctx.eval(
            f"SH_lintVerbLabel({json.dumps(label)}) === null"
        ), label


def test_tab_labels_carry_the_status_string() -> None:
    src = _src()
    assert "SH_statusLine" in src


def test_double_click_promotes_a_preview_tab() -> None:
    src = _src()
    assert "onDoubleClick" in src
    assert "promoteDoc" in src


def test_split_groups_render_side_by_side() -> None:
    src = _src()
    assert 'data-testid={"shell-group:"' in src
    assert "docs.groups.map" in src


def test_tab_menu_is_registry_rendered() -> None:
    src = _src()
    assert 'forSurface("tab-menu")' in src
