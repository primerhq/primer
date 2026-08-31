"""Terminal panel (wiring plan P2 T8).

The workspace events sidebar (nv-events-sidebar.jsx: streaming opt-in,
cursor-paged tail, role-denial fallback) retired in the uiv2 US-011a
cutover - the toggle, the mount and its dedicated sh-api.jsx helper
(setWorkspaceEvents) are gone with it. The terminal panel is unrelated
and survives untouched.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "ui" / "components" / "console"
TERM = (CONSOLE / "nv-terminal.jsx").read_text(encoding="utf-8")


def test_terminal_rides_the_pty_websocket():
    assert "/terminal" in TERM
    assert "window.Terminal" in TERM and "FitAddon" in TERM
    assert "resize" in TERM
    assert 'data-testid="nv-terminal-denied"' in TERM


# ---------------------------------------------------------------------------
# F8(a): denied (backend's real refusal signal, close code 4403 - see
# primer/api/routers/terminal.py's role gate, already tested by
# tests/api/test_terminal.py::test_non_admin_denied_when_terminal_user_access_off)
# is a distinct, non-retryable state from a transient connection-lost blip.
# ---------------------------------------------------------------------------


def test_denied_is_read_from_the_explicit_4403_close_code():
    start = TERM.index("sock.onclose = function (ev) {")
    end = TERM.index("\n    };", start)
    body = TERM[start:end]
    assert "ev.code === 4403" in body
    assert "setDenied(true)" in body


def test_every_other_close_is_connection_lost_not_denied():
    start = TERM.index("sock.onclose = function (ev) {")
    end = TERM.index("\n    };", start)
    body = TERM[start:end]
    assert "setConnLost(true)" in body
    assert 'data-testid="nv-terminal-conn-lost"' in TERM


def test_a_clean_shell_exit_is_not_read_as_connection_lost():
    """{"exit": code} then a normal close is expected termination, not a
    lost connection - must keep showing the transcript + exit badge."""
    start = TERM.index("sock.onclose = function (ev) {")
    end = TERM.index("\n    };", start)
    body = TERM[start:end]
    assert "gotExit" in body


def test_our_own_teardown_close_never_flips_state():
    """The cleanup's sock.close() must not read back as connection-lost -
    a retry or unmount would otherwise flash the retry empty state."""
    start = TERM.index("sock.onclose = function (ev) {")
    end = TERM.index("\n    };", start)
    body = TERM[start:end]
    assert "cancelled" in body
    cleanup_start = TERM.index("return function () {", end)
    cleanup_end = TERM.index("};", cleanup_start)
    cleanup_body = TERM[cleanup_start:cleanup_end]
    assert "cancelled = true" in cleanup_body


def test_retry_affordance_reconnects_via_the_effect_dependency():
    assert 'data-testid="nv-terminal-retry"' in TERM
    assert "retryToken" in TERM
    assert "[con.wid, retryToken]" in TERM


# ---------------------------------------------------------------------------
# F8(b): top-edge drag-resize, 100px-70vh (design).
# ---------------------------------------------------------------------------


def test_resize_handle_present_and_clamped_100_to_70vh():
    assert 'data-testid="nv-terminal-resize-handle"' in TERM
    assert "NV_TERMINAL_MIN_HEIGHT = 100" in TERM
    assert "window.innerHeight * 0.7" in TERM


def test_resize_clamps_both_bounds():
    start = TERM.index("var startResize = function")
    end = TERM.index("\n  };", start)
    body = TERM[start:end]
    assert "next < NV_TERMINAL_MIN_HEIGHT" in body
    assert "next > max" in body


# ---------------------------------------------------------------------------
# F8(c): header shows the workspace NAME + "pty", not the raw wid.
# ---------------------------------------------------------------------------


def test_header_resolves_the_workspace_name_not_the_raw_wid():
    assert "con.workspaces" in TERM
    assert "ws.name || ws.id" in TERM
    assert "· pty" in TERM
    # The old bare "terminal · {con.wid}" header text must be gone.
    assert "terminal · {con.wid}" not in TERM


# ---------------------------------------------------------------------------
# Cross-review MEDIUM (against F8(b)): startResize's window mousemove/
# mouseup listeners were removed only by their own mouseup - an unmount
# mid-drag left them attached forever, and the next mousemove called the
# by-then-disposed xterm fit addon's .fit(), which throws.
# ---------------------------------------------------------------------------


def test_start_resize_tracks_the_live_drag_pair_in_a_ref():
    start = TERM.index("var startResize = function")
    end = TERM.index("\n  };", start)
    body = TERM[start:end]
    # Stamped before the listeners attach, so an unmount mid-drag has
    # something live to clean up.
    assert "dragCleanupRef.current = onUp;" in body
    assert body.index("dragCleanupRef.current = onUp;") < body.index(
        'window.addEventListener("mousemove", onMove);'
    )
    # onUp clears it on a normal mouseup, so a completed drag leaves
    # nothing for the unmount effect to redundantly tear down.
    assert "dragCleanupRef.current = null;" in body


def test_unmount_tears_down_a_still_active_drag():
    """The fix must be a REAL unmount cleanup, not just a ref that sits
    there unused - assert the mount-scoped effect exists AND actually
    calls the tracked pair's teardown."""
    start = TERM.index("React.useEffect(function () {\n    return function () {\n      if (dragCleanupRef.current)")
    assert start >= 0, "no unmount effect found tearing down dragCleanupRef"
    end = TERM.index("}, []);", start)
    body = TERM[start:end]
    assert "dragCleanupRef.current()" in body
