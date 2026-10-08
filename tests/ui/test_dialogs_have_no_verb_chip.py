"""No dialog wears a developer "verb:" chip, and Create session says which workspace it creates in (lead sweep 2026-10-08, L3).

The uiv2 mockup annotates each panel with its command-palette verb ("verb: Create Session", a mono-spaced pill beside the title). That
is a designer's note, and it shipped as UI in the overlay panel and in three legacy create/edit modals. A user cannot act on an
internal verb id. Separately, the Create session dialog never said which workspace the session goes into (the selected one).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
OVERLAYS = (UI / "components" / "console" / "nv-overlays.jsx").read_text(encoding="utf-8")
STYLES = (UI / "styles.css").read_text(encoding="utf-8")

_CHIP_MARKERS = (
    "nv-verb-chip", "agent-modal-verb-chip", "workspace-modal-verb-chip", "profile-modal-verb-chip",
)


def _jsx_files():
    for path in sorted((UI / "components").rglob("*.jsx")):
        yield path, path.read_text(encoding="utf-8")


def test_no_component_renders_a_verb_chip() -> None:
    offenders = []
    for path, src in _jsx_files():
        for marker in _CHIP_MARKERS:
            if marker in src:
                offenders.append(f"{path.relative_to(UI)}: {marker}")
        # The rendered text itself ("verb: Create Agent", "verb: {props.verb}"), whatever the test id.
        for match in re.finditer(r">\s*verb:\s", src):
            offenders.append(f"{path.relative_to(UI)}: rendered 'verb: ' text at {match.start()}")
    assert not offenders, "a developer verb chip is rendered:\n  " + "\n  ".join(offenders)


def test_the_overlay_panel_takes_no_verb_and_no_caller_passes_one() -> None:
    panel = OVERLAYS[OVERLAYS.index("function NV_OverlayPanel"):OVERLAYS.index("// Field primitive")]
    assert "props.verb" not in panel
    assert not re.search(r"<NV_OverlayPanel[^>]*\bverb=", OVERLAYS)


def test_the_chip_style_is_gone_with_the_chip() -> None:
    assert ".nv-verb-chip" not in STYLES


def _workspace_label():
    from py_mini_racer import MiniRacer

    start = OVERLAYS.index("function NV_workspaceLabel")
    end = OVERLAYS.index("\n}\n", start) + len("\n}\n")
    ctx = MiniRacer()
    ctx.eval(OVERLAYS[start:end])
    return lambda workspaces, wid: json.loads(
        ctx.eval("JSON.stringify(NV_workspaceLabel(" + json.dumps(workspaces) + ", " + json.dumps(wid) + "))")
    )


def test_the_target_workspace_is_named_by_its_name_then_its_id() -> None:
    label = _workspace_label()
    spaces = [{"id": "ws-1", "name": "Primer"}, {"id": "ws-2", "name": None}, {"id": "ws-3"}]
    assert label(spaces, "ws-1") == "Primer"
    assert label(spaces, "ws-2") == "ws-2", "an unnamed workspace is shown by its id"
    assert label(spaces, "ws-3") == "ws-3"
    assert label(spaces, "ws-9") == "ws-9", "a workspace not in the list yet is still named, by its id"
    assert label([], "ws-1") == "ws-1"
    assert label(None, "ws-1") == "ws-1"
    assert label(spaces, "") is None and label(spaces, None) is None, "no workspace selected: nothing to name"


def test_the_create_session_dialog_shows_the_target_workspace() -> None:
    body = OVERLAYS[OVERLAYS.index("function NV_CreateSessionOverlay"):OVERLAYS.index("function NV_CreateWorkspaceOverlay")]
    assert 'data-testid="nv-ns-workspace"' in body
    assert "NV_workspaceLabel(con.workspaces, con.wid)" in body
