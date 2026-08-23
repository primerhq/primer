"""The Studio (revamp spec section 7): one full-screen management
area with a grouped, registry-rendered left nav, and the Activity
events console.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
HOST = (UI / "components" / "shell" / "sh-overlay-host.jsx").read_text(
    encoding="utf-8")
ACTIVITY = (UI / "components" / "shell" / "sh-activity.jsx").read_text(
    encoding="utf-8")
API = (UI / "components" / "shell" / "sh-api.jsx").read_text(encoding="utf-8")
DOC_HOST = (UI / "components" / "shell" / "sh-doc-host.jsx").read_text(
    encoding="utf-8")
MANIFEST = json.loads(
    (UI / "fixtures" / "shell" / "manifest.json").read_text(encoding="utf-8"))


def test_nav_renders_from_the_registry():
    m = re.search(r'data-testid="shell-studio-nav"[\s\S]{0,900}', HOST)
    assert m and 'forSurface("studio-nav")' in m.group(0)


def test_nav_is_grouped_and_covers_every_surface():
    m = re.search(r"var SH_STUDIO_GROUPS = \[([\s\S]*?)\];", HOST)
    assert m
    names = set(re.findall(r'"([a-z-]+)"', m.group(1)))
    names.discard("Build")
    overlays = set(MANIFEST["overlays"]) - {"new-session"}
    listed = {n for n in names if n in overlays}
    assert listed == overlays, (
        f"nav must list every studio surface; missing {overlays - listed}"
    )


def test_studio_remembers_the_last_open_surface():
    assert "primer.shell.studio" in HOST
    assert "primer.shell.studio" in DOC_HOST  # studio.open reads it back


def test_activity_is_admin_gated_and_registered():
    m = re.search(r"activity: \{[\s\S]{0,300}", HOST)
    assert m and '"admin"' in m.group(0)
    assert "SH_ActivityPanel" in m.group(0)
    assert "activity" in MANIFEST["overlays"]


def test_activity_pages_by_cursor_and_never_polls():
    assert "after_id=" in API
    assert 'data-testid="activity-load-more"' in ACTIVITY
    assert "pollMs" not in ACTIVITY, "the log is a window, not a tail -f"
