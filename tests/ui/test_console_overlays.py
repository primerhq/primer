"""The overlay set (wiring plan P3 T9).

Static pins over nv-overlays.jsx: the URL-addressed host dispatches the
two designer panels plus the re-hosted legacy surfaces, the create verbs
open the same overlays the URL grammar addresses, and the submit bodies
keep the SharedNewSessionForm / WorkspaceCreateBody contracts.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
OVERLAYS = (UI / "components" / "console" / "nv-overlays.jsx").read_text(
    encoding="utf-8")
SHELL = (UI / "components" / "console" / "nv-shell.jsx").read_text(
    encoding="utf-8")
URLJS = (UI / "foundation" / "shell-url.js").read_text(encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "styles.css").read_text(encoding="utf-8")


def test_new_workspace_is_addressable():
    assert '"new-workspace"' in URLJS, "the grammar names the overlay"


def test_create_verbs_open_the_shared_overlays():
    m = re.search(r'id: "session.create"[\s\S]{0,400}', SHELL)
    assert m and '"new-session"' in m.group(0)
    m = re.search(r'id: "workspace.create"[\s\S]{0,400}', SHELL)
    assert m and '"new-workspace"' in m.group(0)


def test_host_dispatches_three_tiers():
    assert 'overlay.name === "new-session"' in OVERLAYS
    assert 'overlay.name === "new-workspace"' in OVERLAYS
    assert "NV_OVERLAY_MOUNTS[overlay.name]" in OVERLAYS
    assert "NV_LegacyOverlay" in OVERLAYS
    assert "<window.NV_OverlayHost />" in SHELL


def test_panel_closes_by_esc_and_scrim():
    assert '"Escape"' in OVERLAYS
    assert 'className="nv-scrim"' in OVERLAYS
    assert "stopPropagation" in OVERLAYS, "panel clicks must not close it"


def test_session_body_keeps_the_shared_form_contract():
    # Omitting binding asks for the system default agent.
    m = re.search(r"function submit\(\)[\s\S]{0,900}", OVERLAYS)
    assert m and "auto_start" in m.group(0)
    assert "if (bind)" in m.group(0)
    assert "graph_input" in OVERLAYS
    assert "initial_instructions" in OVERLAYS
    # The graph Begin.input_schema form reuses the ONE schema field.
    assert "SharedNewSessionSchemaField" in OVERLAYS


def test_workspace_body_maps_the_designer_overrides():
    assert '"/workspaces"' in OVERLAYS
    assert "template_id" in OVERLAYS
    assert "overrides.env" in OVERLAYS or "body.overrides.env" in OVERLAYS
    assert "init_commands" in OVERLAYS


def test_legacy_pages_keep_router_and_role_contracts():
    assert "SH_installRouterShim" in OVERLAYS
    assert "mount.roles" in OVERLAYS
    assert "switchWorkspace" in OVERLAYS
    assert "nv-overlay-crumb" in OVERLAYS


def test_script_registered_before_the_shell():
    ov = HTML.index("components/console/nv-overlays.jsx")
    sh = HTML.index("components/console/nv-shell.jsx")
    assert ov < sh


def test_overlay_css_landed():
    for cls in (".nv-scrim", ".nv-overlay-panel", ".nv-bind-menu",
                ".nv-pick-row", ".nv-kv-row", ".nv-form-error"):
        assert cls in CSS, cls
    assert "prefers-reduced-motion" in CSS
