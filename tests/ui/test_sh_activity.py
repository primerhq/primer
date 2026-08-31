"""Static JSX checks for the Activity console's event-subscriptions table
(R5 item 6, notes section 4 + docs/superpowers/uiv2/03-backend-gap-map.md:156
BACKEND-GAP #12).

The masked log (SH_ActivityPanel) already existed; this pins the ADDED
subscriptions table: managed rows refuse edits (server-enforced, no UI
action needed) and refuse pause too (client-side, since the live pause
endpoint has zero server guard on managed rows today - the gap map's
"only one managed_by tier exists" finding), unmanaged rows Pause/Resume
for real against the backend.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACTIVITY = ROOT / "ui" / "components" / "shell" / "sh-activity.jsx"
SH_API = ROOT / "ui" / "components" / "shell" / "sh-api.jsx"


def _activity_src() -> str:
    return ACTIVITY.read_text()


def _api_src() -> str:
    return SH_API.read_text()


def test_subscriptions_table_component_defined_and_exported() -> None:
    src = _activity_src()
    assert "function SH_EventSubscriptionsTable" in src
    assert "window.SH_EventSubscriptionsTable = SH_EventSubscriptionsTable" in src


def test_activity_panel_renders_the_subscriptions_table() -> None:
    src = _activity_src()
    assert "<SH_EventSubscriptionsTable" in src


def test_table_testid_and_rows() -> None:
    src = _activity_src()
    assert "activity-subscriptions" in src
    assert '"sub-row:"' in src
    assert '"sub-toggle-pause:"' in src


def test_managed_rows_never_call_the_pause_endpoint() -> None:
    """The refusal must happen BEFORE the API call, not send-then-ignore -
    the live endpoint has no server guard (gap map), so an actual PATCH
    would silently succeed even though the UI claims to refuse it."""
    src = _activity_src()
    start = src.index("function togglePause(")
    end = src.index("\n  }", start)
    body = src[start:end]
    guard_idx = body.index("if (row.managed_by)")
    return_idx = body.index("return;", guard_idx)
    call_idx = body.find("SH_api.setSubscriptionPaused(")
    assert guard_idx < return_idx
    assert call_idx == -1 or return_idx < call_idx


def test_managed_refusal_shows_a_warning_toast() -> None:
    src = _activity_src()
    start = src.index("function togglePause(")
    end = src.index("\n  }", start)
    body = src[start:end]
    assert '"warning"' in body
    assert "toastPush" in body


def test_unmanaged_rows_call_the_real_pause_endpoint() -> None:
    src = _activity_src()
    assert "SH_api.setSubscriptionPaused(row.id, !row.paused)" in src


def test_managed_badge_present() -> None:
    src = _activity_src()
    assert "managed" in src.lower()


def test_sh_api_has_event_subscription_helpers() -> None:
    src = _api_src()
    assert "eventSubscriptions: function" in src
    assert "/event_subscriptions" in src
    assert "setSubscriptionPaused: function" in src
    assert "/paused" in src


def test_sh_activity_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    b = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    code = b._transform(ACTIVITY.read_text(), "components/shell/sh-activity.jsx")
    assert code and "SH_EventSubscriptionsTable" in code
