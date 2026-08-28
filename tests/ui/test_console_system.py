"""The System view (wiring plan P5 T11).

Static pins over nv-system.jsx: the nav covers the grammar's SYSNAV,
role gating keeps the admin surfaces admin-only, the dashboard carries
health + workers + the cross-workspace attention panel, and the admin
bodies re-host the existing pages rather than forking them.
"""

from __future__ import annotations

import json
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
    # R5 ruling: notes section 4's own intro - "all admin-gated except
    # Profile (restricted users see only Profile)" - applies to every
    # non-admin role, not just restricted; the old NV_SYS_USER_NAVS =
    # ["apikeys", "profile"] carve-out was drift (apikeys used to double
    # as personal tokens; that moved to Profile, see test_console_system's
    # tokens-ruling tests below). Executed via MiniRacer, not grepped -
    # a substring check on this file would coincidentally match its own
    # explanatory comments about the OLD behavior.
    from py_mini_racer import MiniRacer

    # Extract just the (JSX-free) function, not the whole file - the rest
    # of nv-system.jsx is React/JSX, which MiniRacer cannot parse raw.
    start = SYS.index("function NV_sysNavsFor(")
    end = SYS.index("\n}\n", start) + 2
    fn_src = SYS[start:end]

    ctx = MiniRacer()
    ctx.eval(fn_src)
    for role in ("user", "restricted", None):
        ctx.eval(f"var out = NV_sysNavsFor({json.dumps(role)});")
        navs = json.loads(ctx.eval("JSON.stringify(out)"))
        assert navs == ["profile"], (role, navs)
    ctx.eval('var adminOut = NV_sysNavsFor("admin");')
    admin_navs = json.loads(ctx.eval("JSON.stringify(adminOut)"))
    assert admin_navs == [
        "dashboard", "users", "apikeys", "sso", "mcp", "internal",
        "activity", "setup", "profile",
    ]


def test_dashboard_composition():
    assert "NV_HealthCards" in SYS and '"/health"' in SYS
    assert "NV_WorkerFleet" in SYS
    assert "/workers/purge_dead" in SYS
    assert "/drain" in SYS
    assert "NV_AttentionEverywhere" in SYS
    # R5: the real cross-workspace aggregate (batch-1's GET /yields/pending,
    # SH_api.pendingAttention) is now the PRIMARY source; pendingYields
    # (per-workspace fan-out) survives only as the pre-aggregate-server
    # fallback, same guard shape as nv-rail.jsx's own Inbox.
    assert "pendingAttention" in SYS
    assert "pendingYields" in SYS, "404 fallback fan-out"


def test_health_cards_match_notes_not_the_old_scheduler_dump():
    # notes section 4: scheduler / worker pool / sessions active /
    # attention count - NOT the old platform/in-flight/claims/missed-
    # heartbeats scheduler-internals dump.
    for k in ("scheduler", "worker pool", "sessions active", "attention"):
        assert f'"{k}"' in SYS, k
    assert "status=running" in SYS, "sessions-active reads GET /sessions?status=running"


def test_worker_rows_carry_turns_and_uptime():
    # notes section 4: "worker rows show ... turns + uptime". Turns come
    # from grouping /workers/stats by worker id (workers.jsx's own lane
    # stats source, just aggregated per-row here); uptime is computed off
    # WorkerInfo.started_at.
    assert "/workers/stats" in SYS
    assert "turnsByWorker" in SYS
    assert "w.started_at" in SYS


def test_attention_open_uses_the_aggregate_fields_and_promotes():
    # workspace_name/session_name come straight off the aggregate row
    # (no client-side workspace join needed); promoteDoc matches the
    # rail's own promoted-open contract (notes 2.1/2.2).
    assert "row.workspace_name" in SYS
    assert "row.session_name" in SYS
    assert "con.promoteDoc" in SYS


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
