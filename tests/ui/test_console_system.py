"""The System view (wiring plan P5 T11).

Static pins over nv-system.jsx: the nav covers the grammar's SYSNAV,
role gating keeps the admin surfaces admin-only, the dashboard carries
health + workers + the cross-workspace attention panel, and the admin
bodies re-host the existing pages rather than forking them.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SYS = (UI / "components" / "console" / "nv-system.jsx").read_text(
    encoding="utf-8")
URLJS = (UI / "foundation" / "shell-url.js").read_text(encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "styles.css").read_text(encoding="utf-8")


def test_nav_covers_the_grammar():
    m = re.search(r"system: \[([\s\S]*?)\]", URLJS)
    assert m
    navs = re.findall(r'"([a-z]+)"', m.group(1))
    for nav in navs:
        assert f'"{nav}"' in SYS, f"no row for {nav}"


def test_role_gating():
    # Non-admins get their own pages only; the admin list is the full set.
    assert "NV_SYS_USER_NAVS" in SYS
    assert '"apikeys", "profile"' in SYS
    assert 'role === "admin"' in SYS


def test_dashboard_composition():
    assert "NV_HealthCards" in SYS and '"/health"' in SYS
    assert "NV_WorkerFleet" in SYS
    assert "/workers/purge_dead" in SYS
    assert "/drain" in SYS
    assert "NV_AttentionEverywhere" in SYS
    assert "pendingYields" in SYS, "cross-workspace fan-out"


def test_admin_bodies_rehost_the_existing_pages():
    for comp in ("ADM_AdminUsersPage", "AT_ApiTokensPage",
                 "SSO_ProvidersPage", "MC_McpPage",
                 "InternalCollectionsPage", "SH_ActivityPanel",
                 "SetupWizardSteps", "LA_LinkedAccountsPage"):
        assert comp in SYS, comp


def test_profile_changes_password_via_the_auth_route():
    assert "/auth/change-password" in SYS
    assert "current_password" in SYS and "new_password" in SYS


def test_script_and_css_landed():
    sys_i = HTML.index("components/console/nv-system.jsx")
    shell_i = HTML.index("components/console/nv-shell.jsx")
    assert sys_i < shell_i
    for cls in (".nv-health-card", ".nv-worker-row", ".nv-attn-row",
                ".nv-profile-pw"):
        assert cls in CSS, cls
