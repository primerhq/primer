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

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHELL_STATUS = ROOT / "ui" / "foundation" / "shell-status.js"
SHELL_VERBS = ROOT / "ui" / "foundation" / "shell-verbs.js"
CONSOLE = ROOT / "ui" / "components" / "console"
DOC = (CONSOLE / "nv-session-doc.jsx").read_text(encoding="utf-8")
RAIL = (CONSOLE / "nv-rail.jsx").read_text(encoding="utf-8")
PALETTE = (CONSOLE / "nv-palette.jsx").read_text(encoding="utf-8")
STYLES = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

# Every V8 isolate this file creates, closed after each test: an isolate nobody disposes lives until the
# process ends, and tests/ui as a whole peaks at over a gigabyte for exactly that reason.
_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _new_context():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    return ctx


def _ctx():
    ctx = _new_context()
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


# ---- SH_canStop: only a running, non-parked turn can be stopped --------------------------------


def test_only_a_running_session_that_is_not_parked_can_be_stopped() -> None:
    """The server's own condition: it records a Stop only on a RUNNING row with no park. Every other row
    answers 200 as a no-op (idle, waiting, paused, created) or 409 (parked, ended)."""
    ctx = _ctx()
    assert _js(ctx, 'SH_canStop({status: "running"})') is True
    for row in (
        "null", "{}", '{status: "waiting"}', '{status: "paused"}', '{status: "created"}', '{status: "ended"}',
        '{status: "running", parked_status: "parked"}', '{status: "running", parked_status: "resumable"}',
    ):
        assert _js(ctx, f"SH_canStop({row})") is False, row


# ---- SH_stoppingLine: what the strip says, honestly ------------------------------------------


def test_a_stop_during_the_model_wait_just_says_stopping() -> None:
    ctx = _ctx()
    for verb in ("thinking", "sending", "responding"):
        assert _js(ctx, f'SH_stoppingLine({{verb: "{verb}"}})') == "stopping"


def test_a_stop_during_a_tool_says_the_tool_is_being_ended_by_name() -> None:
    """A Stop cancels a running tool (a file write is waited for instead, a few seconds at most); the strip cannot tell
    which, so it says the thing that is true of both and does not imply the tool must run to its end."""
    ctx = _ctx()
    assert _js(ctx, 'SH_stoppingLine({verb: "grep_src", object: "src/"})') == "stopping: ending grep_src"


def test_a_stop_while_a_tool_runs_but_its_name_is_unknown_says_the_running_tool() -> None:
    ctx = _ctx()
    assert _js(ctx, 'SH_stoppingLine({verb: "executing"})') == "stopping: ending the running tool"


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


def test_the_composer_stop_and_the_strip_button_only_appear_for_a_stoppable_turn() -> None:
    """Stop on a turn that is not running (a parked session, a resting one) can only fail or do nothing."""
    composer = re.search(r"\{props\.running[^\n]*\? \(\s*<button type=\"button\" className=\"nv-stop-btn\"", DOC)
    assert composer, "the composer Stop button's render condition is gone"
    assert "props.canStop" in composer.group(0), "the composer Stop must be gated on canStop"
    strip = _function_source(DOC, "NV_StatusStrip")
    assert "props.canStop" in strip, "the status strip's interrupt button must be gated on canStop"
    assert "canStop={window.SH_canStop(session)}" in DOC, "the session doc must pass canStop to the composer"
    assert re.search(
        r"<NV_StatusStrip shown=\{props\.statusShown\}\s+stopping=\{props\.stopping\}\s+canStop=\{props\.canStop\}", DOC,
    ), "the composer must pass canStop on to the status strip"


def test_a_stoppable_session_is_what_the_palette_verb_needs_and_the_palette_says_so() -> None:
    verb = DOC[DOC.index('id: "session.interrupt"'):]
    verb = verb[:verb.index("run: function")]
    assert "available: function (ctx)" in verb and "SH_canStop(ctx.session)" in verb
    ranking = PALETTE[PALETTE.index("SH_rankVerbs("):PALETTE.index("SH_rankVerbs(") + 500]
    assert re.search(r"session:\s*window\.NV_focusedSessionRow\s*\?\s*window\.NV_focusedSessionRow\(\)\s*:\s*null", ranking), (
        "the palette must tell the ranker which session is focused"
    )
    assert "window.NV_focusedSessionRow = " in DOC


def test_the_verb_registry_keeps_and_applies_an_availability_predicate() -> None:
    """A verb is stored through an explicit whitelist: a field that is not listed is silently dropped and any
    gate reading it never fires (the comment next to requiresLive). available must be on that list, and the
    ranker must apply it."""
    ctx = _new_context()
    ctx.eval("var window = globalThis;")
    ctx.eval(SHELL_VERBS.read_text(encoding="utf-8"))
    ctx.eval(
        "var reg = SH_createVerbRegistry();"
        "reg.register({ id: 'a.stop', label: 'Stop It', contexts: ['session'], surfaces: ['palette', 'tab-menu'],"
        "  available: function (c) { return !!(c.session && c.session.ok); }, run: function () {} });"
        "reg.register({ id: 'a.always', label: 'Open Docs', contexts: ['session'], surfaces: ['palette', 'tab-menu'],"
        "  run: function () {} });"
        "function ids(c) { return SH_rankVerbs(reg, '', c).map(function (v) { return v.id; }); }"
    )
    assert json.loads(ctx.eval("JSON.stringify(ids({docKind: 'session', session: {ok: true}}))")) == ["a.stop", "a.always"]
    assert json.loads(ctx.eval("JSON.stringify(ids({docKind: 'session', session: {ok: false}}))")) == ["a.always"]
    assert json.loads(ctx.eval("JSON.stringify(ids({docKind: 'session'}))")) == ["a.always"]


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
    assert re.search(r"var stopping = [^;]*stopPending && window\.SH_canStop\(session\)", DOC), (
        "the client's pending leg must obey the same parked/ended exclusions as the served flag, or a "
        "parked row reads 'stopping' for the length of the 5s fallback"
    )
    assert "stopping={stopping}" in DOC, "the composer (desktop and mobile share it) must be told"


def test_every_entry_point_shares_one_guarded_function_that_toasts() -> None:
    fn = _function_source(DOC, "NV_doInterrupt")
    assert "NV_STOP_IN_FLIGHT" in fn, "a second click while the request is pending must not fire another"
    assert re.search(r'toast\("Stopping', fn), "success must be acknowledged with a toast"
    assert 'toast("Interrupt failed: "' in fn, "the existing failure toast is kept"
    assert "window.NV_doInterrupt = NV_doInterrupt;" in DOC


def test_the_rail_menu_offers_interrupt_only_on_a_session_that_can_be_stopped() -> None:
    """A parked session has no turn to stop (the server answers 409) and an idle one answers 200 as a no-op:
    offering the row on either would only produce an error or a lie. Park and End stay (they act on any
    live session)."""
    menu = _function_source(RAIL, "NV_Rail_SessionContextMenu")
    interrupt_row = re.search(r'if \(window\.SH_canStop\(s\)\) \{\s*rows\.push\(act\("Interrupt"', menu)
    assert interrupt_row, "the Interrupt row must be gated on SH_canStop(s)"
    assert 'rows.push(act("End"' in menu and 'rows.push(act("Park"' in menu


def test_the_rail_failure_toast_is_an_error_not_an_info() -> None:
    menu = _function_source(RAIL, "NV_Rail_SessionContextMenu")
    toast = menu[menu.index("function toast(msg, extra)"):menu.index("var over = ")]
    assert "extra" in toast and "kind" in toast and "requestId" in toast, "the rail must forward the toast's kind and request id"
    assert "window.NV_doInterrupt(wid, sid, props.onChanged, toast)" in menu, "and the Stop row hands it the same toast"
    assert 'kind: "info"' not in toast, "a failed Stop must not be an info toast"


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


def _interrupt_ctx(*, rejects: bool = False, row: str = "{ interrupt_requested: true }"):
    """``row`` is what POST .../interrupt answers with on a 200: the session row. ``toastExtras`` records the
    second argument (kind, requestId) of every toast."""
    ctx = _new_context()
    ctx.eval(
        "var calls = 0, toasts = [], toastExtras = [], refetched = [];"
        "var SH_api = { interrupt: function () { calls++; return "
        + ('Promise.reject({ detail: "boom", requestId: "req-1" })' if rejects else f"Promise.resolve({row})")
        + "; } };"
        "function refetch(row) { refetched.push(row); }"
        "function toast(msg, extra) { toasts.push(msg); toastExtras.push(extra || null); }"
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


def test_a_200_that_recorded_nothing_says_so_instead_of_claiming_a_stop() -> None:
    """POST .../interrupt answers 200 as a NO-OP on every row that is neither running nor parked (idle, waiting,
    paused, created, or a turn that just finished): the row comes back with interrupt_requested false. Toasting
    "Stopping the turn" there is a lie, and the click's pending state would sit for its 5s fallback."""
    ctx = _interrupt_ctx(row="{ status: 'waiting', interrupt_requested: false }")
    ctx.eval('var first = null; NV_doInterrupt("w", "s", refetch, toast).then(function (r) { first = r; });')

    assert json.loads(ctx.eval("JSON.stringify(toasts)")) == ["Nothing to stop: no turn is running"]
    assert json.loads(ctx.eval("JSON.stringify(first)")) == {"noop": True}, (
        "the component clears its pending state at once on this result"
    )
    assert ctx.eval("refetched.length") == 1, "the row is refreshed so the surface shows the real state"


def test_a_200_without_a_row_is_also_nothing_to_stop() -> None:
    ctx = _interrupt_ctx(row="null")
    ctx.eval('var first = null; NV_doInterrupt("w", "s", refetch, toast).then(function (r) { first = r; });')

    assert json.loads(ctx.eval("JSON.stringify(toasts)")) == ["Nothing to stop: no turn is running"]
    assert json.loads(ctx.eval("JSON.stringify(first)")) == {"noop": True}


def test_a_no_op_releases_the_pending_guard() -> None:
    ctx = _interrupt_ctx(row="{ interrupt_requested: false }")
    ctx.eval('NV_doInterrupt("w", "s", refetch, toast);')
    ctx.eval('NV_doInterrupt("w", "s", refetch, toast);')

    assert ctx.eval("calls") == 2


def test_the_composer_clears_its_pending_state_on_a_no_op_as_well_as_on_a_failure() -> None:
    handler = DOC[DOC.index("onInterrupt={function () {"):]
    handler = handler[:handler.index("}}")]
    assert "setStopPending(true)" in handler
    assert re.search(r"res\.failed\s*\|\|\s*res\.noop|res\.noop\s*\|\|\s*res\.failed", handler), handler


def test_failure_toasts_the_error_reports_it_and_allows_a_retry() -> None:
    ctx = _interrupt_ctx(rejects=True)
    ctx.eval('var first = null; NV_doInterrupt("w", "s", refetch, toast).then(function (r) { first = r; });')

    assert json.loads(ctx.eval("JSON.stringify(toasts)")) == ["Interrupt failed: boom"]
    assert json.loads(ctx.eval("JSON.stringify(toastExtras)")) == [{"kind": "error", "requestId": "req-1"}], (
        "a failed Stop is an error toast, and carries the request id for the bug report"
    )
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
    ctx = _new_context()
    ctx.eval("var window = globalThis; var self = globalThis;")
    ctx.eval((ROOT / "ui" / "vendor" / "babel.min.js").read_text(encoding="utf-8"))
    ctx.eval("function compile(src) { return Babel.transform(src, { presets: ['react'] }).code.length; }")
    for src in (DOC, RAIL, PALETTE):
        ctx.eval("var SRC = " + json.dumps(src) + ";")
        assert ctx.eval("compile(SRC)") > 0
