"""The client-tool executor module (S3 spec section 5, crosscheck M9/M3).

Pure logic with an injected host, so it is EXECUTED in MiniRacer rather
than substring-matched: the replay fence and the vocabulary dispatch are
the parts that must actually be right.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "client-tools.js"
INDEX = ROOT / "ui" / "index.html"


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; window.primerApi = {};")
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    ctx.eval(
        """
        var CALLS = [];
        var HOST = {
          openDoc: function (kind, ref, line) {
            CALLS.push(["openDoc", kind, ref, line === undefined ? null : line]);
          },
          toast: function (msg) { CALLS.push(["toast", msg]); },
          attachLifecycle: {
            attach: function () { return null; },
            heartbeat: function () { return null; },
            detach: function () { return null; },
          },
        };
        var EX = CT_createExecutor(HOST);
        function frame(seq, name, args) {
          return {
            "class": "client_action",
            session_id: "s-1",
            seq: seq,
            payload: {call_id: "tc-" + seq, name: name, arguments: args},
          };
        }
        """
    )
    return ctx


def test_module_exists_and_is_registered_in_the_bundle() -> None:
    assert MODULE.exists()
    src = MODULE.read_text(encoding="utf-8")
    assert "function CT_createExecutor(" in src
    assert "window.CT_createExecutor" in src
    # Host-agnostic: no app surfaces, no fetch, no URLs.
    for banned in ("openTab", "primerApi", "apiFetch", "EventSource", "/v1/"):
        assert banned not in src, banned
    assert 'src="foundation/client-tools.js"' in INDEX.read_text(encoding="utf-8")


def test_unattached_sessions_ignore_everything() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'EX.handleEvent(frame(1, "client__open_file", {path: "a.txt"}))'
    ) == "ignored"
    assert ctx.eval("CALLS.length") == 0


def test_a_record_above_the_mark_executes_once() -> None:
    ctx = _ctx()
    ctx.eval('EX.setAttachment("s-1", 5)')
    assert ctx.eval(
        'EX.handleEvent(frame(6, "client__open_file", {path: "a.txt", line: 3}))'
    ) == "executed"
    assert ctx.eval("CALLS.length") == 1
    assert ctx.eval("CALLS[0][0]") == "openDoc"
    assert ctx.eval("CALLS[0][1]") == "file"
    assert ctx.eval("CALLS[0][2]") == "a.txt"
    assert ctx.eval("CALLS[0][3]") == 3
    # A tap reconnect redelivering the SAME frame must not re-execute it.
    assert ctx.eval(
        'EX.handleEvent(frame(6, "client__open_file", {path: "a.txt", line: 3}))'
    ) == "rendered"
    assert ctx.eval("CALLS.length") == 1


def test_two_attached_clients_both_execute_the_same_frame() -> None:
    # Spec section 7 (delivery): every attached client receives the record
    # and every one of them executes it. The dispatcher deliberately does
    # NOT elect a single winner; duplicate execution is harmless because
    # the host op is idempotent (studio.openTab dedupes on tab.id, pinned
    # in Task 13's adapter test).
    ctx = _ctx()
    ctx.eval(
        """
        var EX2 = CT_createExecutor(HOST);
        EX.setAttachment("s-1", 5);
        EX2.setAttachment("s-1", 5);
        var dup = frame(6, "client__open_file", {path: "a.txt"});
        var verdicts = [EX.handleEvent(dup), EX2.handleEvent(dup)];
        """
    )
    assert ctx.eval("verdicts[0]") == "executed"
    assert ctx.eval("verdicts[1]") == "executed"
    assert ctx.eval("CALLS.length") == 2
    assert ctx.eval("CALLS[0][2]") == "a.txt"
    assert ctx.eval("CALLS[1][2]") == "a.txt"


def test_records_at_or_below_the_mark_render_only() -> None:
    ctx = _ctx()
    ctx.eval('EX.setAttachment("s-1", 5)')
    for seq in (1, 5):
        assert ctx.eval(
            f'EX.handleEvent(frame({seq}, "client__open_file", {{path: "a.txt"}}))'
        ) == "rendered"
    assert ctx.eval("CALLS.length") == 0


def test_reattaching_refences_at_the_new_mark() -> None:
    ctx = _ctx()
    ctx.eval('EX.setAttachment("s-1", 5)')
    ctx.eval('EX.clearAttachment("s-1")')
    ctx.eval('EX.setAttachment("s-1", 20)')
    assert ctx.eval(
        'EX.handleEvent(frame(9, "client__open_file", {path: "a.txt"}))'
    ) == "rendered"
    assert ctx.eval(
        'EX.handleEvent(frame(21, "client__open_file", {path: "a.txt"}))'
    ) == "executed"


def test_inform_user_goes_to_the_injected_toast() -> None:
    ctx = _ctx()
    ctx.eval('EX.setAttachment("s-1", 0)')
    assert ctx.eval(
        'EX.handleEvent(frame(1, "misc__inform_user", {message: "hello"}))'
    ) == "executed"
    assert ctx.eval("CALLS[0][0]") == "toast"
    assert ctx.eval("CALLS[0][1]") == "hello"


def test_other_event_classes_and_unknown_tools_are_ignored() -> None:
    ctx = _ctx()
    ctx.eval('EX.setAttachment("s-1", 0)')
    assert ctx.eval('EX.handleEvent({"class": "tool_call", seq: 1})') == "ignored"
    assert ctx.eval(
        'EX.handleEvent(frame(1, "client__focus_session", {}))'
    ) == "ignored"
    assert ctx.eval("CALLS.length") == 0
    # An ignored unknown tool must not consume the seq: a later valid
    # frame at a higher seq still executes.
    assert ctx.eval(
        'EX.handleEvent(frame(2, "client__open_file", {path: "a.txt"}))'
    ) == "executed"


def test_events_for_another_session_are_ignored() -> None:
    ctx = _ctx()
    ctx.eval('EX.setAttachment("s-1", 0)')
    ctx.eval('var other = frame(9, "client__open_file", {path: "a.txt"});')
    ctx.eval('other.session_id = "s-2";')
    assert ctx.eval("EX.handleEvent(other)") == "ignored"
    assert ctx.eval("CALLS.length") == 0


# ---- GU-7: a heartbeat after a failed first attach arms the executor ---------------------------------------------------------------------------


def _executor(attach_answers):
    """An executor whose attach answers come from ``attach_answers`` (a JS array of 'fail' or an attached_seq) and whose heartbeat answers ``HB``."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; window.primerApi = {}; var CALLS = [], N = 0, HB = { attached_seq: 5 };")
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    ctx.eval(
        f"var ANSWERS = {attach_answers};"
        + """
        var EX = CT_createExecutor({
          openDoc: function (k, r) { CALLS.push(["openDoc", r]); },
          toast: function (m) { CALLS.push(["toast", m]); },
          attachLifecycle: {
            attach: function () { var a = ANSWERS[N++]; return a === 'fail' ? Promise.reject(new Error('503')) : Promise.resolve({ attached_seq: a }); },
            heartbeat: function () { return Promise.resolve(HB); },
            detach: function () { return Promise.resolve({}); },
          },
        });
        function inform(seq) {
          return EX.handleEvent({ "class": "client_action", session_id: "s1", seq: seq, payload: { name: "client__inform_user", arguments: { message: "m" + seq } } });
        }
        """
    )
    return ctx


def _run(ctx, js: str):
    ctx.eval("void 0;")
    out = ctx.eval(js)
    ctx.eval("void 0;")
    return out


def test_a_heartbeat_after_a_failed_attach_arms_the_executor() -> None:
    ctx = _executor("['fail']")
    try:
        ctx.eval("EX.start('s1').catch(function () {});")
        ctx.eval("void 0;")
        assert ctx.eval("inform(6)") == "ignored"                 # nothing attached yet
        ctx.eval("EX.heartbeat('s1');")
        ctx.eval("void 0;")
        assert ctx.eval("inform(6)") == "executed"               # the heartbeat (the same POST) created the attachment: actions run above its mark
        assert ctx.eval("inform(5)") == "rendered"               # and the replay fence is the server's attached_seq
        assert ctx.eval("JSON.stringify(CALLS)") == '[["toast","m6"]]'
    finally:
        ctx.close()


def test_a_heartbeat_with_the_same_attached_seq_keeps_the_fence_and_what_ran() -> None:
    ctx = _executor("[5]")
    try:
        ctx.eval("EX.start('s1');")
        ctx.eval("void 0;")
        assert ctx.eval("inform(6)") == "executed"
        ctx.eval("EX.heartbeat('s1');")
        ctx.eval("void 0;")
        assert ctx.eval("inform(6)") == "rendered"               # redelivery stays a no-op
    finally:
        ctx.close()


def test_a_heartbeat_that_reports_a_new_attached_seq_moves_the_fence() -> None:
    ctx = _executor("[5]")
    try:
        ctx.eval("EX.start('s1');")
        ctx.eval("void 0;")
        ctx.eval("HB = { attached_seq: 9 }; EX.heartbeat('s1');")
        ctx.eval("void 0;")
        assert ctx.eval("inform(8)") == "rendered"               # the server re-attached at 9: older frames are replay
        assert ctx.eval("inform(10)") == "executed"
    finally:
        ctx.close()


def test_a_heartbeat_that_lands_after_stop_does_not_re_arm() -> None:
    ctx = _executor("[5]")
    try:
        ctx.eval("EX.start('s1');")
        ctx.eval("void 0;")
        ctx.eval("var P = EX.heartbeat('s1'); EX.stop('s1');")
        ctx.eval("void 0;")
        assert ctx.eval("inform(6)") == "ignored"
    finally:
        ctx.close()
