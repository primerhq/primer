"""The three-view chrome (wiring plan P1 T4).

Static pins over the nv- shell skeleton: regions render, affordances
run registered verbs, view switching rides the URL, the profile
dropdown role-gates System, and the bundle transpiles the new files.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SHELL = (UI / "components" / "console" / "nv-shell.jsx").read_text(
    encoding="utf-8")
CHROME = (UI / "components" / "console" / "nv-chrome.jsx").read_text(
    encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")
APP = (UI / "app.jsx").read_text(encoding="utf-8")


def test_regions_render():
    for tid in ("nv-root", "nv-actbar", "nv-topbar"):
        src = SHELL if tid == "nv-root" else CHROME
        assert f'data-testid="{tid}"' in src, tid


def test_flag_gates_the_mount():
    # Flag day (P7): the console mounts unconditionally; both the tweak
    # and the old shell's gate are gone.
    assert "NV_Shell" in APP
    assert "consoleNext" not in APP
    assert "SH_RootGate" not in APP


def test_affordances_run_registered_verbs():
    # Chrome never hardcodes behavior: clicks resolve registry verbs.
    for verb in ("view.studio", "view.platform", "view.system",
                 "workspace.switch", "workspace.create", "palette.open",
                 "terminal.toggle"):
        assert verb in SHELL, f"{verb} not registered"
    assert 'registry.get' in CHROME
    assert re.search(r'data-verb="view\.studio"', CHROME)


def test_registered_chords_are_live_bindings():
    # One dispatcher walks the registry: a verb's chord can never be
    # declared and then silently dead (Ctrl+N was exactly that once).
    assert "chordMatches" in SHELL
    assert "registry.all()" in SHELL
    assert 'v.id === "palette.open"' in SHELL, "the palette owns Ctrl+K"


def test_view_switch_rides_the_url():
    assert "SH_buildUrl" in SHELL and "SH_parseUrl" in SHELL
    assert "pushState" in SHELL
    assert "hashchange" in SHELL and "popstate" in SHELL


def test_profile_menu_role_gates_system():
    m = re.search(r'con\.role !== "restricted"[\s\S]{0,400}', CHROME)
    assert m and "view.system" in m.group(0)


def test_workspace_switch_keeps_open_tabs():
    """Superseded by US-007 R2: tabs are global across workspaces (notes
    2.3 - "any workspace's docs can co-exist"), so switching workspace no
    longer drops the open doc. It only changes which workspace drives the
    Files sidebar/terminal/rail selection - the pre-R2 single-doc-per-
    workspace model this test used to pin is gone."""
    # Window widened past 400 (2026-08-29 UI review, F6): the chord/
    # surfaces explanatory comment now sits ahead of the executable body.
    m = re.search(r'id: "workspace.switch"[\s\S]{0,800}', SHELL)
    assert m and "setDoc(null)" not in m.group(0)
    assert m and "setWid(arg.wid)" in m.group(0)


def test_scripts_registered_before_app():
    nv = HTML.index("components/console/nv-shell.jsx")
    app = HTML.index('src="app.jsx"')
    assert nv < app
    assert "components/console/nv-chrome.jsx" in HTML
