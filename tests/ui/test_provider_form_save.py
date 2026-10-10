"""The provider form keeps Save off until the kind's fields have loaded, and shows a Save that was refused (board task 01a12350-792d).

Found on main by the lead (ui-e2e ``test_u0047_provider_list_reflects_new_row_after_modal_create`` failed on main 919f3f28 and on #700, passed on neighbouring commits: a race). The Register menu
(``PC_RegisterDropdown``) fetched ``GET /<plural>/_types`` under the key ``provider-register-types:<plural>`` and the form (``PC_ProviderForm``) fetched the SAME URL under ``provider-types:<plural>``, so the
form started with nothing. Until that second answer arrived ``shape`` was ``{}``: no config fields (so no API key box), no Limits, nothing required, so Save was ENABLED, and ``PC_submittable(draft, {}, ...)``
sent no ``limits``, which every class requires: ``POST /v1/llm_providers`` answered 422 ``limits: Field required``. The catalog's ``save()`` has no ``catch`` and the button ignored the promise, so the 422
became an unhandled rejection and the modal stayed open saying nothing.

The REAL form runs in V8 on the mini React (``tests/ui/_mini_react.py``) with the real ``ui/foundation/api.js`` (``ApiError``, ``readRefusal``), the real 422 envelope the server answered for that POST and
the real ``/_types`` shapes of the ``anthropic`` and ``web_fetch`` ``local`` kinds (captured from a running install).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")

# GET /v1/llm_providers/_types (the anthropic entry) and GET /v1/web_fetch_providers/_types (the local entry), as the server serves them
ANTHROPIC = {
    "label": "Anthropic",
    "config_fields": [{"key": "api_key", "label": "API key (optional)", "type": "password", "required": False, "help": "Required for the real Anthropic API; leave blank only when an upstream proxy supplies auth."}],
    "row_fields": [], "discoverable": True, "limits": True,
}
WEB_FETCH_LOCAL = {"config_fields": []}
# POST /v1/llm_providers with no "limits": the 422 the operator never saw
LIMITS_422 = {
    "type": "/errors/validation-error", "title": "Validation Error", "status": 422,
    "detail": "One or more request parameters or body fields failed validation.", "instance": "/v1/llm_providers",
    "extensions": {"errors": [{"type": "missing", "loc": ["body", "limits"], "msg": "Field required"}], "request_id": "req-0aea9d32c0e9"},
}

_PRELUDE = """
window.addEventListener = function () {}; window.removeEventListener = function () {};
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], disabled: p.disabled, "aria-busy": p["aria-busy"], onClick: p.onClick }, p.children); }
function Banner(p) { return React.createElement("div", { "data-testid": p["data-testid"] }, p.title, p.children); }
function Icon() { return null; }
"""

_DRIVER = """
var __keys = []; var __refetched = 0;
var __types = { data: undefined, error: null, loading: true, refetch: function () { __refetched += 1; } };
window.primerApi.useResource = function (key) { __keys.push(key); return __types; };
window.primerApi.useCapabilities = function () { return { data: { extras: {} } }; };
window.primerApi.capabilityHint = function (e) { return String(e); };
window.primerApi.EXTRA_FOR_PROVIDER_TYPE = {};
window.primerApi.apiFetch = function () { return Promise.resolve({ ok: true }); };
var __submitted = []; var __cancelled = 0; var __onSubmit = function (body) { __submitted.push(body); return Promise.resolve(); };
var __settled = null; var __release = null;
function mountForm(props) {
  MR.mount(PC_ProviderForm, Object.assign({
    plural: "llm_providers", typesPath: "/llm_providers/_types", value: { provider: "anthropic", id: "p1" }, onChange: function () {},
    onSubmit: function (body) { return __onSubmit(body); }, onCancel: function () { __cancelled += 1; }, editing: false, existingId: null, canInvalidate: false,
  }, props || {}));
}
function textOf(n) {
  var out = [];
  (function collect(x) {
    if (x == null || typeof x === "boolean") return;
    if (typeof x === "string" || typeof x === "number") { out.push(String(x)); return; }
    if (Array.isArray(x)) { x.forEach(collect); return; }
    if (x.__el) collect(typeof x.type === "function" && x.type !== React.Fragment ? x.out : x.children);
  })(n);
  return out.join("");
}
function press(testid) {
  __settled = "pending";
  var out = MR.find(testid).props.onClick();
  Promise.resolve(out).then(function () { __settled = "fulfilled"; }, function () { __settled = "rejected"; });
}
"""


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(ROOT / "ui" / "components" / "provider-form.jsx")


@pytest.fixture
def ctx(code):
    c = mini_react_context(API + "\n" + code, _PRELUDE)
    c.eval(_DRIVER)
    try:
        yield c
    finally:
        c.close()


def _types(ctx, **state) -> None:
    ctx.eval("Object.assign(__types, " + json.dumps({"error": None, "loading": False, **state}) + "); MR.rerender();")


def _mount(ctx, **props) -> None:
    ctx.eval(f"mountForm({json.dumps(props)});")


def _props(ctx, testid: str) -> dict | None:
    return json.loads(ctx.eval(
        f"(function () {{ var el = MR.find({json.dumps(testid)}); return el ? JSON.stringify({{ disabled: !!el.props.disabled, busy: el.props['aria-busy'] === undefined ? null : el.props['aria-busy'], role: el.props.role || null }}) : 'null'; }})()"
    ))


def _text_of(ctx, testid: str) -> str | None:
    return ctx.eval(f"(function () {{ var el = MR.find({json.dumps(testid)}); return el ? textOf(el) : null; }})()")


def _settle(ctx) -> str:
    ctx.eval("void 0;")
    ctx.eval("MR.rerender();")
    return ctx.eval("__settled")


def test_save_and_test_are_off_and_a_loading_line_is_shown_until_the_kinds_fields_have_arrived(ctx) -> None:
    """F1: ``shape`` is ``{}`` until ``_types`` answers, which offered Save with no Limits and no API key box."""
    _mount(ctx, plural="web_fetch_providers", typesPath="/web_fetch_providers/_types", value={"provider": "local", "id": "p1"})
    assert _props(ctx, "provider-form-save")["disabled"] is True, "Save was offered before the fields were known"
    assert _props(ctx, "provider-form-test")["disabled"] is True
    assert _props(ctx, "provider-form-loading") is not None, "nothing said that the fields are loading"
    _types(ctx, data={"local": WEB_FETCH_LOCAL})
    assert _props(ctx, "provider-form-save")["disabled"] is False
    assert _props(ctx, "provider-form-test")["disabled"] is False
    assert _props(ctx, "provider-form-loading") is None


def test_save_is_offered_once_the_fields_are_known_and_sends_what_the_form_shows(ctx) -> None:
    _mount(ctx)
    _types(ctx, data={"anthropic": ANTHROPIC})
    assert _props(ctx, "provider-form-save")["disabled"] is False
    ctx.eval("press('provider-form-save');")
    assert _settle(ctx) == "fulfilled"
    (body,) = json.loads(ctx.eval("JSON.stringify(__submitted)"))
    assert body["provider"] == "anthropic" and body["limits"] == {"max_concurrency": 1}, body


def test_a_kind_the_install_does_not_serve_is_said_and_save_stays_off(ctx) -> None:
    """F1: the types arrived but not the kind this draft names; a form for it would send a request nothing can validate."""
    _mount(ctx, value={"provider": "retired-kind", "id": "p1"})
    _types(ctx, data={"anthropic": ANTHROPIC})
    assert _props(ctx, "provider-form-save")["disabled"] is True
    assert "retired-kind" in (_text_of(ctx, "provider-form-kind-missing") or ""), "the form must say that the kind is not served"
    assert _props(ctx, "provider-form-loading") is None, "this is not a load in progress"


def test_a_types_error_is_shown_in_the_form_with_a_way_to_cancel_and_try_again(ctx) -> None:
    """F1: the form used to be replaced by a banner with no Cancel; the error now sits in the form, Save is off, and the request can be repeated."""
    _mount(ctx)
    envelope = {"type": "/errors/internal", "title": "Internal Server Error", "status": 500, "detail": "the types could not be read", "instance": "/v1/llm_providers/_types"}
    ctx.eval("__types.error = new window.primerApi.ApiError(" + json.dumps(envelope) + "); __types.loading = false; MR.rerender();")
    alert = _props(ctx, "provider-form-types-error")
    assert alert is not None and alert["role"] == "alert"
    assert "the types could not be read" in (_text_of(ctx, "provider-form-types-error") or "")
    assert _props(ctx, "provider-form-save")["disabled"] is True
    assert _props(ctx, "provider-form-cancel") is not None, "an operator who cannot load the fields must still be able to leave"
    ctx.eval("MR.find('provider-form-types-retry').props.onClick(); MR.rerender();")
    assert ctx.eval("__refetched") == 1


@pytest.mark.parametrize("editing", [False, True], ids=["create", "edit"])
def test_a_save_the_server_refused_is_shown_in_the_form_and_never_rejects(ctx, editing: bool) -> None:
    """F2: the 422 for a missing ``limits`` was an unhandled rejection and nothing on screen. The button's handler returns a promise that settles fulfilled, the refusal is in a ``role="alert"`` line
    (``provider-form-save-error``) in the words of the one reader (``readRefusal``), and Save works again for another try. The edit (PUT) path is the same form."""
    props = {"editing": True, "existingId": "p1"} if editing else {}
    _mount(ctx, **props)
    _types(ctx, data={"anthropic": ANTHROPIC})
    ctx.eval("__onSubmit = function () { return Promise.reject(new window.primerApi.ApiError(" + json.dumps(LIMITS_422) + ")); };")
    ctx.eval("press('provider-form-save');")
    assert _settle(ctx) == "fulfilled", "the click handler's promise rejected: an unhandled rejection in the browser"
    alert = _props(ctx, "provider-form-save-error")
    assert alert is not None, "a refused Save said nothing"
    assert alert["role"] == "alert"
    text = _text_of(ctx, "provider-form-save-error") or ""
    assert "limits" in text and "Field required" in text, text
    assert _props(ctx, "provider-form-save")["disabled"] is False, "Save must work again after a refusal"


def test_the_refusal_goes_away_when_the_next_save_is_pressed(ctx) -> None:
    _mount(ctx)
    _types(ctx, data={"anthropic": ANTHROPIC})
    ctx.eval("__onSubmit = function () { return Promise.reject(new window.primerApi.ApiError(" + json.dumps(LIMITS_422) + ")); }; press('provider-form-save');")
    _settle(ctx)
    assert _props(ctx, "provider-form-save-error") is not None
    ctx.eval("__onSubmit = function () { return new Promise(function (resolve) { __release = resolve; }); }; press('provider-form-save'); MR.rerender();")
    assert _props(ctx, "provider-form-save-error") is None, "the old refusal stayed on screen while the new request was out"


def test_save_is_busy_while_the_request_is_out_and_a_second_press_is_not_possible(ctx) -> None:
    """F2: ``busy`` covered only the Test round trip; a double click on Save sent two POSTs."""
    _mount(ctx)
    _types(ctx, data={"anthropic": ANTHROPIC})
    ctx.eval("__onSubmit = function (body) { __submitted.push(body); return new Promise(function (resolve) { __release = resolve; }); }; press('provider-form-save'); MR.rerender();")
    save = _props(ctx, "provider-form-save")
    assert save["disabled"] is True and save["busy"] in (True, "true"), save
    ctx.eval("__release(); void 0;")
    assert _settle(ctx) == "fulfilled"
    assert _props(ctx, "provider-form-save")["disabled"] is False
    assert ctx.eval("__submitted.length") == 1


def test_the_form_and_the_register_menu_read_the_same_types_under_one_key(ctx) -> None:
    """F3: one request, not two. The key is built in one place (``PC_typesKey``), and the catalog's Register menu uses it too."""
    _mount(ctx)
    keys = json.loads(ctx.eval("JSON.stringify(__keys)"))
    assert keys and set(keys) == {ctx.eval("PC_typesKey('llm_providers')")}, keys
    catalog = (ROOT / "ui" / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")
    assert "provider-register-types" not in catalog and "PC_typesKey(" in catalog
