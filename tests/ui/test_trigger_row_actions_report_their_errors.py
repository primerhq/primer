"""The triggers LIST row's Fire now and Delete say when they are refused (board ticket 01a11db6-44fd, found in the #572 review).

``TR_TriggerRow`` (``ui/components/triggers.jsx``) ran Fire now and Delete inside ``catch (_err) { /* surfaced on the detail page */ }``, but nothing stores those errors where the detail
page reads them: a fire refused for a deleted trigger, a role that may not write, a session that has ended, or a delete refused for any reason left the row exactly as it was and said
nothing. Now both read the refusal through the one reader (``TR_refusalText``, the text the dialogs and the detail page show for the same refusal) and raise it as an error toast; a Fire now
that works says so; the row's buttons are enabled again after a refusal.

The REAL row runs in V8 on the mini React (``tests/ui/_mini_react.py``) with the real ``ui/foundation/api.js`` ``ApiError`` and the REAL problem envelopes of the auth gate and the trigger
router (``tests/_support/trigger_envelopes.py``): a swallowed ``catch`` cannot be pinned by reading the source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._support.trigger_envelopes import real_envelopes
from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")

TRIGGER = {"id": "tr-1", "name": "Nightly", "slug": "nightly", "enabled": True, "config": {"kind": "cron", "expr": "0 3 * * *"}, "next_fire_at": None, "created_at": None}

_PRELUDE = """
window.addEventListener = function () {}; window.removeEventListener = function () {};
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], disabled: p.disabled, onClick: p.onClick }, p.children); }
function Banner(p) { return React.createElement("div", null, p.children); }
function Icon() { return null; }
function Modal(p) { return React.createElement("div", null, p.children); }
"""

_DRIVER = """
var __toasts = []; var __calls = []; var __changed = 0; var __confirm = true; var __outcome = null;
window.primerApi.toastPush = function (t) { __toasts.push(t); };
window.primerApi.apiFetch = function (method, path) {
  __calls.push(method + " " + path);
  return __outcome === null ? Promise.resolve({}) : Promise.reject(new window.primerApi.ApiError(__outcome));
};
var confirmDialog = function () { return Promise.resolve(__confirm); };
function mountRow() { MR.mount(TR_TriggerRow, { trigger: __TRIGGER__, onOpen: function () {}, onChanged: function () { __changed += 1; } }); }
function refusalText(envelope, fallback) { return TR_refusalText(new window.primerApi.ApiError(envelope), fallback); }
""".replace("__TRIGGER__", json.dumps(TRIGGER))


@pytest.fixture(scope="module")
def envelopes() -> dict[str, dict]:
    return real_envelopes()


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


def _press(ctx, what: str) -> None:
    ctx.eval(f"MR.click('trigger-row-{what}-tr-1'); MR.rerender();")
    ctx.eval("void 0;")   # the handler is async: let its promise jobs run
    ctx.eval("MR.rerender();")


def _toasts(ctx) -> list[dict]:
    return json.loads(ctx.eval("JSON.stringify(__toasts)"))


def _disabled(ctx, what: str) -> bool:
    return bool(ctx.eval(f"MR.find('trigger-row-{what}-tr-1').props.disabled"))


@pytest.mark.parametrize("which", ["session_ended", "role_refused", "not_found", "router_forbidden"])
def test_a_refused_fire_now_raises_an_error_toast_with_the_text_the_detail_page_shows(row, envelopes, which: str) -> None:
    _outcome(row, envelopes[which])
    _press(row, "fire")
    toasts = _toasts(row)
    assert len(toasts) == 1 and toasts[0]["kind"] == "error", toasts
    assert toasts[0]["title"] == "Fire failed"
    assert toasts[0]["detail"] == json.loads(row.eval(f"JSON.stringify(refusalText({json.dumps(envelopes[which])}, 'Fire failed'))")), "the same reader and fallback as the detail page's Fire now"
    assert toasts[0]["detail"] not in ("auth_required", "forbidden_role", "trigger_not_found"), "never a bare code"


@pytest.mark.parametrize("what", ["fire", "delete"])
def test_the_error_toast_carries_the_request_id_of_the_refusal(row, what: str) -> None:
    """The toast shows ``request-id`` with a copy link when it has one: what an operator pastes into a bug report."""
    envelope = {"type": "/errors/not-found", "title": "Not Found", "status": 404, "detail": "trigger 'tr-1' not found", "extensions": {"code": "trigger_not_found", "request_id": "req-abc123"}}
    _outcome(row, envelope)
    _press(row, what)
    assert _toasts(row)[0]["requestId"] == "req-abc123"


def test_the_session_that_ended_says_so_in_words(row, envelopes) -> None:
    _outcome(row, envelopes["session_ended"])
    _press(row, "fire")
    assert "sign in again" in _toasts(row)[0]["detail"]


def test_a_refused_fire_now_leaves_the_row_alone_and_gives_the_buttons_back(row, envelopes) -> None:
    _outcome(row, envelopes["not_found"])
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


@pytest.mark.parametrize("which", ["session_ended", "role_refused", "not_found"])
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
