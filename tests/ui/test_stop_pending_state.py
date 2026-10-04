"""The console acknowledges a Stop: a 'stopping' state, a disabled Stop, a toast.

Before this the console showed NOTHING after Stop: no toast, no state, and nothing read the served
``interrupt_requested`` flag, so the status strip kept saying ``running: <tool>`` with Stop still
enabled. An operator could not tell a lost click from a slow stop, and the natural reaction was to
press Close (Cancel), which ENDS the session.

The state is derived from the SERVED flag, not guessed: ``interrupt_requested`` is true from the moment
POST .../interrupt is recorded until the worker lands the Stop (it then clears it, and the session
reads ``waiting``). The pure decisions live in ``ui/foundation/shell-status.js`` and are driven here
through MiniRacer against the real source; the wiring is checked against the component source, as the
neighbouring console tests do.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL_STATUS = ROOT / "ui" / "foundation" / "shell-status.js"
CONSOLE = ROOT / "ui" / "components" / "console"
DOC = (CONSOLE / "nv-session-doc.jsx").read_text(encoding="utf-8")
RAIL = (CONSOLE / "nv-rail.jsx").read_text(encoding="utf-8")
STYLES = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(SHELL_STATUS.read_text(encoding="utf-8"))
    return ctx


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _function_source(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    return src[start:src.index("\n}\n", start) + 3]


# ---- SH_isStopping: derived from the served flag ----------------------------------------------


def test_a_live_session_with_the_flag_set_is_stopping() -> None:
    ctx = _ctx()
    assert _js(ctx, 'SH_isStopping({status: "running", interrupt_requested: true})') is True


def test_the_flag_clearing_ends_the_state_so_the_button_comes_back() -> None:
    """The worker clears interrupt_requested when the Stop lands; the state must end with it."""
    ctx = _ctx()
    assert _js(ctx, 'SH_isStopping({status: "waiting", interrupt_requested: false})') is False


def test_nothing_is_stopping_without_the_flag_or_a_session_or_after_the_session_ended() -> None:
    ctx = _ctx()
    assert _js(ctx, "SH_isStopping(null)") is False
    assert _js(ctx, "SH_isStopping({})") is False
    assert _js(ctx, 'SH_isStopping({status: "running"})') is False
    # An ended session is never "still stopping": a stale flag on it must not hold the UI in a pending state.
    assert _js(ctx, 'SH_isStopping({status: "ended", interrupt_requested: true})') is False


def test_a_parked_session_is_never_stopping() -> None:
    """No turn is running while a session is parked, and the server refuses a Stop on one (409), so a
    leftover flag on a parked row must not put the console in a 'stopping' state."""
    ctx = _ctx()
    for parked in ("parked", "resumable"):
        assert _js(
            ctx, f'SH_isStopping({{status: "running", parked_status: "{parked}", interrupt_requested: true}})',
        ) is False


# ---- SH_stoppingLine: what the strip says, honestly ------------------------------------------


def test_a_stop_during_the_model_wait_just_says_stopping() -> None:
    ctx = _ctx()
    for verb in ("thinking", "sending", "responding"):
        assert _js(ctx, f'SH_stoppingLine({{verb: "{verb}"}})') == "stopping"


def test_a_stop_during_a_tool_says_it_waits_for_the_tool_by_name() -> None:
    """Until a running tool can be cancelled, a Stop lets it finish: say so instead of implying it is instant."""
    ctx = _ctx()
    assert _js(ctx, 'SH_stoppingLine({verb: "grep_src", object: "src/"})') == "stopping: waiting for grep_src to finish"


def test_a_stop_while_a_tool_runs_but_its_name_is_unknown_says_the_running_tool() -> None:
    ctx = _ctx()
    assert _js(ctx, 'SH_stoppingLine({verb: "executing"})') == "stopping: waiting for the running tool to finish"


def test_the_line_is_plain_text_with_no_em_dash_and_no_status_prefix() -> None:
    ctx = _ctx()
    line = _js(ctx, 'SH_stoppingLine({verb: "grep_src"})')
    assert chr(0x2014) not in line and not line.startswith("running")


# ---- SH_lifecycleLabel: a Stop reads as stopped, a Cancel as cancelled ----------------------------


def test_a_stop_marker_reads_stopped_and_a_cancel_reads_cancelled() -> None:
    ctx = _ctx()
    assert _js(ctx, 'SH_lifecycleLabel("cancelled", {reason: "operator_interrupt"})') == "■ stopped"
    assert _js(ctx, 'SH_lifecycleLabel("cancelled", {reason: "cancelled"})') == "■ cancelled"
    assert _js(ctx, 'SH_lifecycleLabel("cancelled", null)') == "■ cancelled"


def test_the_other_lifecycle_markers_keep_their_labels() -> None:
    ctx = _ctx()
    assert _js(ctx, 'SH_lifecycleLabel("done", {})') == "· done"
    assert _js(ctx, 'SH_lifecycleLabel("yielded", {})') == "· yielded"


# ---- wiring: the component uses them -----------------------------------------------------------


def test_the_composer_stop_button_is_disabled_and_relabelled_while_stopping() -> None:
    m = re.search(r'<button type="button" className="nv-stop-btn"[\s\S]{0,420}?</button>', DOC)
    assert m, "the composer Stop button is gone"
    button = m.group(0)
    assert "disabled={props.stopping}" in button
    assert "Stopping" in button


def test_the_status_strip_says_stopping_and_disables_its_interrupt_button() -> None:
    strip = _function_source(DOC, "NV_StatusStrip")
    assert "window.SH_stoppingLine" in strip and "props.stopping" in strip
    button = re.search(r'<button type="button" className="nv-interrupt-btn"[\s\S]{0,400}?</button>', strip).group(0)
    assert "disabled={props.stopping}" in button
    assert 'data-pending=' in button, "the pending state must be addressable for tests and styling"


def test_stopping_is_the_served_flag_or_a_request_still_in_flight() -> None:
    """Immediate feedback on click (the request is pending), then the served flag takes over."""
    assert "window.SH_isStopping(session)" in DOC
    assert re.search(r"var stopping = [^;]*stopPending[^;]*SH_isStopping\(session\)", DOC), (
        "stopping must be (a click whose request has not settled) OR (the served flag)"
    )
    assert "stopping={stopping}" in DOC, "the composer (desktop and mobile share it) must be told"


def test_every_entry_point_shares_one_guarded_function_that_toasts() -> None:
    fn = _function_source(DOC, "NV_doInterrupt")
    assert "NV_STOP_IN_FLIGHT" in fn, "a second click while the request is pending must not fire another"
    assert re.search(r'toast\("Stopping', fn), "success must be acknowledged with a toast"
    assert 'toast("Interrupt failed: "' in fn, "the existing failure toast is kept"
    assert "window.NV_doInterrupt = NV_doInterrupt;" in DOC


def test_the_rail_menu_does_not_offer_interrupt_on_a_parked_session() -> None:
    """A parked session has no turn to stop and the server answers 409; offering the row would only
    produce an error. Park and End stay (a parked session can still be ended)."""
    menu = _function_source(RAIL, "NV_Rail_SessionContextMenu")
    interrupt_row = re.search(r'if \(![^)]*parked_status[^)]*\) \{\s*rows\.push\(act\("Interrupt"', menu)
    assert interrupt_row, "the Interrupt row must be gated on the session not being parked"
    assert 'rows.push(act("End"' in menu and 'rows.push(act("Park"' in menu


def test_a_refused_stop_shows_the_servers_honest_reason_and_clears_the_pending_state() -> None:
    ctx = _interrupt_ctx(rejects=True)
    detail = (
        "Session 's': no turn is running; the session is waiting for you "
        "(an approval, an answer or a timer). Use Cancel to end it."
    )
    ctx.eval(
        "SH_api.interrupt = function () { calls++; return Promise.reject({ status: 409, detail: "
        + json.dumps(detail) + " }); };"
        'var first = null; NV_doInterrupt("w", "s", refetch, toast).then(function (r) { first = r; });'
    )

    assert json.loads(ctx.eval("JSON.stringify(toasts)")) == ["Interrupt failed: " + detail]
    assert json.loads(ctx.eval("JSON.stringify(first)")) == {"failed": True}


def test_the_rail_menu_no_longer_fires_an_unacknowledged_unhandled_request() -> None:
    """It called SH_api.interrupt(...).then(onChanged) with no toast and no error handling at all."""
    assert "SH_api.interrupt(" not in RAIL
    assert "window.NV_doInterrupt(" in RAIL


def test_the_transcript_marker_uses_the_shared_label() -> None:
    assert "window.SH_lifecycleLabel(" in DOC
    assert '"■ cancelled"' not in DOC, "the hard-coded label would show a Stop as a Cancel"


def test_a_pending_stop_button_does_not_keep_the_destructive_hover_and_keeps_its_hit_size() -> None:
    for cls in ("nv-stop-btn", "nv-interrupt-btn"):
        assert re.search(rf"\.{cls}:disabled\s*\{{[^}}]*cursor:\s*not-allowed", STYLES), cls
        assert re.search(rf"\.{cls}:disabled:hover\s*\{{", STYLES) or f".{cls}:not(:disabled):hover" in STYLES, (
            f"{cls}: a disabled button must not turn red on hover as if it would act"
        )


# ---- behaviour of the shared request function, run for real ----------------------------------


def _interrupt_ctx(*, rejects: bool = False):
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(
        "var calls = 0, toasts = [], refetched = [];"
        "var SH_api = { interrupt: function () { calls++; return "
        + ('Promise.reject({ detail: "boom" })' if rejects else "Promise.resolve({ interrupt_requested: true })")
        + "; } };"
        "function refetch(row) { refetched.push(row); }"
        "function toast(msg) { toasts.push(msg); }"
    )
    ctx.eval(DOC[DOC.index("var NV_STOP_IN_FLIGHT"):DOC.index("\n}\n", DOC.index("function NV_doInterrupt")) + 3])
    return ctx


def test_a_second_click_while_the_request_is_pending_shares_it() -> None:
    ctx = _interrupt_ctx()

    ctx.eval('NV_doInterrupt("w", "s", refetch, toast); NV_doInterrupt("w", "s", refetch, toast);')

    assert ctx.eval("calls") == 1, "a second click fired a second request"


def test_success_toasts_once_refetches_and_then_allows_another_stop() -> None:
    ctx = _interrupt_ctx()
    ctx.eval('NV_doInterrupt("w", "s", refetch, toast);')

    assert json.loads(ctx.eval("JSON.stringify(toasts)")) == ["Stopping the turn"]
    assert ctx.eval("refetched.length") == 1
    ctx.eval('NV_doInterrupt("w", "s", refetch, toast);')
    assert ctx.eval("calls") == 2, "the pending guard was never released"


def test_failure_toasts_the_error_reports_it_and_allows_a_retry() -> None:
    ctx = _interrupt_ctx(rejects=True)
    ctx.eval('var first = null; NV_doInterrupt("w", "s", refetch, toast).then(function (r) { first = r; });')

    assert json.loads(ctx.eval("JSON.stringify(toasts)")) == ["Interrupt failed: boom"]
    assert json.loads(ctx.eval("JSON.stringify(first)")) == {"failed": True}, (
        "the component clears its pending state on this result"
    )
    ctx.eval('NV_doInterrupt("w", "s", refetch, toast);')
    assert ctx.eval("calls") == 2


def test_different_sessions_do_not_share_a_pending_stop() -> None:
    ctx = _interrupt_ctx()

    ctx.eval('NV_doInterrupt("w", "s1", refetch, toast); NV_doInterrupt("w", "s2", refetch, toast);')

    assert ctx.eval("calls") == 2


# ---- the edited files still compile (the console has no build step: a syntax error is a blank page) -----


def test_the_edited_console_files_still_compile() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; var self = globalThis;")
    ctx.eval((ROOT / "ui" / "vendor" / "babel.min.js").read_text(encoding="utf-8"))
    ctx.eval("function compile(src) { return Babel.transform(src, { presets: ['react'] }).code.length; }")
    for src in (DOC, RAIL):
        ctx.eval("var SRC = " + json.dumps(src) + ";")
        assert ctx.eval("compile(SRC)") > 0
