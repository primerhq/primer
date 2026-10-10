"""The provider form keeps Save off until the kind's fields have loaded, and shows a Save that was refused (board task 01a12350-792d).

Found on main by the lead (ui-e2e ``test_u0047_provider_list_reflects_new_row_after_modal_create`` failed on main 919f3f28 and on #700, passed on neighbouring commits: a race). The Register menu
(``PC_RegisterDropdown``) fetched ``GET /<plural>/_types`` under the key ``provider-register-types:<plural>`` and the form (``PC_ProviderForm``) fetched the SAME URL under ``provider-types:<plural>``, so the
form started with nothing. Until that second answer arrived ``shape`` was ``{}``: no config fields (so no API key box), no Limits, nothing required, so Save was ENABLED, and ``PC_submittable(draft, {}, ...)``
sent no ``limits``, which the LLM provider class requires: ``POST /v1/llm_providers`` answered 422 ``limits: Field required``. The catalog's ``save()`` has no ``catch`` and the button ignored the promise, so the
422 became an unhandled rejection and the modal stayed open saying nothing.

Round 2 (the #716 review): a button that turns ``disabled`` while it has focus drops the focus to ``<body>``, outside the dialog, so Save stays focusable while the request is out (``aria-disabled`` and
``aria-busy``, with a ref guard against a second submit); Try again sits after the alert, says it is trying, and stays focusable while it does.

The REAL form runs in V8 on the mini React (``tests/ui/_mini_react.py``) with the real ``ui/foundation/api.js`` (``ApiError``, ``readRefusal``). What the server answers is not typed in here: the ``/_types``
maps are what the route functions return, and the 422 is what the real ``llm_providers`` router answers (through the real error handlers) for a body without ``limits``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")


def _llm_types() -> dict:
    from primer.api.routers.providers import list_llm_provider_types

    return asyncio.run(list_llm_provider_types())


def _web_fetch_types() -> dict:
    from primer.api.routers.web_fetch import list_provider_types

    return asyncio.run(list_provider_types())


def _limits_422() -> dict:
    """What ``POST /v1/llm_providers`` answers for a body with no ``limits``: the real router and the real error handlers, a stand-in for the storage nothing reaches."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from primer.api.errors import register_error_handlers
    from primer.api.routers import providers

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(providers.llm_provider_router, prefix="/v1")
    app.dependency_overrides[providers.get_llm_provider_storage] = lambda: object()
    answer = TestClient(app, raise_server_exceptions=False).post("/v1/llm_providers", json={"provider": "anthropic", "id": "p1", "config": {}})
    assert answer.status_code == 422, answer.text
    return answer.json()



_PRELUDE = """
window.addEventListener = function () {}; window.removeEventListener = function () {};
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], disabled: p.disabled, "aria-busy": p["aria-busy"], "aria-disabled": p["aria-disabled"], onClick: p.onClick }, p.children); }
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
def anthropic() -> dict:
    """The ``anthropic`` entry of ``GET /v1/llm_providers/_types``, as the route function answers it."""
    return _llm_types()["anthropic"]


@pytest.fixture(scope="module")
def web_fetch_local() -> dict:
    """The ``local`` entry of ``GET /v1/web_fetch_providers/_types``."""
    return _web_fetch_types()["local"]


@pytest.fixture(scope="module")
def limits_422() -> dict:
    return _limits_422()


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(ROOT / "ui" / "components" / "provider-form.jsx")


@pytest.fixture(scope="module")
def catalog_code() -> str:
    return transpile(ROOT / "ui" / "components" / "provider-catalog.jsx")


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
        f"(function () {{ var el = MR.find({json.dumps(testid)}); if (!el) return 'null'; var p = el.props; "
        "var attr = function (k) { return p[k] === undefined ? null : p[k]; }; "
        "return JSON.stringify({ disabled: !!p.disabled, busy: attr('aria-busy'), aria_disabled: attr('aria-disabled'), role: p.role || null }); })()"
    ))


def _text_of(ctx, testid: str) -> str | None:
    return ctx.eval(f"(function () {{ var el = MR.find({json.dumps(testid)}); return el ? textOf(el) : null; }})()")


def _settle(ctx) -> str:
    ctx.eval("void 0;")
    ctx.eval("MR.rerender();")
    return ctx.eval("__settled")


def _fail_types(ctx, **state) -> None:
    """The fetch of ``_types`` failed with a 500 (the envelope shape the console reads)."""
    envelope = {"type": "/errors/internal", "title": "Internal Server Error", "status": 500, "detail": "the types could not be read", "instance": "/v1/llm_providers/_types"}
    assign = "".join(f"__types.{k} = {json.dumps(v)}; " for k, v in {"loading": False, **state}.items())
    ctx.eval("__types.error = new window.primerApi.ApiError(" + json.dumps(envelope) + "); " + assign + "MR.rerender();")


def test_the_shapes_and_the_refusal_are_the_servers_own(anthropic, limits_422, web_fetch_local) -> None:
    """The stand-ins are not typed in: they are what the producers answer, so a server-side change in them changes what these tests read."""
    assert anthropic["limits"] is True and any(f["key"] == "api_key" for f in anthropic["config_fields"]), anthropic
    assert web_fetch_local == {"config_fields": []}
    assert limits_422["status"] == 422 and limits_422["extensions"]["errors"][0]["loc"] == ["body", "limits"], limits_422


def test_save_and_test_are_off_and_a_loading_line_is_shown_until_the_kinds_fields_have_arrived(ctx, web_fetch_local) -> None:
    """F1: ``shape`` is ``{}`` until ``_types`` answers, which offered Save with no Limits and no API key box."""
    _mount(ctx, plural="web_fetch_providers", typesPath="/web_fetch_providers/_types", value={"provider": "local", "id": "p1"})
    assert _props(ctx, "provider-form-save")["disabled"] is True, "Save was offered before the fields were known"
    assert _props(ctx, "provider-form-test")["disabled"] is True
    loading = _props(ctx, "provider-form-loading")
    assert loading is not None, "nothing said that the fields are loading"
    assert loading["role"] == "status", "the loading line is a status for a screen reader"
    _types(ctx, data={"local": web_fetch_local})
    assert _props(ctx, "provider-form-save")["disabled"] is False
    assert _props(ctx, "provider-form-test")["disabled"] is False
    assert _props(ctx, "provider-form-loading") is None


def test_save_is_offered_once_the_fields_are_known_and_sends_what_the_form_shows(ctx, anthropic) -> None:
    _mount(ctx)
    _types(ctx, data={"anthropic": anthropic})
    assert _props(ctx, "provider-form-save")["disabled"] is False
    ctx.eval("press('provider-form-save');")
    assert _settle(ctx) == "fulfilled"
    (body,) = json.loads(ctx.eval("JSON.stringify(__submitted)"))
    assert body["provider"] == "anthropic" and body["limits"] == {"max_concurrency": 1}, body


def test_a_kind_the_install_does_not_serve_is_said_and_save_stays_off(ctx, anthropic) -> None:
    """F1: the types arrived but not the kind this draft names; a form for it would send a request nothing can validate."""
    _mount(ctx, value={"provider": "retired-kind", "id": "p1"})
    _types(ctx, data={"anthropic": anthropic})
    assert _props(ctx, "provider-form-save")["disabled"] is True
    missing = _props(ctx, "provider-form-kind-missing")
    assert missing is not None and missing["role"] == "alert"
    assert "retired-kind" in (_text_of(ctx, "provider-form-kind-missing") or ""), "the form must say that the kind is not served"
    assert _props(ctx, "provider-form-loading") is None, "this is not a load in progress"


def test_a_types_error_is_shown_in_the_form_with_a_way_to_cancel_and_try_again(ctx) -> None:
    """F1: the form used to be replaced by a banner with no Cancel; the error now sits in the form, Save is off, and the request can be repeated."""
    _mount(ctx)
    _fail_types(ctx)
    alert = _props(ctx, "provider-form-types-error")
    assert alert is not None and alert["role"] == "alert"
    assert "the types could not be read" in (_text_of(ctx, "provider-form-types-error") or "")
    assert "Try again" not in (_text_of(ctx, "provider-form-types-error") or ""), "the button is not part of what the alert announces"
    assert _props(ctx, "provider-form-save")["disabled"] is True
    assert _props(ctx, "provider-form-cancel") is not None, "an operator who cannot load the fields must still be able to leave"
    ctx.eval("MR.find('provider-form-types-retry').props.onClick(); MR.rerender();")
    assert ctx.eval("__refetched") == 1


def test_a_retry_that_is_out_says_so_stays_focusable_and_is_not_sent_twice(ctx) -> None:
    """The error stays while the retry is out (``useResource`` keeps ``error`` until the answer), so the form shows the retry: no loading line under the alert, the button says it is trying, it stays
    focusable (``aria-disabled``, not ``disabled``: a button that is disabled while it has focus drops the focus out of the dialog) and a second press sends nothing."""
    _mount(ctx)
    _fail_types(ctx, loading=True)
    assert _props(ctx, "provider-form-loading") is None, "a loading line under the error alert"
    retry = _props(ctx, "provider-form-types-retry")
    assert retry["disabled"] is False and retry["aria_disabled"] == "true" and retry["busy"] == "true", retry
    assert "Trying again" in (_text_of(ctx, "provider-form-types-retry") or "")
    ctx.eval("MR.find('provider-form-types-retry').props.onClick(); MR.rerender();")
    assert ctx.eval("__refetched") == 0, "a retry that is already out was sent again"
    ctx.eval("__types.loading = false; MR.rerender();")
    retry = _props(ctx, "provider-form-types-retry")
    assert retry["aria_disabled"] is None and retry["busy"] is None and "Try again" in (_text_of(ctx, "provider-form-types-retry") or "")


def test_a_later_failure_keeps_the_fields_it_already_has(ctx, anthropic) -> None:
    """``useResource`` is stale-while-error: data that arrived stays when a later fetch fails, and the form keeps showing the kind's fields rather than an alert over a usable form."""
    _mount(ctx)
    _types(ctx, data={"anthropic": anthropic})
    _fail_types(ctx, data={"anthropic": anthropic})
    assert _props(ctx, "provider-form-types-error") is None, "an alert over a form that has its fields"
    assert "API key" in (_text_of(ctx, "provider-form-llm_providers") or ""), "the kind's fields went away"
    assert _props(ctx, "provider-form-save")["disabled"] is False


@pytest.mark.parametrize("editing", [False, True], ids=["create", "edit"])
def test_a_save_the_server_refused_is_shown_in_the_form_and_never_rejects(ctx, editing: bool, anthropic, limits_422) -> None:
    """F2: the 422 for a missing ``limits`` was an unhandled rejection and nothing on screen. The button's handler returns a promise that settles fulfilled, the refusal is in a ``role="alert"`` line
    (``provider-form-save-error``) in the words of the one reader (``readRefusal``), and Save works again for another try. The edit (PUT) path is the same form."""
    props = {"editing": True, "existingId": "p1"} if editing else {}
    _mount(ctx, **props)
    _types(ctx, data={"anthropic": anthropic})
    ctx.eval("__onSubmit = function () { return Promise.reject(new window.primerApi.ApiError(" + json.dumps(limits_422) + ")); };")
    ctx.eval("press('provider-form-save');")
    assert _settle(ctx) == "fulfilled", "the click handler's promise rejected: an unhandled rejection in the browser"
    alert = _props(ctx, "provider-form-save-error")
    assert alert is not None, "a refused Save said nothing"
    assert alert["role"] == "alert"
    text = _text_of(ctx, "provider-form-save-error") or ""
    assert "limits" in text and "Field required" in text, text
    save = _props(ctx, "provider-form-save")
    assert save["disabled"] is False and save["aria_disabled"] is None and save["busy"] is None, "Save must work again after a refusal"


def test_the_refusal_goes_away_when_the_next_save_is_pressed(ctx, anthropic, limits_422) -> None:
    _mount(ctx)
    _types(ctx, data={"anthropic": anthropic})
    ctx.eval("__onSubmit = function () { return Promise.reject(new window.primerApi.ApiError(" + json.dumps(limits_422) + ")); }; press('provider-form-save');")
    _settle(ctx)
    assert _props(ctx, "provider-form-save-error") is not None
    ctx.eval("__onSubmit = function () { return new Promise(function (resolve) { __release = resolve; }); }; press('provider-form-save'); MR.rerender();")
    assert _props(ctx, "provider-form-save-error") is None, "the old refusal stayed on screen while the new request was out"


def test_save_stays_focusable_while_the_request_is_out_and_a_second_press_sends_nothing(ctx, anthropic) -> None:
    """F2 and the review's B1: ``busy`` covered only the Test round trip, so a double click on Save sent two POSTs; the first fix made Save ``disabled`` while the request was out, and a focused button that
    turns disabled drops the focus to ``<body>``, outside the dialog (the focus trap listens on the dialog), so after a refusal Tab walked the page behind the scrim. Save is ``aria-disabled`` and
    ``aria-busy`` instead: it keeps its focus, and the handler refuses a second submit itself."""
    _mount(ctx)
    _types(ctx, data={"anthropic": anthropic})
    ctx.eval("__onSubmit = function (body) { __submitted.push(body); return new Promise(function (resolve) { __release = resolve; }); }; press('provider-form-save'); MR.rerender();")
    save = _props(ctx, "provider-form-save")
    assert save["disabled"] is False, "Save turned disabled while it has the focus: the focus drops out of the dialog"
    assert save["aria_disabled"] == "true" and save["busy"] == "true", save
    ctx.eval("MR.find('provider-form-save').props.onClick(); MR.find('provider-form-save').props.onClick(); void 0;")
    assert ctx.eval("__submitted.length") == 1, "a second press while the request was out sent a second request"
    ctx.eval("__release(); void 0;")
    assert _settle(ctx) == "fulfilled"
    save = _props(ctx, "provider-form-save")
    assert save["disabled"] is False and save["aria_disabled"] is None and save["busy"] is None
    ctx.eval("press('provider-form-save');")
    assert ctx.eval("__submitted.length") == 2, "Save did not work again once the first request was answered"


def test_a_save_that_cannot_be_pressed_for_another_reason_is_still_disabled(ctx, anthropic) -> None:
    """Only the request-in-flight state moves to ``aria-disabled``; a blank Name or an unknown shape stays a real ``disabled``."""
    _mount(ctx, value={"provider": "anthropic", "id": ""})
    _types(ctx, data={"anthropic": anthropic})
    assert _props(ctx, "provider-form-save")["disabled"] is True


def test_the_form_and_the_register_menu_read_the_same_types_under_one_key(code, catalog_code) -> None:
    """F3: one request, not two. The Register menu (the REAL ``PC_RegisterDropdown``) and the form are mounted against a ``useResource`` that records the key each asks for, and they ask for the same one;
    a form handed another ``typesPath`` reads another resource (the key follows the path, not the class alone)."""
    c = mini_react_context(API + "\n" + code + "\n" + catalog_code, _PRELUDE)
    try:
        c.eval(_DRIVER)
        c.eval("MR.mount(PC_RegisterDropdown, { klass: { plural: 'llm_providers' }, onPick: function () {} });")
        menu_keys = json.loads(c.eval("JSON.stringify(__keys)"))
        c.eval("__keys.length = 0; mountForm({});")
        form_keys = json.loads(c.eval("JSON.stringify(__keys)"))
        c.eval("__keys.length = 0; mountForm({ typesPath: '/elsewhere/_types' });")
        other_keys = json.loads(c.eval("JSON.stringify(__keys)"))
    finally:
        c.close()
    assert menu_keys and form_keys and set(menu_keys) == set(form_keys) and len(set(menu_keys)) == 1, (menu_keys, form_keys)
    assert other_keys and set(other_keys).isdisjoint(form_keys), (other_keys, form_keys)


def _prime(ctx, *, error: bool = False, data: dict | None = None, loading: bool = False) -> None:
    """The shared ``_types`` entry as a component finds it when it MOUNTS (set before the mount, no rerender)."""
    envelope = {"type": "/errors/internal", "title": "Internal Server Error", "status": 500, "detail": "the types could not be read", "instance": "/v1/llm_providers/_types"}
    failure = "new window.primerApi.ApiError(" + json.dumps(envelope) + ")" if error else "null"
    ctx.eval(f"__types.error = {failure}; __types.data = {json.dumps(data)}; __types.loading = {json.dumps(loading)};")


@pytest.mark.parametrize(
    ("entry", "asks"),
    [
        pytest.param({"error": True}, 1, id="failed-and-idle"),
        pytest.param({"error": True, "loading": True}, 0, id="failed-but-a-retry-is-out"),
        pytest.param({"error": True, "data": "anthropic"}, 0, id="failed-with-stale-data"),
        pytest.param({"data": "anthropic"}, 0, id="healthy"),
        pytest.param({"loading": True}, 0, id="first-load-in-flight"),
    ],
)
def test_a_form_that_mounts_on_a_failed_idle_entry_asks_once(ctx, anthropic, entry: dict, asks: int) -> None:
    """The Register menu and the form share one cache entry (one request, not two), and ``useResource`` only copies an entry's state to whoever joins it: a menu request that FAILED reaches every form opened
    after it, which showed the menu's old error with Save off and never fetched. A form that mounts on a failed entry with nothing in flight asks once; it does not ask when data is there (stale or not), when a
    request is already out, or when nothing has failed."""
    state = dict(entry)
    if state.get("data") == "anthropic":
        state["data"] = {"anthropic": anthropic}
    _prime(ctx, **state)
    _mount(ctx)
    assert ctx.eval("__refetched") == asks


@pytest.mark.parametrize(
    ("entry", "asks"),
    [
        pytest.param({"error": True}, 1, id="failed-and-idle"),
        pytest.param({"error": True, "loading": True}, 0, id="failed-but-a-retry-is-out"),
        pytest.param({"data": "anthropic"}, 0, id="healthy"),
    ],
)
def test_the_register_menu_asks_again_when_it_is_opened_on_a_failed_idle_entry(code, catalog_code, anthropic, entry: dict, asks: int) -> None:
    """The menu said 'No kinds available.' for a failed request and nothing could repair it but a reload. Opening it on a failed entry with nothing in flight asks once; mounting alone asks nothing."""
    c = mini_react_context(API + "\n" + code + "\n" + catalog_code, _PRELUDE)
    try:
        c.eval(_DRIVER)
        state = dict(entry)
        if state.get("data") == "anthropic":
            state["data"] = {"anthropic": anthropic}
        _prime(c, **state)
        c.eval("MR.mount(PC_RegisterDropdown, { klass: { plural: 'llm_providers' }, onPick: function () {} });")
        assert c.eval("__refetched") == 0, "mounting the menu asked"
        c.eval("MR.find('provider-register-toggle').props.onClick(); MR.rerender();")
        assert c.eval("__refetched") == asks
    finally:
        c.close()
