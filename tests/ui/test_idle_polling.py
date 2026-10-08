"""An open, idle session document does not ask the server for the same facts every two seconds (console review 2026-10-08, C-038).

One open session that was doing nothing made about 110 requests a minute (the review's probe: 82 requests in 45 s): the session row every
2 s, the pending yields, the external-tool banner, the session list, the Files tree and the workspace list (twice, under two cache keys)
every 5 to 15 s. With the live tap connected and nothing in flight, the tap and the refetch on its frames carry every change; the polls only
catch up, so they slow to 15 s. They stay fast whenever the tap is not live or anything is running or about to, and a tap frame, a
Stop, a send or a running row brings the fast cadence back at once.

The decisions are pure and run in V8; the request count of a real idle session document is checked in a browser by
``tests/ui_e2e/test_idle_session_polling_journey.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "ui" / "components" / "console"
DOC = (CONSOLE / "nv-session-doc.jsx").read_text(encoding="utf-8")
RAIL = (CONSOLE / "nv-rail.jsx").read_text(encoding="utf-8")
SHELL = (CONSOLE / "nv-shell.jsx").read_text(encoding="utf-8")
FILES = (CONSOLE / "nv-files-sidebar.jsx").read_text(encoding="utf-8")
EXTERNAL = (ROOT / "ui" / "components" / "external-tools.jsx").read_text(encoding="utf-8")


def _function(src: str, name: str) -> str:
    start = src.index("function " + name + "(")
    return src[start:src.index("\n}\n", start) + len("\n}\n")]


@pytest.fixture
def ctx():
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval("var window = globalThis;")
    c.eval(_function(DOC, "NV_sessionIsCalm") + "var NV_CALM_POLL_MS = 15000;")
    c.eval(_function(RAIL, "NV_anySessionRunning"))
    try:
        yield c
    finally:
        c.close()


def _calm(ctx, **over) -> bool:
    base = {
        "tapLive": True, "live": False, "optimistic": False, "stopPending": False,
        "row": {"status": "waiting", "turn_status": "idle", "interrupt_requested": False, "pause_requested": False},
    }
    base.update(over)
    return ctx.eval("NV_sessionIsCalm(" + json.dumps(base) + ")")


def test_a_session_at_rest_with_a_live_tap_is_calm(ctx) -> None:
    assert _calm(ctx) is True


@pytest.mark.parametrize("over", [
    {"tapLive": False},                                        # nothing delivers changes but the poll
    {"live": True},                                            # the tap is showing a running status
    {"optimistic": True},                                      # a send is in flight
    {"stopPending": True},                                     # a Stop is waiting for the served flag
    {"row": None},                                             # not loaded yet
    {"row": {"status": "running", "turn_status": "running"}},  # a turn is executing
    {"row": {"status": "waiting", "turn_status": "running"}},
    {"row": {"status": "waiting"}},                            # a row that does not say it is idle says nothing
    {"row": {"status": "waiting", "turn_status": "idle", "interrupt_requested": True}},
    {"row": {"status": "waiting", "turn_status": "idle", "pause_requested": True}},
    {"row": {"status": "waiting", "turn_status": "idle", "cancel_requested": True}},      # a Cancel is landing: the worker has not finished it
    {"row": {"status": "running", "turn_status": "idle"}},                                 # the row says running even though no turn is
    {"row": {"status": "running", "turn_status": "idle", "interrupt_requested": False, "pause_requested": False}},
])
def test_anything_in_flight_or_unknown_keeps_the_fast_cadence(ctx, over) -> None:
    assert _calm(ctx, **over) is False, over


def test_a_parked_paused_or_never_started_session_at_rest_is_calm(ctx) -> None:
    for status in ("waiting", "paused", "created"):
        assert _calm(ctx, row={"status": status, "turn_status": "idle"}) is True, status


def test_a_workspace_is_busy_only_when_one_of_its_own_sessions_runs(ctx) -> None:
    def busy(data, wid) -> bool:
        return ctx.eval("NV_anySessionRunning(" + json.dumps(data) + ", " + json.dumps(wid) + ")")

    data = {"items": [{"session_id": "a", "workspace_id": "w1", "status": "running"}, {"session_id": "b", "workspace_id": "w2", "status": "waiting"}]}
    assert busy(data, "w1") is True and busy(data, "w2") is False
    assert busy({"items": []}, "w1") is False
    assert busy({"items": [{"session_id": "a", "workspace_id": "w1", "status": "waiting", "session_state": "running"}]}, "w1") is True
    assert busy({"items": [{"session_id": "a", "workspace_id": "w1", "status": "waiting", "turn_status": "running"}]}, "w1") is True
    assert busy(data, None) is True, "no workspace named: any session counts"
    assert busy(None, "w2") is True, "unknown is treated as busy"


# --- where the cadences are used -----------------------------------------------------------------------------------------------


def test_the_session_row_and_the_pending_yields_follow_the_calm_state() -> None:
    head = DOC[DOC.index("var gatesSnap = window.useSessionStore"):DOC.index("var resolvedRecords")]
    assert "NV_sessionIsCalm(" in head
    assert "pollMs: pollStopped ? 0 : (calm ? NV_CALM_POLL_MS : 2000)" in head, "the session row"
    assert "pollMs: calm ? NV_CALM_POLL_MS : 5000" in head, "the pending yields"
    assert "ignoreIdle: true" in head


def test_the_slow_cadence_starts_only_after_the_session_has_been_at_rest_for_a_while() -> None:
    """A turn's last frame can arrive before the row shows where the session came to rest, so a stale "waiting" chip stayed up for a whole
    slow interval right after a turn (the phase-indicator journey caught it). Calm is a state set by a timer that restarts whenever the
    session stirs, and cleared the moment it does."""
    head = DOC[DOC.index("var calmState = React.useState(false);"):DOC.index("var gates = window.primerApi.useResource")]
    assert "var NV_CALM_AFTER_MS = 5000;" in DOC
    assert "setTimeout(function () { setCalm(true); }, NV_CALM_AFTER_MS)" in head
    assert "if (!atRest) { setCalm(false); return undefined; }" in head
    assert "return function () { clearTimeout(timer); };" in head


def test_a_tap_frame_for_this_session_refetches_the_row_so_a_slow_poll_never_delays_it() -> None:
    listener = DOC[DOC.index("window.useWorkspaceTapListener(con.wid, function (ev) {"):]
    listener = listener[:listener.index("\n  });")]
    assert "detail.refetch()" in listener and "gates.refetch()" in listener


def test_the_external_tool_banner_takes_its_cadence_from_the_session_doc() -> None:
    assert "pollMs = 5000" in EXTERNAL or "pollMs: 5000" not in EXTERNAL
    assert re.search(r"function ExternalPendingBanner\(\{[^}]*pollMs", EXTERNAL)
    mount = DOC[DOC.index("<window.ExternalPendingBanner"):]
    assert "pollMs={calm ? NV_CALM_POLL_MS : 5000}" in mount[:300]


def test_the_rail_the_shell_and_the_files_sidebar_share_one_session_list_subscription() -> None:
    """One definition, so the three readers of the cache key cannot disagree about its cadence. It stays at 5 s: a session created
    elsewhere emits no tap frame, so the poll is how it appears in the rail."""
    assert "function NV_useSessionList()" in RAIL and "window.NV_useSessionList = NV_useSessionList;" in RAIL
    assert "var NV_RAIL_SESSIONS_POLL_MS = 5000;" in RAIL
    assert "NV_useSessionList()" in _function(RAIL, "NV_Rail")
    assert "window.NV_useSessionList()" in SHELL
    assert "window.NV_useSessionList()" in FILES
    for src, name in ((RAIL, "rail"), (SHELL, "shell")):
        assert '"nv-rail-all-sessions",\n    function (signal) { return SH_api.allSessions(signal); },\n    { pollMs: 5000' not in src, name
    assert "NV_CALM" not in _function(RAIL, "NV_useSessionList"), "the list is never slowed"


def test_the_files_tree_polls_slowly_when_no_session_of_the_workspace_runs() -> None:
    assert "pollMs: filesPollMs" in FILES
    assert "NV_anySessionRunning(" in FILES


def test_the_workspace_list_is_one_poll_not_two() -> None:
    """The shell and the rail both fetched GET /workspaces every 15 s under different cache keys."""
    assert '"nv-rail-workspaces"' not in RAIL
    assert '"nv-workspaces"' in RAIL and '"nv-workspaces"' in SHELL
