"""ui/foundation/use-mutation.js: a refused write is shown AND is not an unhandled promise rejection (finding ADM-18 of the 2026-10-08 admin review).

``mutate`` shows the toast (or calls ``onError``) when the request fails and then rethrows, because callers that ``await`` it need to know it failed (a dialog
must not close on a failed save). 34 of the 57 call sites do not await it (``create.mutate(body)`` in a click handler), so the rethrown error was an unhandled
rejection: every 422 or 409 the form handles perfectly well also reached ``unhandledrejection`` / Playwright's ``pageerror``.

The fix keeps the contract (an awaiting caller still sees the rejection) and marks the promise ``mutate`` returns as handled, so a caller that ignores it
raises nothing. V8 has no ``unhandledrejection`` event, so the test observes the cause: whether a rejection handler is attached to the returned promise before
control returns to the caller (``catch`` is ``then(undefined, fn)``, which is what is recorded). ``tests/ui_e2e/test_refused_write_leaves_no_uncaught_error_journey.py``
watches the real browser event.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "ui" / "foundation" / "use-mutation.js"

_OPEN: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN.append(ctx)
    ctx.eval("var window = globalThis;")
    ctx.eval(
        "window.React = { useState: function (init) { return [init, function () {}]; }, useRef: function (v) { return { current: v }; },"
        " useCallback: function (f) { return f; } };"
    )
    # Record every promise a rejection handler is attached to (catch(fn) is then(undefined, fn)).
    ctx.eval(
        "var __handled = new Set(); var __then = Promise.prototype.then;"
        " Promise.prototype.then = function (onF, onR) { if (typeof onR === 'function') __handled.add(this); return __then.call(this, onF, onR); };"
    )
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    ctx.eval("var __toasts = []; window.primerApi.toastPush = function (t) { __toasts.push(t); };")
    return ctx


def _run(script: str):
    ctx = _ctx()
    ctx.eval(script)
    # let the microtask queue settle before reading anything back
    ctx.eval("1")
    return ctx


FAILING = "var m = window.primerApi.useMutation(function () { return Promise.reject(new Error('refused')); }, %s);"


def test_the_promise_a_caller_ignores_has_a_rejection_handler_attached_already() -> None:
    """``onClick={() => create.mutate(body)}``: the caller never touches the returned promise."""
    ctx = _ctx()
    ctx.eval(FAILING % "{ onError: function () {} }")

    assert ctx.eval("var p = m.mutate({}); __handled.has(p)") is True, "the returned promise has no rejection handler: an ignored refusal is an unhandled rejection"


def test_the_default_toast_path_marks_it_handled_too() -> None:
    ctx = _ctx()
    ctx.eval(FAILING % "{}")

    assert ctx.eval("var p = m.mutate({}); __handled.has(p)") is True


def test_an_awaiting_caller_still_sees_the_rejection() -> None:
    """The contract the 23 awaiting call sites rely on: a failed save must reach their catch, so the dialog stays open."""
    ctx = _ctx()
    ctx.eval(FAILING % "{ onError: function () {} }")
    ctx.eval("var outcome = 'pending'; m.mutate({}).then(function () { outcome = 'resolved'; }, function (e) { outcome = 'rejected:' + e.message; });")

    assert ctx.eval("outcome") == "rejected:refused"


def test_an_awaiting_caller_inside_try_catch_is_unchanged() -> None:
    ctx = _ctx()
    ctx.eval(FAILING % "{ onError: function () {} }")
    ctx.eval(
        "var caught = null; (async function () { try { await m.mutate({}); caught = 'no error'; } catch (e) { caught = e.message; } })();"
    )

    assert ctx.eval("caught") == "refused"


def test_the_refusal_is_still_shown_through_on_error_or_the_toast() -> None:
    ctx = _ctx()
    ctx.eval("var seen = []; " + FAILING % "{ onError: function (e) { seen.push(e.message); } }")
    ctx.eval("m.mutate({});")
    assert json.loads(ctx.eval("JSON.stringify(seen)")) == ["refused"]
    assert json.loads(ctx.eval("JSON.stringify(__toasts)")) == [], "an onError handler replaces the toast"

    ctx2 = _ctx()
    ctx2.eval(FAILING % "{}")
    ctx2.eval("m.mutate({});")
    toasts = json.loads(ctx2.eval("JSON.stringify(__toasts)"))
    assert len(toasts) == 1 and toasts[0]["kind"] == "error" and toasts[0]["title"] == "Request failed"


def test_a_successful_mutation_resolves_with_its_data_and_calls_on_success() -> None:
    ctx = _ctx()
    ctx.eval(
        "var got = []; var m = window.primerApi.useMutation(function (b) { return Promise.resolve({ ok: b.n }); }, { onSuccess: function (d) { got.push(d.ok); } });"
    )
    ctx.eval("var result = null; m.mutate({ n: 7 }).then(function (d) { result = d.ok; });")

    assert ctx.eval("result") == 7 and json.loads(ctx.eval("JSON.stringify(got)")) == [7]
