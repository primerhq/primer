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
