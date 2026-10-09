"""The triggers LIST row's Fire now and Delete say when they are refused (board ticket 01a11db6-44fd, found in the #572 review).

``TR_TriggerRow`` (``ui/components/triggers.jsx``) ran Fire now and Delete inside ``catch (_err) { /* surfaced on the detail page */ }``, but nothing stores those errors where the detail
page reads them: a fire refused for a deleted trigger, a role that may not write, a session that has ended, or a delete refused for any reason left the row exactly as it was and said
nothing. Now both read the refusal through the one reader (``TR_refusalText``, the text the dialogs and the detail page show for the same refusal) and raise it as an error toast; a Fire now
that works says so; the row's buttons are enabled again after a refusal.

Round 2 (lead's review of #695): a Fire now that was SKIPPED (a disabled trigger answers 200 ``{skipped: true}``) is "Not fired", not "Trigger fired"; a fire whose deliveries failed is not a
plain success; a trigger that is already gone (``trigger_not_found``) refreshes the list and is worded for the row; the buttons are off while a request is pending; a throwing ``onChanged`` is
never reported as a refused write. The bodies of the fires are the REAL route's (``real_fire_bodies``).

The REAL row runs in V8 on the mini React (``tests/ui/_mini_react.py``) with the real ``ui/foundation/api.js`` ``ApiError`` and the REAL problem envelopes of the auth gate and the trigger
router (``tests/_support/trigger_envelopes.py``): a swallowed ``catch`` cannot be pinned by reading the source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._support.trigger_envelopes import real_envelopes, real_fire_bodies
from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")

TRIGGER = {"id": "tr-1", "name": "Nightly", "slug": "nightly", "enabled": True, "config": {"kind": "scheduled", "cron": "0 3 * * *", "timezone": "Asia/Dubai"}, "next_fire_at": None, "created_at": None}

_PRELUDE = """
window.addEventListener = function () {}; window.removeEventListener = function () {};
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], disabled: p.disabled, onClick: p.onClick }, p.children); }
function Banner(p) { return React.createElement("div", null, p.children); }
function Icon() { return null; }
function Modal(p) { return React.createElement("div", null, p.children); }
"""

_DRIVER = """
var __toasts = []; var __calls = []; var __changed = 0; var __confirm = true; var __outcome = null; var __response = {}; var __pending = false; var __throwOnChanged = false;
window.primerApi.toastPush = function (t) { __toasts.push(t); };
window.primerApi.apiFetch = function (method, path) {
  __calls.push(method + " " + path);
  if (__pending) return new Promise(function () {});
  return __outcome === null ? Promise.resolve(__response) : Promise.reject(new window.primerApi.ApiError(__outcome));
};
var confirmDialog = function () { return Promise.resolve(__confirm); };
function mountRow() { MR.mount(TR_TriggerRow, { trigger: __TRIGGER__, onOpen: function () {}, onChanged: function () { __changed += 1; if (__throwOnChanged) throw new Error("the list refetch blew up"); } }); }
function refusalText(envelope, fallback) { return TR_refusalText(new window.primerApi.ApiError(envelope), fallback); }
""".replace("__TRIGGER__", json.dumps(TRIGGER))


@pytest.fixture(scope="module")
def envelopes() -> dict[str, dict]:
    return real_envelopes()


@pytest.fixture(scope="module")
def fires() -> dict[str, dict]:
    return real_fire_bodies()


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(ROOT / "ui" / "components" / "triggers.jsx")


@pytest.fixture
def row(code):
    ctx = mini_react_context(API + "\n" + code, _PRELUDE)
    ctx.eval(_DRIVER)
    ctx.eval("mountRow();")
    try:
        yield ctx
    finally:
        ctx.close()


def _outcome(ctx, envelope: dict | None) -> None:
    ctx.eval(f"__outcome = {json.dumps(envelope)};")


def _answer(ctx, body: dict) -> None:
    ctx.eval(f"__response = {json.dumps(body)};")


def _press(ctx, what: str) -> None:
    ctx.eval(f"MR.click('trigger-row-{what}-tr-1'); MR.rerender();")
    ctx.eval("void 0;")   # the handler is async: let its promise jobs run
    ctx.eval("MR.rerender();")


def _toasts(ctx) -> list[dict]:
    return json.loads(ctx.eval("JSON.stringify(__toasts)"))


def _disabled(ctx, what: str) -> bool:
    return bool(ctx.eval(f"MR.find('trigger-row-{what}-tr-1').props.disabled"))


@pytest.mark.parametrize("which", ["session_ended", "role_refused", "router_forbidden"])
def test_a_refused_fire_now_raises_an_error_toast_with_the_text_the_detail_page_shows(row, envelopes, which: str) -> None:
    _outcome(row, envelopes[which])
    _press(row, "fire")
    toasts = _toasts(row)
    assert len(toasts) == 1 and toasts[0]["kind"] == "error", toasts
    assert toasts[0]["title"] == "Fire failed"
    assert toasts[0]["detail"] == json.loads(row.eval(f"JSON.stringify(refusalText({json.dumps(envelopes[which])}, 'Fire failed'))")), "the same reader and fallback as the detail page's Fire now"
    assert toasts[0]["detail"] not in ("auth_required", "forbidden_role"), "never a bare code"


@pytest.mark.parametrize("what", ["fire", "delete"])
def test_the_error_toast_carries_the_request_id_of_the_refusal(row, what: str) -> None:
    """The toast shows ``request-id`` with a copy link when it has one: what an operator pastes into a bug report."""
    envelope = {"type": "/errors/forbidden", "title": "Forbidden", "status": 403, "detail": "only the trigger's owner may do this", "extensions": {"code": "forbidden_role", "request_id": "req-abc123"}}
    _outcome(row, envelope)
    _press(row, what)
    assert _toasts(row)[0]["requestId"] == "req-abc123"


def test_the_session_that_ended_says_so_in_words(row, envelopes) -> None:
    _outcome(row, envelopes["session_ended"])
    _press(row, "fire")
    assert "sign in again" in _toasts(row)[0]["detail"]


@pytest.mark.parametrize("which", ["session_ended", "role_refused"])
def test_a_refused_fire_now_leaves_the_row_alone_and_gives_the_buttons_back(row, envelopes, which: str) -> None:
    _outcome(row, envelopes[which])
    _press(row, "fire")
    assert row.eval("__changed") == 0, "nothing changed, so the list is not refetched"
    assert not _disabled(row, "fire") and not _disabled(row, "delete")


def test_a_fire_now_that_works_says_so_and_refreshes_the_list(row) -> None:
    _press(row, "fire")
    assert json.loads(row.eval("JSON.stringify(__calls)")) == ["POST /triggers/tr-1/fire_now"]
    toasts = _toasts(row)
    assert len(toasts) == 1 and toasts[0]["kind"] == "success" and "Nightly" in toasts[0]["detail"], toasts
    assert row.eval("__changed") == 1
    assert not _disabled(row, "fire")


@pytest.mark.parametrize("which", ["session_ended", "role_refused"])
def test_a_refused_delete_raises_an_error_toast_with_the_text_the_detail_page_shows(row, envelopes, which: str) -> None:
    _outcome(row, envelopes[which])
    _press(row, "delete")
    toasts = _toasts(row)
    assert len(toasts) == 1 and toasts[0]["kind"] == "error", toasts
    assert toasts[0]["title"] == "Delete failed"
    assert toasts[0]["detail"] == json.loads(row.eval(f"JSON.stringify(refusalText({json.dumps(envelopes[which])}, 'The trigger could not be deleted.'))"))
    assert row.eval("__changed") == 0 and not _disabled(row, "delete")


def test_a_delete_that_works_refreshes_the_list_and_stays_quiet(row) -> None:
    """The row disappears from the refetched list: that is the feedback."""
    _press(row, "delete")
    assert json.loads(row.eval("JSON.stringify(__calls)")) == ["DELETE /triggers/tr-1"]
    assert _toasts(row) == [] and row.eval("__changed") == 1


def test_a_delete_that_is_not_confirmed_sends_nothing_and_says_nothing(row, envelopes) -> None:
    row.eval("__confirm = false;")
    _outcome(row, envelopes["not_found"])
    _press(row, "delete")
    assert json.loads(row.eval("JSON.stringify(__calls)")) == [] and _toasts(row) == [] and row.eval("__changed") == 0


def test_the_rows_actions_no_longer_swallow_an_error_in_the_source() -> None:
    """The control for the V8 cases: the two blocks do not keep an empty catch."""
    src = (ROOT / "ui" / "components" / "triggers.jsx").read_text(encoding="utf-8")
    row_src = src[src.index("function TR_TriggerRow("):src.index("function TR_TriggerRow(") + 2200]
    assert "catch (_err)" not in row_src


# ---- round 2 ------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_fire_now_that_was_skipped_says_so_and_does_not_claim_a_fire(row, fires) -> None:
    """B1: a disabled trigger answers 200 ``{skipped: true, fire_id: null, results: []}``. The row's Fire now stays enabled on a disabled trigger; "Trigger fired" would be false."""
    _answer(row, fires["skipped"])
    _press(row, "fire")
    toasts = _toasts(row)
    assert len(toasts) == 1, toasts
    assert toasts[0]["kind"] == "warning" and toasts[0]["title"] == "Not fired"
    assert toasts[0]["detail"] == "Nightly is disabled; enable it to fire it."
    assert "Trigger fired" not in json.dumps(toasts)
    assert row.eval("__changed") == 0, "nothing happened, so the list is not refetched"
    assert not _disabled(row, "fire")


@pytest.mark.parametrize("which", ["fired", "no_subscriptions", "delivery_skipped"])
def test_a_fire_that_went_through_is_a_success(row, fires, which: str) -> None:
    """A delivery that was skipped on purpose (no event match) is not a failure; a trigger with no subscription fired all the same."""
    _answer(row, fires[which])
    _press(row, "fire")
    toasts = _toasts(row)
    assert len(toasts) == 1 and toasts[0]["kind"] == "success" and toasts[0]["title"] == "Trigger fired", toasts
    assert row.eval("__changed") == 1


@pytest.mark.parametrize(("which", "counts"), [("partial", "1 of 2"), ("all_failed", "2 of 2")])
def test_a_fire_whose_deliveries_failed_is_a_warning_that_says_how_many(row, fires, which: str, counts: str) -> None:
    _answer(row, fires[which])
    _press(row, "fire")
    toasts = _toasts(row)
    assert len(toasts) == 1 and toasts[0]["kind"] == "warning", toasts
    assert toasts[0]["title"] == "Fired, with failures"
    assert counts + " deliveries failed" in toasts[0]["detail"] and "open the trigger" in toasts[0]["detail"]
    assert row.eval("__changed") == 1, "the trigger did fire: its last fire error changed, so the list is refreshed"


def test_a_fire_now_on_a_trigger_that_is_gone_refreshes_the_list_and_says_so_for_the_row(row, envelopes) -> None:
    """B2: ``trigger_not_found`` on the LIST row. The detail page's "Go back to the triggers list." is no use here, and the stale row must go."""
    _outcome(row, envelopes["not_found"])
    _press(row, "fire")
    toasts = _toasts(row)
    assert len(toasts) == 1 and toasts[0]["kind"] == "error" and toasts[0]["title"] == "Fire failed"
    assert toasts[0]["detail"] == "Nightly no longer exists; the list has been refreshed."
    assert "Go back" not in toasts[0]["detail"]
    assert row.eval("__changed") == 1, "the list is refetched so that the stale row goes"
    assert not _disabled(row, "fire")


def test_a_delete_of_a_trigger_that_is_already_gone_is_not_a_failure(row, envelopes) -> None:
    _outcome(row, envelopes["not_found"])
    _press(row, "delete")
    toasts = _toasts(row)
    assert len(toasts) == 1 and toasts[0]["kind"] == "info" and toasts[0]["title"] == "Already deleted"
    assert toasts[0]["detail"] == "Nightly was already deleted; the list has been refreshed."
    assert row.eval("__changed") == 1


def test_the_gone_toast_still_carries_the_request_id(row) -> None:
    envelope = {"type": "/errors/not-found", "title": "Not Found", "status": 404, "detail": "tr-1", "extensions": {"code": "trigger_not_found", "request_id": "req-gone-1"}}
    _outcome(row, envelope)
    _press(row, "fire")
    assert _toasts(row)[0]["requestId"] == "req-gone-1"


@pytest.mark.parametrize("what", ["fire", "delete"])
def test_the_buttons_are_off_while_a_request_is_pending(row, what: str) -> None:
    """N1: Fire now, Edit and Delete are all disabled from the moment the request is sent until it answers."""
    row.eval("__pending = true;")
    assert not _disabled(row, "fire") and not _disabled(row, "edit") and not _disabled(row, "delete")
    _press(row, what)
    assert json.loads(row.eval("JSON.stringify(__calls.length)")) == 1
    assert _disabled(row, "fire") and _disabled(row, "edit") and _disabled(row, "delete")
    assert _toasts(row) == [], "nothing to say yet"


@pytest.mark.parametrize("what", ["fire", "delete"])
def test_a_refetch_that_throws_is_never_reported_as_a_refused_write(row, what: str) -> None:
    """N2: ``onChanged`` runs outside the ``try`` that reports a refusal; a list refetch that blows up must not make a write that WENT THROUGH say "failed"."""
    row.eval("__throwOnChanged = true;")
    _press(row, what)
    toasts = _toasts(row)
    assert all(t["kind"] != "error" for t in toasts), toasts
    assert row.eval("__changed") == 1
