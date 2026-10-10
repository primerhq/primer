"""What an answered fire_now says on the detail page and the phone fact sheet (ticket 01a12144).

Two surfaces said 'Fired' when the server SKIPPED the fire: a disabled trigger's fire_now answers
200 {skipped: true, fire_id: null, results: []}, and the triggers DETAIL page printed the bare
'Fired' while the phone fact sheet toasts 'Fired <id>'. The list row already gets this right: it
reports the fire through TR_fireOutcome (a skipped fire is the warning 'Not fired' with the why,
a fire whose deliveries failed is 'Fired, with failures', otherwise 'Trigger fired'). The two
surfaces now use the same helper.

The V8 cases run the real triggers.jsx TR_fireOutcome in MiniRacer with the real fire_now bodies
from tests/_support/trigger_envelopes.py (the way #695's tests do); the detail page is pinned
statically, the way this directory pins the rest of the console.

Round 2: a REFUSED fire on the phone fact sheet says the request id the server sent (it comes off
the ApiError itself; readRefusal returns no id, so the copy line never showed), and for a trigger
the server already deleted (trigger_not_found) it says the desktop list row's words, then closes
the sheet and refetches the phone list. Those pins are behavioural: the real fireNow() from
nv-mobile-shell.jsx runs in MiniRacer on a rejected real ApiError built from a real envelope with
the production request-id middleware (the reviewer's probe /var/tmp/review714/probe_phone_rid.py).

Follow-up (the lead's review of items c/d/e): a write that refetches its own row away (a confirmed
delete, an Already-deleted delete, a fire the server answers trigger_not_found) lands focus on the
adjacent row's Delete (next, or previous when it is the last row) or, with no row left, the
overlay's h1 - not the list's action-bar span; the toast's request-id copy is a <button> named
'Copy request id'; and the TR_fireOutcome rule that a delivery skipped on purpose is not a failure
is pinned with the real bodies, including the mutant the #695 review left uncaught.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "triggers.jsx").read_text(encoding="utf-8")
MOB = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")
SHELL = (ROOT / "ui" / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


@pytest.fixture(scope="module")
def bodies() -> dict[str, dict]:
    from tests._support.trigger_envelopes import real_fire_bodies

    return real_fire_bodies()


@pytest.fixture(scope="module")
def refusals() -> dict[str, dict]:
    """The REAL refusal envelopes with the production request-id middleware (the reviewer's probe).

    The real register_error_handlers, the real require_user gate and the trigger router's own
    _raise_code answer through TestClient, so extensions.request_id is the one a real server sends
    (tests/_support/trigger_envelopes.py's app carries no request-id middleware).
    """
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from primer.api._app_middleware import _install_request_id
    from primer.api.deps import require_user
    from primer.api.errors import register_error_handlers
    from primer.api.routers.triggers import _raise_code

    app = FastAPI()
    register_error_handlers(app)
    _install_request_id(app)

    @app.post("/v1/not_found")
    def not_found():
        _raise_code(404, "trigger_not_found", "tr-1")

    @app.post("/v1/gated", dependencies=[Depends(require_user)])
    def gated():
        return {}

    client = TestClient(app, raise_server_exceptions=False)
    return {
        "not_found": client.post("/v1/not_found").json(),
        "session_ended": client.post("/v1/gated").json(),
    }


def _outcome(res: dict, label: str) -> dict:
    """The real TR_fireOutcome from triggers.jsx, run in V8 (MiniRacer) on the given body."""
    from py_mini_racer import MiniRacer

    start = SRC.index("function TR_fireOutcome(")
    end = SRC.index("\n}\n", start) + 3
    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval("var window = {};")
    ctx.eval(SRC[start:end])
    return json.loads(ctx.eval(f"JSON.stringify(TR_fireOutcome({json.dumps(res)}, {json.dumps(label)}))"))


def _fire_now() -> "callable":
    """The real fireNow() from nv-mobile-shell.jsx, in V8 (MiniRacer): the real api.js ApiError and
    readRefusal, the real TR helpers, a con.toast that records what the toast host would render
    (the copy line shows when the payload carries a requestId), props carrying onClose / onChanged
    call counters and an SH_api.fireTrigger that rejects with a REAL ApiError or resolves with a
    real fire body (the reviewer's probe /var/tmp/review714/probe_phone_rid.py).

    run() returns the recorded toasts (kind, text, requestId as the toast host would render them)
    and the onClose / onChanged call counts.
    """
    from py_mini_racer import MiniRacer

    def cut(src: str, start_marker: str, end_marker: str) -> str:
        s = src.index(start_marker)
        e = src.index(end_marker, s) + len(end_marker)
        return src[s:e]

    api = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")
    remedies = cut(SRC, "var TR_REMEDIES = {", "\n};\n")
    refusal = cut(SRC, "function TR_refusalText(", "\n}\n")
    outcome = cut(SRC, "function TR_fireOutcome(", "\n}\n")
    fire = cut(MOB, "  function fireNow() {", "\n  }\n")

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval("var window = {};")
    ctx.eval(api)
    ctx.eval(remedies + "\n" + refusal + "\n" + outcome)
    ctx.eval("window.TR_fireOutcome = TR_fireOutcome; window.TR_refusalText = TR_refusalText;")
    ctx.eval(
        "var __pushed = []; var __closed = 0; var __changed = 0;"
        "var __mode = null; var __env = null; var __body = null;"
        'var props = { row: { id: "tr-1", name: "Nightly", slug: "nightly" },'
        "onClose: function () { __closed++; }, onChanged: function () { __changed++; } };"
        "var firing = false; function setFiring() {}"
        "var con = { toast: function (msg, extra) {"
        "__pushed.push({ kind: (extra && extra.kind) || 'info', text: String(msg),"
        "requestId: (extra && (extra.requestId || extra.request_id)) || null });"
        "}};"
        "var SH_api = { fireTrigger: function () {"
        "return __mode === 'reject' ? Promise.reject(new window.primerApi.ApiError(__env))"
        ": Promise.resolve(__body); } };"
    )
    ctx.eval(fire)

    def run(mode: str, env: dict | None = None, body: dict | None = None) -> dict:
        ctx.eval(
            f"__pushed = []; __closed = 0; __changed = 0; __mode = {json.dumps(mode)};"
            f"__env = {json.dumps(env)}; __body = {json.dumps(body)};"
        )
        ctx.eval("fireNow();")
        ctx.eval("void 0;")
        return json.loads(ctx.eval("JSON.stringify({ pushed: __pushed, closed: __closed, changed: __changed })"))

    return run


# ---- TR_fireOutcome says what the server answered (V8, the real bodies) -----------------------------------------


def test_a_disabled_trigger_says_not_fired(bodies) -> None:
    """The 200 {skipped: true, fire_id: null} body: nothing was fired, and the why is said, not 'Fired'."""
    out = _outcome(bodies["skipped"], "nightly")
    assert out["kind"] == "warning"
    assert out["title"] == "Not fired"
    assert out["detail"] == "nightly is disabled; enable it to fire it."


def test_a_fired_trigger_says_fired(bodies) -> None:
    assert _outcome(bodies["fired"], "nightly") == {
        "kind": "success",
        "title": "Trigger fired",
        "detail": "nightly",
    }


def test_a_fire_without_subscriptions_still_fired(bodies) -> None:
    assert _outcome(bodies["no_subscriptions"], "nightly")["title"] == "Trigger fired"


def test_partial_and_total_failures_are_failures_not_skips(bodies) -> None:
    partial = _outcome(bodies["partial"], "nightly")
    assert partial["title"] == "Fired, with failures"
    assert "1 of 2" in partial["detail"]
    assert _outcome(bodies["all_failed"], "nightly")["title"] == "Fired, with failures"


def test_the_real_delivery_skipped_on_purpose_is_not_a_failure(bodies) -> None:
    """The real event_matcher skip row (ok: true, skipped: true) is a delivery that was skipped on purpose."""
    assert _outcome(bodies["delivery_skipped"], "nightly")["title"] == "Trigger fired"


def test_a_skipped_delivery_with_ok_false_is_not_a_failure() -> None:
    """The mutant the #695 review left uncaught: without the !r.skipped filter this body reports a failure.

    results = [{ok: true}, {skipped: true, ok: false}] is 'Trigger fired', not 'Fired, with failures' -
    a skipped delivery is not a failed one.
    """
    res = {
        "skipped": False,
        "fire_id": "fire-9",
        "results": [{"ok": True}, {"skipped": True, "ok": False}],
    }
    assert _outcome(res, "nightly") == {"kind": "success", "title": "Trigger fired", "detail": "nightly"}


# ---- the detail page reuses the outcome for a skipped fire -------------------------------------------------------


def test_the_detail_page_branches_on_the_skipped_outcome() -> None:
    """A skipped fire renders the TR_fireOutcome words ('Not fired', and why), not the bare 'Fired'."""
    block = SRC[SRC.index('data-testid="fire-now-result"'):SRC.index('data-testid="fire-now-results-list"')]
    assert "fireResult.skipped" in block
    assert "TR_fireOutcome" in block


# ---- the phone fact sheet's real fireNow() on a refused fire (V8, the real ApiError) ------------------------------


def test_the_mobile_fire_now_says_the_request_id_the_server_sent(refusals) -> None:
    """The copy line needs the id, and readRefusal returns no id: the toast's requestId comes off
    the ApiError itself and equals the envelope's extensions.request_id, for every refusal."""
    run = _fire_now()
    for name in ("not_found", "session_ended"):
        env = refusals[name]
        out = run("reject", env=env)
        assert len(out["pushed"]) == 1, name
        assert out["pushed"][0]["requestId"] == env["extensions"]["request_id"], name


def test_the_mobile_fire_now_for_a_gone_trigger_says_the_list_words_and_refreshes(refusals) -> None:
    """A trigger the server already deleted is the desktop list row's words (the label, then ' no
    longer exists; the list has been refreshed.'), not the detail page's 'Go back to the triggers
    list': the sheet goes away with the stale row and the phone list refetches."""
    run = _fire_now()
    out = run("reject", env=refusals["not_found"])
    assert out["pushed"][0]["kind"] == "error"
    assert out["pushed"][0]["text"] == "Nightly no longer exists; the list has been refreshed."
    assert out["closed"] == 1
    assert out["changed"] == 1


def test_the_mobile_fire_now_for_other_refusals_keeps_the_reader_words_and_the_sheet(refusals) -> None:
    """Only trigger_not_found gets the list words and the close-and-refresh: another refusal is the
    one reader's text under 'Fire failed:' (with its request id), and the sheet stays open on the
    live row."""
    run = _fire_now()
    out = run("reject", env=refusals["session_ended"])
    assert out["pushed"][0]["kind"] == "error"
    assert out["pushed"][0]["text"] == "Fire failed: Your session has ended; sign in again."
    assert out["closed"] == 0
    assert out["changed"] == 0


def test_the_mobile_fire_now_says_the_outcome_for_an_answered_fire(bodies) -> None:
    """The answered path is unchanged: the outcome's words through the real fireNow(), no request
    id to copy on a 200."""
    run = _fire_now()
    out = run("resolve", body=bodies["skipped"])
    assert out["pushed"][0]["kind"] == "warning"
    assert out["pushed"][0]["text"] == "Not fired: Nightly is disabled; enable it to fire it."
    assert out["pushed"][0]["requestId"] is None
    assert out["closed"] == 0
    assert out["changed"] == 0


# ---- the phone fact sheet reads the outcome and the one refusal reader ---------------------------------------------


def test_the_mobile_fact_sheet_uses_the_outcome_and_the_one_reader() -> None:
    """A skipped fire is not 'Fired <id>'; a refused one is the one reader's text. The request id
    and the trigger_not_found words are pinned behaviourally above (the static grep for requestId
    passed for the wrong reason: readRefusal returns no id, so the id came off the wrong object)."""
    start = MOB.index("function fireNow()")
    end = MOB.index("\n  }\n", start) + 4
    fn = MOB[start:end]
    assert '"Fired " + (props.row.id' not in fn
    assert '"Fire failed: " + ((e && e.message)' not in fn
    assert "TR_fireOutcome" in fn
    assert "TR_refusalText" in fn


def test_the_mobile_shell_can_reach_the_trigger_helpers() -> None:
    """TR_fireOutcome / TR_refusalText are module-local to triggers.jsx; the console page needs the window exports."""
    assert "window.TR_fireOutcome = TR_fireOutcome" in SRC
    assert "window.TR_refusalText = TR_refusalText" in SRC


# ---- focus after a confirmed delete --------------------------------------------------------------------------------


def test_a_write_that_refetches_the_row_away_moves_focus_to_the_adjacent_row_or_the_h1() -> None:
    """The confirm Modal restores focus to the row's Delete button, and a gone trigger's Fire now keeps focus on
    its button; each of the three refetches that drops the row (a confirmed delete, an Already-deleted delete, a
    trigger_not_found fire) would then leave focus on <body>. All three move it through the one helper: the
    adjacent row's Delete (next, or previous when it is the last row) or, with no row left, the overlay's h1
    (the surface's one heading, tabIndex -1: the re-hosted pages render action bars, never headings, so the
    list's own filter-bar span is not the fallback) (ticket 01a12144)."""
    assert SRC.count("TR_focusAfterRowGone(") == 4
    start = SRC.index("function TR_focusAfterRowGone(")
    helper = SRC[start:SRC.index("\n}\n", start)]
    assert "nextElementSibling" in helper
    assert "previousElementSibling" in helper
    assert "nv-overlay-title" in helper
    assert ".focus()" in helper
    assert "triggers-list-heading" not in SRC
    list_start = SRC.index("function TR_TriggerList")
    bar = SRC[SRC.index("filter-bar", list_start):SRC.index("filter-bar", list_start) + 400]
    assert "tabIndex={-1}" not in bar
    overlay = (ROOT / "ui" / "components" / "console" / "nv-overlays.jsx").read_text(encoding="utf-8")
    h1 = overlay[overlay.index('<h1 className="nv-overlay-title'):overlay.index('<h1 className="nv-overlay-title') + 220]
    assert "tabIndex={-1}" in h1


# ---- the toast request-id copy is keyboard-usable -----------------------------------------------------------------


def test_the_toast_request_id_copy_is_a_named_button() -> None:
    """An <a> with no href, role or key handler cannot be used from the keyboard; it is a button, and its name
    is 'Copy request id', not the bare 'copy' an operator would have to guess at."""
    m = re.search(r"<button\b[^>]{0,200}?data-testid=\"toast-copy-request-id\"", SHELL, re.S)
    assert m, "the copy control must be a <button>, not an <a> with only an onClick"
    assert 'type="button"' in m.group(0)
    assert 'aria-label="Copy request id"' in m.group(0)
    assert ".toast .req-id button" in CSS
