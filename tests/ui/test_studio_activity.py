"""Structural-presence checks for StudioActivity (PR-B / B4).

Verifies:
  - window.StudioActivity / ActionRequired / WorkspaceActivity are exported
  - WorkspaceTap is reused (not reimplemented)
  - region-activity placeholder is gone from studio.jsx
  - all required data-testids are present in studio-activity.jsx
  - the file transpiles cleanly in the full bundle

These are static-source checks only (no React rendering), matching the
approach used in test_studio_shell.py / test_studio_sidebar.py.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
ACTIVITY = UI / "components" / "studio-activity.jsx"
STUDIO = UI / "components" / "studio.jsx"
INDEX = UI / "index.html"


def _activity_src() -> str:
    return ACTIVITY.read_text(encoding="utf-8")


def _studio_src() -> str:
    return STUDIO.read_text(encoding="utf-8")


def _index_order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


# ---------------------------------------------------------------------------
# Component existence + window exports
# ---------------------------------------------------------------------------

def test_studio_activity_file_exists_and_exports() -> None:
    assert ACTIVITY.exists(), "studio-activity.jsx missing"
    src = _activity_src()
    assert "function StudioActivity(" in src
    assert "function ActionRequired(" in src
    assert "function WorkspaceActivity(" in src
    assert "window.StudioActivity = StudioActivity;" in src
    assert "window.ActionRequired = ActionRequired;" in src
    assert "window.WorkspaceActivity = WorkspaceActivity;" in src


# ---------------------------------------------------------------------------
# WorkspaceTap reuse — must delegate; must NOT reimplement
# ---------------------------------------------------------------------------

def test_workspace_tap_reused_not_reimplemented() -> None:
    src = _activity_src()
    # Must reference window.WorkspaceTap (the existing component)
    assert "window.WorkspaceTap" in src, "WorkspaceTap not reused"
    # Must NOT reimplement the SSE connection for the activity feed (no new
    # EventSource inside WorkspaceActivity — only ActionRequired's reconcile ES)
    # We check the WorkspaceActivity function body doesn't contain its own ES open.
    # Strategy: the word "WorkspaceActivity" appears as a function header
    # before WorkspaceTap is referenced; there should be no "new EventSource"
    # inside WorkspaceActivity's definition. We extract its approximate body.
    wa_start = src.index("function WorkspaceActivity(")
    # Find the next top-level function after WorkspaceActivity
    sa_start = src.index("function StudioActivity(", wa_start)
    wa_body = src[wa_start:sa_start]
    assert "new EventSource" not in wa_body, "WorkspaceActivity must not open its own EventSource"


# ---------------------------------------------------------------------------
# region-activity placeholder replaced in studio.jsx
# ---------------------------------------------------------------------------

def test_region_activity_placeholder_gone() -> None:
    src = _studio_src()
    assert 'testid="region-activity"' not in src, "B4 placeholder still present in studio.jsx"
    assert "<StudioActivity wid={wid}" in src, "StudioActivity not wired into studio.jsx"


# ---------------------------------------------------------------------------
# data-testids (port-map §4.5)
# ---------------------------------------------------------------------------

def test_action_required_testids() -> None:
    src = _activity_src()
    for testid in (
        "action-required",
        "action-required-list",
        "action-required-count",
        "action-item",
        "action-session-link",
        "approve",
        "reject",
        "respond",
        "cancel-yield",
        "action-approval-controls",
        "action-ask-controls",
        "action-cancel-controls",
    ):
        assert f'data-testid="{testid}"' in src, f"Missing data-testid: {testid}"


def test_workspace_activity_testid() -> None:
    src = _activity_src()
    assert 'data-testid="workspace-activity"' in src
    assert 'data-testid="studio-activity-root"' in src


# ---------------------------------------------------------------------------
# Endpoint calls (verified against session-detail.jsx + approvals.jsx)
# ---------------------------------------------------------------------------

def test_approval_endpoints_present() -> None:
    src = _activity_src()
    # Approve / reject: POST /sessions/{sid}/tool_approval/respond
    assert "tool_approval/respond" in src
    assert 'decision: "approved"' in src
    assert 'decision: "rejected"' in src


def test_ask_user_respond_endpoint_present() -> None:
    src = _activity_src()
    # ask_user respond: POST /sessions/{sid}/ask_user/respond
    assert "ask_user/respond" in src


def test_cancel_yield_endpoint_present() -> None:
    src = _activity_src()
    # cancel: POST /sessions/{sid}/yields/{tcid}/cancel
    assert "/yields/" in src
    assert "/cancel" in src


# ---------------------------------------------------------------------------
# Live-reconcile strategy: SHARED workspace tap (one EventSource per Studio
# view) + debounce refetch. ActionRequired no longer opens its own EventSource
# — it reads the consolidated hub via useWorkspaceTapListener (#4 / fe-review
# N4). The single EventSource lives in foundation/use-workspace-tap.js.
# ---------------------------------------------------------------------------

def test_live_reconcile_uses_shared_tap_listener() -> None:
    src = _activity_src()
    # ActionRequired subscribes to the shared tap hub, NOT its own EventSource.
    assert "useWorkspaceTapListener" in src
    assert "new EventSource" not in src, (
        "ActionRequired must read the shared workspace-tap hub, not open its "
        "own EventSource (#4)"
    )
    # Reconcile still triggers on yielded/done events
    assert '"yielded"' in src
    assert '"done"' in src
    # Debounce prevents burst re-fetches
    assert "debounce" in src or "setTimeout" in src


def test_pending_yields_endpoint() -> None:
    src = _activity_src()
    assert "yields/pending" in src


# ---------------------------------------------------------------------------
# Bundle order: studio-activity.jsx must appear after workspace-tap.jsx
# and before studio.jsx
# ---------------------------------------------------------------------------

def test_studio_activity_registered_in_index() -> None:
    order = _index_order()
    assert "components/studio-activity.jsx" in order, "studio-activity.jsx not in index.html"


def test_studio_activity_load_order() -> None:
    order = _index_order()
    tap_idx = order.index("components/workspace-tap.jsx")
    act_idx = order.index("components/studio-activity.jsx")
    stu_idx = order.index("components/studio.jsx")
    assert tap_idx < act_idx, "studio-activity.jsx must load after workspace-tap.jsx"
    assert act_idx < stu_idx, "studio-activity.jsx must load before studio.jsx"


# ---------------------------------------------------------------------------
# Full bundle transpile gate (whole bundle must still parse cleanly)
# ---------------------------------------------------------------------------

def test_bundle_transpiles_with_studio_activity() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/studio-activity.jsx === */" in text
    assert "/* === components/studio.jsx === */" in text
