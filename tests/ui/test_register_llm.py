"""The LLM pages of the Register provider overlay (plan 02, Tasks 12 and 13; design 6.3, 6.4, 6.9 and 13.3; master Review Focus 1, 3, 4 and 5).

An LLM provider is registered in two pages: the connection (id, type, flavor, URL, key, limits), whose "Next" lists the models the provider serves, and the models
page, whose "Import N models" creates the provider and then one LLM profile per ticked model, one after another. Editing is the first page again, with the id and
type locked, "Save", and "Import more models", which runs the second page against the SAVED provider.

Everything runs for real in V8 (``tests/ui/_register_harness.py``): the real kit, form, foundation and overlay, the real ``register-llm.jsx``, the ``_types`` maps the
route functions answer, and refusals built by the real error handlers (and, for a 422, the real LLM provider router). Only the network is a script: ``route`` says what
each ``METHOD /path`` answers (an answer, a refusal, or a request held until the test releases it).

A hand-off of the focus needs a DOM, which this harness does not have: ``PC_LlmFocus`` is driven here with a document of its own, and
``tests/ui_e2e/test_register_llm_focus_journey.py`` is the journey that drives the page in a browser.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._register_harness import COMPONENTS, UI, base_code, context, js, settle, transpiled, types_of

LLM_FILE = COMPONENTS / "providers" / "register-llm.jsx"

STUBS = r"""
var SAVED = [], CLOSED = 0;
var ROUTES = {}, HELD = {};
// "METHOD /path" -> { answer } | { refuse: envelope } | { hold: name } (or a list of them: one per call, the last one repeating). An unscripted request is answered with
// the body it was sent, as a create answers the row it stored.
HANDLER = function (method, path, body) {
  var r = ROUTES[method + " " + path];
  if (r === undefined) return Promise.resolve(body == null ? { ok: true } : JSON.parse(JSON.stringify(body)));
  if (Array.isArray(r)) r = r.length > 1 ? r.shift() : r[0];
  if (r.refuse) return refuse(r.refuse);
  if (r.hold) return new Promise(function (ok, no) { HELD[r.hold] = { ok: ok, no: no }; });
  return Promise.resolve(JSON.parse(JSON.stringify(r.answer)));
};
// A caller mounts the overlay afresh each time it opens it; the harness keeps a component's state while the same type stays at the root, so the root is emptied first.
function mountOverlay(props) {
  var base = { onClose: function () { CLOSED += 1; }, onSaved: function (row) { SAVED.push(row); } };
  MR.mount(function Closed() { return null; }, {});
  MR.mount(PC_RegisterOverlay, Object.assign(base, props));
}
function options(testid) {
  return ALL(testid).filter(function (n) { return n.type === "option"; }).map(function (n) { return n.props.value; });
}
function calls(method, path) {
  return CALLS.filter(function (c) { return (!method || c.method === method) && (!path || c.path === path); });
}
function value(testid) { var n = MR.find(testid); return n ? n.props.value : null; }
function text(testid) { return textOf(MR.find(testid)); }
function disabled(testid) { var n = nativeButton(testid); return n ? !!n.props.disabled : null; }
function ariaDisabled(testid) { var n = nativeButton(testid); return n ? n.props["aria-disabled"] === "true" : null; }
function ariaBusy(testid) { var n = nativeButton(testid); return n ? n.props["aria-busy"] === "true" : null; }
function tick(name) { MR.find("pc-import:" + name).props.onChange({}); MR.rerender(); }
"""

URL = "http://lm.test/v1"


@pytest.fixture(scope="module")
def code() -> str:
    return base_code(with_overlay=True) + "\n" + transpiled(LLM_FILE)


@pytest.fixture
def ctx(code):
    c = context(code)
    c.eval(STUBS)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(scope="module")
def envelopes() -> dict[str, dict]:
    """The refusals the console reads, answered by the real error handlers (and, for the 422, by the real LLM provider router)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from primer.api.errors import register_error_handlers
    from primer.api.routers import providers
    from primer.api.routers.providers import _probe_failure
    from primer.model.except_ import ConflictError

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(providers.llm_provider_router, prefix="/v1")
    app.dependency_overrides[providers.get_llm_provider_storage] = lambda: object()

    @app.post("/v1/probe_failed")
    def probe_failed():
        raise _probe_failure("openai-compatible probe failed: ConnectError: All connection attempts failed")

    @app.post("/v1/provider_taken")
    def provider_taken():
        raise ConflictError("LLMProvider with id 'lm' already exists")

    @app.post("/v1/profile_taken")
    def profile_taken():
        raise ConflictError("ModelProfile with id 'lm--b' already exists")

    @app.post("/v1/profile_broke")
    def profile_broke():
        raise RuntimeError("the database went away")

    client = TestClient(app, raise_server_exceptions=False)
    made = {
        "probe_failed": client.post("/v1/probe_failed"),
        "provider_taken": client.post("/v1/provider_taken"),
        "profile_taken": client.post("/v1/profile_taken"),
        "profile_broke": client.post("/v1/profile_broke"),
        # a body the real router refuses before it touches storage: no config and no limits
        "put_422": client.put("/v1/llm_providers/lm", json={"id": "lm", "provider": "openchat"}),
    }
    expected = {"probe_failed": 400, "provider_taken": 409, "profile_taken": 409, "profile_broke": 500, "put_422": 422}
    for name, answer in made.items():
        assert answer.status_code == expected[name], (name, answer.status_code, answer.text)
    return {name: answer.json() for name, answer in made.items()}


def _route(ctx, table: dict) -> None:
    ctx.eval(f"ROUTES = {json.dumps(table)};")


def _serve(ctx) -> None:
    ctx.eval(f"serve(PC_typesKey('llm_providers'), {json.dumps(types_of('llm_providers'))}); MR.rerender();")


def _open(ctx, **props) -> None:
    props.setdefault("initialClass", "llm")
    ctx.eval(f"mountOverlay({json.dumps(props)});")
    _serve(ctx)


def _type(ctx, testid: str, text: str) -> None:
    ctx.eval(f"setValue({json.dumps(testid)}, {json.dumps(text)})")


def _press(ctx, testid: str) -> str:
    ctx.eval(f"press({json.dumps(testid)})")
    return settle(ctx)


def _fill(ctx, provider_id: str = "lm", url: str | None = URL, key: str | None = None) -> None:
    _type(ctx, "pc-field:id", provider_id)
    if url is not None:
        _type(ctx, "pc-field:url", url)
    if key is not None:
        _type(ctx, "pc-field:api_key", key)


def _models(*entries) -> dict:
    return {"answer": {"models": [{"name": e} if isinstance(e, str) else e for e in entries]}}


def _to_page_two(ctx, models, **fill) -> None:
    """Open the overlay, fill the connection page for an OpenAI-compatible server that lists ``models``, press Next, and land on the models page."""
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": _models(*models)})
    _fill(ctx, **fill)
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert ctx.eval("MR.find('pc-llm-models') !== null") is True, "the models page is up"


def _tick(ctx, *names: str) -> None:
    for name in names:
        ctx.eval(f"tick({json.dumps(name)})")


def _error(ctx) -> str | None:
    return ctx.eval("MR.find('pc-register-error') ? textOf(MR.find('pc-register-error')) : null")


def _status(ctx, name: str) -> str:
    return ctx.eval(f"text({json.dumps('pc-import-status:' + name)})")


def _help(ctx, testid: str) -> str:
    """The help line under a control of the connection page (``null`` when it has none)."""
    row = js(ctx, f"fieldRow('pc-llm-settings', {json.dumps(testid)})")
    return " ".join(c["text"] for c in row if c["className"] == "field-help" and not c["role"])


def _calls(ctx, method: str | None = None, path: str | None = None) -> list[dict]:
    return js(ctx, f"calls({json.dumps(method)}, {json.dumps(path)})")


def _requests(ctx) -> list[tuple[str, str]]:
    return [(c["method"], c["path"]) for c in js(ctx, "CALLS")]


# ---- page one ----------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "has_url"),
    [("openchat", True), ("openresponses", True), ("ollama", True), ("anthropic", False), ("gemini", False), ("openrouter", False)],
)
def test_page_one_shows_url_only_for_kinds_that_have_one(ctx, kind: str, has_url: bool) -> None:
    """The server says what a kind takes (``_types``): the URL box is there exactly when the kind serves a ``url`` field, and it is required and placeheld as served."""
    served = types_of("llm_providers")[kind]["config_fields"]
    assert has_url == any(f["key"] == "url" for f in served), "the expectation here is what the server serves"
    _open(ctx)
    _type(ctx, "pc-field:type", kind)
    assert ctx.eval("MR.find('pc-field:url') !== null") is has_url
    if has_url:
        field = next(f for f in served if f["key"] == "url")
        assert ctx.eval("MR.find('pc-field:url').props['aria-required']") == "true" and field["required"] is True
        assert ctx.eval("MR.find('pc-field:url').props.placeholder") == field["placeholder"]
        assert js(ctx, "fieldRow('pc-llm-settings', 'pc-field:url')")[0]["text"].startswith("URL"), "the box is labelled URL"


def test_the_type_select_lists_the_six_kinds_in_the_designs_order_and_starts_on_openchat(ctx) -> None:
    _open(ctx)
    assert js(ctx, "options('pc-field:type')") == ["openchat", "openresponses", "anthropic", "gemini", "ollama", "openrouter"]
    assert ctx.eval("value('pc-field:type')") == "openchat"
    labels = js(ctx, "ALL('pc-field:type').filter(function (n) { return n.type === 'option'; }).map(function (n) { return textOf(n); })")
    assert labels == ["OpenAI (completions)", "OpenAI (responses)", "Anthropic", "Gemini", "Ollama", "OpenRouter"]


def test_flavor_shows_for_the_openai_kinds_with_the_served_options(ctx) -> None:
    """Flavor is for the two OpenAI kinds. The options are the ones the server serves (completions also takes Ollama), the draft starts on ``other`` (the model's own
    default), and what the box shows is what is sent."""
    served = types_of("llm_providers")
    _open(ctx)
    openchat = next(f for f in served["openchat"]["config_fields"] if f["key"] == "flavor")["options"]
    openresponses = next(f for f in served["openresponses"]["config_fields"] if f["key"] == "flavor")["options"]
    assert "ollama" in openchat and "ollama" not in openresponses, "the premise: completions takes Ollama, responses does not"
    assert js(ctx, "options('pc-field:flavor')") == openchat
    assert ctx.eval("value('pc-field:flavor')") == "other"
    labels = js(ctx, "ALL('pc-field:flavor').filter(function (n) { return n.type === 'option'; }).map(function (n) { return textOf(n); })")
    assert labels == ["OpenAI", "LM Studio", "Ollama", "vLLM", "Other"]
    _type(ctx, "pc-field:type", "openresponses")
    assert js(ctx, "options('pc-field:flavor')") == openresponses
    assert ctx.eval("value('pc-field:flavor')") == "other"
    for kind in ("anthropic", "gemini", "ollama", "openrouter"):
        _type(ctx, "pc-field:type", kind)
        assert ctx.eval("MR.find('pc-field:flavor') === null") is True, kind


def test_the_default_flavor_is_what_the_next_sends_and_a_pick_replaces_it(ctx) -> None:
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": _models("m")})
    _fill(ctx)
    _press(ctx, "pc-register-next")
    assert _calls(ctx, "POST", "/llm_providers/_discover_models")[0]["body"]["config"]["flavor"] == "other"
    _open(ctx)
    _type(ctx, "pc-field:flavor", "lmstudio")
    _fill(ctx)
    _press(ctx, "pc-register-next")
    assert _calls(ctx, "POST", "/llm_providers/_discover_models")[-1]["body"]["config"]["flavor"] == "lmstudio"


def test_changing_the_kind_starts_its_config_over(ctx) -> None:
    """The old kind's fields are dropped with it (the form's rule): a URL typed for completions is not sent for Anthropic, and the new kind starts from its own defaults."""
    _open(ctx)
    _fill(ctx, key="sk-old")
    _type(ctx, "pc-field:type", "anthropic")
    assert ctx.eval("MR.find('pc-field:url') === null") is True
    assert ctx.eval("value('pc-field:api_key')") == ""
    _type(ctx, "pc-field:type", "openresponses")
    assert ctx.eval("value('pc-field:url')") == "" and ctx.eval("value('pc-field:flavor')") == "other"
    assert ctx.eval("value('pc-field:id')") == "lm", "the id is the page's, not the kind's"


def test_openrouter_shows_its_attribution_fields(ctx) -> None:
    served = {f["key"]: f for f in types_of("llm_providers")["openrouter"]["config_fields"]}
    assert set(served) == {"api_key", "app_name", "app_url"}, "the premise: OpenRouter serves a key and two attribution fields, and no URL"
    _open(ctx)
    _type(ctx, "pc-field:type", "openrouter")
    assert ctx.eval("MR.find('pc-field:url') === null") is True
    assert ctx.eval("MR.find('pc-field:app_name') !== null && MR.find('pc-field:app_url') !== null") is True
    shown = ctx.eval("textOf(MR.find('pc-llm-settings'))")
    assert "App name" in shown and "App URL" in shown
    assert ctx.eval("MR.find('pc-field:api_key').props['aria-required']") == "true", "the key is required for OpenRouter (as served)"
    assert "Optional for local servers" not in _help(ctx, "pc-field:api_key"), "OpenRouter is remote: the local-server help does not apply"
    assert js(ctx, "fieldRow('pc-llm-settings', 'pc-field:app_name')")[0]["text"] == "App name"
    assert js(ctx, "fieldRow('pc-llm-settings', 'pc-field:app_url')")[0]["text"] == "App URL"


def test_the_key_help_follows_the_kind(ctx) -> None:
    """A kind with a URL is a server that may run open; a hosted kind keeps the words the server serves for it; a required key needs no such word."""
    served = types_of("llm_providers")
    _open(ctx)
    assert _help(ctx, "pc-field:api_key") == "Optional for local servers"
    _type(ctx, "pc-field:type", "anthropic")
    assert _help(ctx, "pc-field:api_key") == next(f for f in served["anthropic"]["config_fields"] if f["key"] == "api_key")["help"]
    _type(ctx, "pc-field:type", "ollama")
    assert _help(ctx, "pc-field:api_key") == "Optional for local servers"


def test_limits_and_retries_show_the_model_defaults(ctx) -> None:
    _open(ctx)
    assert "LIMITS AND RETRIES" in ctx.eval("textOf(MR.find('pc-limits'))")
    assert [ctx.eval(f"value('pc-limit:{f}')") for f in ("max_concurrency", "request_timeout_seconds", "max_retries", "retry_backoff_seconds")] == ["1", "300", "2", "0.5"]
    for field in ("connect_timeout_seconds", "total_timeout_seconds", "retry_backoff_max_seconds"):
        assert ctx.eval(f"MR.find('pc-limit:{field}') === null") is True, f"{field} is folded away until it is asked for"
    ctx.eval("MR.click('pc-limits-more:toggle')")
    assert ctx.eval("value('pc-limit:retry_backoff_max_seconds')") == "8"
    assert ctx.eval("value('pc-limit:connect_timeout_seconds')") == "" and ctx.eval("value('pc-limit:total_timeout_seconds')") == ""


def test_the_limits_are_the_models_defaults_and_are_what_a_register_sends(ctx, envelopes) -> None:
    """The defaults on screen are the model's own (``Limits`` in ``primer/model/providers/_shared.py``), and a provider registered without touching them sends them."""
    from primer.model.providers._shared import Limits

    limits = Limits(max_concurrency=1)
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": {"refuse": envelopes["probe_failed"]}})
    _fill(ctx)
    _press(ctx, "pc-register-next")
    _press(ctx, "pc-register-without-models")
    (made,) = _calls(ctx, "POST", "/llm_providers")
    sent = made["body"]["limits"]
    assert sent == {"max_concurrency": 1, "request_timeout_seconds": limits.request_timeout_seconds, "max_retries": limits.max_retries,
                    "retry_backoff_seconds": limits.retry_backoff_seconds, "retry_backoff_max_seconds": limits.retry_backoff_max_seconds}


def test_next_is_off_until_the_connection_can_be_registered(ctx) -> None:
    """Off while the id is not a valid id, a required served field is blank, or a limit is wrong. ``disabled`` (not the in-flight ``busy``) says so."""
    _open(ctx)
    assert ctx.eval("disabled('pc-register-next')") is True, "nothing typed"
    _type(ctx, "pc-field:id", "a/b")
    _type(ctx, "pc-field:url", URL)
    assert ctx.eval("disabled('pc-register-next')") is True, "'a/b' is not an id the server can address"
    _type(ctx, "pc-field:id", "lm")
    assert ctx.eval("disabled('pc-register-next')") is False
    _type(ctx, "pc-field:url", "   ")
    assert ctx.eval("disabled('pc-register-next')") is True, "a blank URL is blank"
    _type(ctx, "pc-field:url", URL)
    _type(ctx, "pc-limit:max_concurrency", "0")
    assert ctx.eval("disabled('pc-register-next')") is True and "whole number of 1 or more" in ctx.eval("textOf(MR.find('pc-limits'))")
    _type(ctx, "pc-limit:max_concurrency", "4")
    assert ctx.eval("disabled('pc-register-next')") is False
    _type(ctx, "pc-limit:retry_backoff_seconds", "")
    assert ctx.eval("disabled('pc-register-next')") is True, "a blank backoff is Required"
    _type(ctx, "pc-limit:retry_backoff_seconds", "1.5")
    _type(ctx, "pc-field:type", "openrouter")
    assert ctx.eval("disabled('pc-register-next')") is True, "OpenRouter requires its key"
    _type(ctx, "pc-field:api_key", "sk-or-1")
    assert ctx.eval("disabled('pc-register-next')") is False


def test_next_sends_the_draft_to_discover_models(ctx) -> None:
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": _models("m")})
    _fill(ctx)
    assert _press(ctx, "pc-register-next") == "fulfilled"
    (call,) = _calls(ctx)
    assert call == {"method": "POST", "path": "/llm_providers/_discover_models", "body": {"provider": "openchat", "config": {"url": URL, "flavor": "other"}}}
    assert "api_key" not in call["body"]["config"], "a blank optional key is omitted, never sent as an empty string (ticket T3)"


def test_next_sends_a_typed_key_and_nothing_the_probe_does_not_take(ctx) -> None:
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": _models("m")})
    _fill(ctx, key="sk-1")
    _press(ctx, "pc-register-next")
    (call,) = _calls(ctx)
    assert call["body"] == {"provider": "openchat", "config": {"url": URL, "flavor": "other", "api_key": "sk-1"}}, "the body of the probe is {provider, config}: no id, no limits"


def test_a_failed_discovery_stays_on_page_one_and_offers_register_without_models(ctx, envelopes) -> None:
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": [{"refuse": envelopes["probe_failed"]}, _models("m1")]})
    _fill(ctx)
    assert ctx.eval("MR.find('pc-register-without-models') === null") is True, "not offered before anything has failed"
    assert _press(ctx, "pc-register-next") == "fulfilled", "the click handler's promise never rejects"
    assert ctx.eval("MR.find('pc-llm-settings') !== null && MR.find('pc-llm-models') === null") is True, "still page one"
    reason = ctx.eval(f"window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(envelopes['probe_failed'])})).message")
    assert "All connection attempts failed" in reason
    assert _error(ctx) == reason
    assert ctx.eval("MR.find('pc-register-error').props.role") == "alert"
    left_to_right = js(ctx, "testids('pc-register')")
    assert left_to_right.index("pc-register-without-models") < left_to_right.index("pc-register-next"), "the offer sits before Next"
    assert ctx.eval("MR.find('pc-register-cancel') !== null") is True
    # Next retries
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert ctx.eval("MR.find('pc-llm-models') !== null") is True
    assert _error(ctx) is None


def test_an_empty_model_list_stays_on_page_one(ctx) -> None:
    """Review Focus 5: a server that answers but lists nothing leaves the operator on page one, with the reason, and the way to register without models."""
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": {"answer": {"models": []}}})
    _fill(ctx)
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert ctx.eval("MR.find('pc-llm-models') === null") is True
    assert _error(ctx) == "The provider answered but listed no models."
    assert ctx.eval("MR.find('pc-register-without-models') !== null") is True


def test_register_without_models_posts_only_the_provider(ctx, envelopes) -> None:
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": {"refuse": envelopes["probe_failed"]}})
    _fill(ctx, key="sk-1")
    _press(ctx, "pc-register-next")
    assert _press(ctx, "pc-register-without-models") == "fulfilled"
    assert _requests(ctx) == [("POST", "/llm_providers/_discover_models"), ("POST", "/llm_providers")], "no profile, no second provider"
    body = _calls(ctx, "POST", "/llm_providers")[0]["body"]
    assert body == {
        "id": "lm", "provider": "openchat", "config": {"url": URL, "flavor": "other", "api_key": "sk-1"},
        "limits": {"max_concurrency": 1, "request_timeout_seconds": 300, "max_retries": 2, "retry_backoff_seconds": 0.5, "retry_backoff_max_seconds": 8},
    }
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Provider registered", "detail": "lm"}]
    assert js(ctx, "SAVED") == [body] and ctx.eval("CLOSED") == 1


def test_register_without_models_is_refused_in_the_open(ctx, envelopes) -> None:
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": {"refuse": envelopes["probe_failed"]}, "POST /llm_providers": {"refuse": envelopes["provider_taken"]}})
    _fill(ctx)
    _press(ctx, "pc-register-next")
    assert _press(ctx, "pc-register-without-models") == "fulfilled"
    assert "already exists" in _error(ctx)
    assert ctx.eval("CLOSED") == 0 and js(ctx, "SAVED") == [] and js(ctx, "TOASTS") == []
    assert ctx.eval("ariaBusy('pc-register-without-models')") is False, "and it can be pressed again"


# ---- page two ----------------------------------------------------------------------------------------------------------------------------------------------


def test_page_two_prefills_context_windows_and_counts_ticks(ctx) -> None:
    models = [{"name": "alpha", "context_length": 8192}, "beta", {"name": "gamma", "context_length": 32768}]
    _to_page_two(ctx, models)
    assert "Models found at lm" in js(ctx, "MR.texts()")
    assert "✓ Connected; 3 models listed" in js(ctx, "MR.texts()")
    assert ctx.eval("textOf(MR.find('pc-import-head'))") == "Models found at lm✓ Connected; 3 models listed"
    labels = js(ctx, "ALL('pc-import').filter(function (n) { return n.type === 'label'; }).map(function (n) { return textOf(n); })")
    assert labels == ["Select all", "alpha", "beta", "gamma"], "the server's order"
    assert "Context window" in ctx.eval("textOf(MR.find('pc-import'))")
    assert [ctx.eval(f"value('pc-import-context:{n}')") for n in ("alpha", "beta", "gamma")] == ["8192", "", "32768"]
    assert [ctx.eval(f"MR.find('pc-import:{n}').props.checked") for n in ("alpha", "beta", "gamma")] == [False, False, False], "nothing is ticked"
    assert ctx.eval("text('pc-register-submit')") == "Import 0 models" and ctx.eval("disabled('pc-register-submit')") is True
    _tick(ctx, "alpha", "gamma")
    assert ctx.eval("text('pc-register-submit')") == "Import 2 models" and ctx.eval("disabled('pc-register-submit')") is False
    _tick(ctx, "gamma")
    assert ctx.eval("text('pc-register-submit')") == "Import 1 model"
    assert "named <provider>--<model>, editable later" in ctx.eval("text('pc-import-help')")
    assert ctx.eval("MR.find('pc-register-back') !== null") is True and ctx.eval("text('pc-register-back')") == "‹ Back"


def test_one_model_is_said_in_the_singular(ctx) -> None:
    _to_page_two(ctx, [{"name": "only", "context_length": 100}])
    assert "✓ Connected; 1 model listed" in js(ctx, "MR.texts()")


def test_select_all_ticks_every_model_that_can_be_ticked(ctx) -> None:
    _to_page_two(ctx, ["a", "b", "///"])
    ctx.eval("MR.find('pc-import:all').props.onChange({}); MR.rerender();")
    assert [ctx.eval(f"MR.find('pc-import:{n}').props.checked") for n in ("a", "b", "///")] == [True, True, False], "a name no id can be made from is left out"


def test_import_is_off_until_every_ticked_row_has_a_context_window(ctx) -> None:
    """Review Focus 5: Anthropic and real OpenAI report no ``context_length``. Import stays off until every ticked row has a whole number of 1 or more, and the row says so."""
    _to_page_two(ctx, [{"name": "known", "context_length": 8192}, "unknown"])
    _tick(ctx, "known", "unknown")
    assert ctx.eval("disabled('pc-register-submit')") is True
    assert _status(ctx, "unknown") == "Required" and _status(ctx, "known") == ""
    assert ctx.eval("MR.find('pc-import-context:unknown').props['aria-invalid']") == "true"
    for bad in ("0", "-5", "12.5", "1e3", "abc"):
        _type(ctx, "pc-import-context:unknown", bad)
        assert ctx.eval("disabled('pc-register-submit')") is True, bad
        assert _status(ctx, "unknown") == "Enter a whole number of 1 or more", bad
    _type(ctx, "pc-import-context:unknown", "4096")
    assert ctx.eval("disabled('pc-register-submit')") is False and _status(ctx, "unknown") == ""
    _tick(ctx, "unknown")
    assert _status(ctx, "unknown") == "", "a row that is not ticked has nothing to say about its window"
    _type(ctx, "pc-import-context:unknown", "")
    assert ctx.eval("disabled('pc-register-submit')") is False, "only the ticked rows matter"


def test_a_model_listed_twice_is_one_row(ctx) -> None:
    _to_page_two(ctx, [{"name": "dup", "context_length": 10}, "dup", {"name": "other", "context_length": 20}])
    assert js(ctx, "ALL('pc-import').filter(function (n) { return n.type === 'label' && textOf(n) !== 'Select all'; }).map(textOf)") == ["dup", "other"]
    assert "✓ Connected; 2 models listed" in js(ctx, "MR.texts()")


def test_a_failed_listing_is_forgotten_when_the_kind_changes(ctx, envelopes) -> None:
    """The offer to register without models is about the connection that failed; another kind is another connection."""
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": {"refuse": envelopes["probe_failed"]}})
    _fill(ctx)
    _press(ctx, "pc-register-next")
    assert ctx.eval("MR.find('pc-register-without-models') !== null") is True and _error(ctx)
    _type(ctx, "pc-field:type", "ollama")
    assert ctx.eval("MR.find('pc-register-without-models') === null") is True and _error(ctx) is None


def test_when_every_profile_already_exists_the_import_is_done(ctx, envelopes) -> None:
    """A 409 is a profile that is there, not a failure: nothing is left to retry, so the overlay closes with the provider."""
    _to_page_two(ctx, [{"name": "a", "context_length": 1}, {"name": "b", "context_length": 2}])
    _tick(ctx, "a", "b")
    _route(ctx, {
        "POST /model_profiles": {"refuse": envelopes["profile_taken"]},
        "GET /model_profiles/lm--a": {"answer": {"provider_id": "lm", "model_name": "a"}},
        "GET /model_profiles/lm--b": {"answer": {"provider_id": "lm", "model_name": "b"}},
    })
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert ctx.eval("CLOSED") == 1 and len(js(ctx, "SAVED")) == 1
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Provider registered", "detail": "lm: 2 profiles imported"}]


def test_a_conflicting_profile_id_is_verified_before_calling_it_imported(ctx, envelopes) -> None:
    """A 409 can belong to another provider/model pair whose normalized id
    collides with this row. Only the same pair counts as already imported."""
    ctx.eval(f"var PROFILE_TAKEN = {json.dumps(envelopes['profile_taken'])};")
    ctx.eval("""
    var RESULT = null;
    PC_importProfiles({
      apiFetch: function (method, path, body) {
        if (method === "POST") return refuse(PROFILE_TAKEN);
        if (method === "GET" && path === "/model_profiles/lm--llama3.1-8b") {
          return Promise.resolve({ id: "lm--llama3.1-8b", provider_id: "other", model_name: "llama3.1:8b" });
        }
        return Promise.reject(new Error("unexpected request"));
      },
      providerId: "lm",
      rows: [{ name: "llama3.1-8b", ticked: true, context: "8192", status: "pending" }]
    }).then(function (rows) { RESULT = rows; });
    void 0;
    """)
    ctx.eval("void 0;")
    result = js(ctx, "RESULT")
    assert result[0]["status"] == "failed"
    assert result[0]["error"] == "the id is taken by another profile"


def test_a_name_with_no_id_cannot_be_ticked(ctx) -> None:
    _to_page_two(ctx, ["///", {"name": "ok", "context_length": 10}])
    assert _status(ctx, "///") == "No id can be made from this name"
    assert ctx.eval("MR.find('pc-import:///').props.disabled") is True
    ctx.eval("MR.find('pc-import:///').props.onChange({}); MR.rerender();")
    assert ctx.eval("MR.find('pc-import:///').props.checked") is False


def test_two_names_that_make_one_id_cannot_both_be_imported(ctx) -> None:
    """``a/b`` and ``a-b`` are ONE profile id. The second would answer 409 and be read as already imported, which it is not: it cannot be ticked."""
    _to_page_two(ctx, [{"name": "a/b", "context_length": 10}, {"name": "a-b", "context_length": 10}])
    assert ctx.eval("MR.find('pc-import:a/b').props.disabled") is False
    assert ctx.eval("MR.find('pc-import:a-b').props.disabled") is True
    assert "same as the one for a/b" in _status(ctx, "a-b")


def test_back_returns_to_page_one_with_the_draft_and_keeps_the_list_for_the_same_connection(ctx) -> None:
    _to_page_two(ctx, [{"name": "a", "context_length": 100}, {"name": "b", "context_length": 200}], key="sk-1")
    _tick(ctx, "b")
    assert ctx.eval("MR.click('pc-register-back'); MR.find('pc-llm-settings') !== null") is True
    assert ctx.eval("value('pc-field:id')") == "lm" and ctx.eval("value('pc-field:url')") == URL and ctx.eval("value('pc-field:api_key')") == "sk-1"
    _type(ctx, "pc-limit:max_concurrency", "3")
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert len(_calls(ctx, "POST", "/llm_providers/_discover_models")) == 1, "the same connection is not asked again"
    assert ctx.eval("MR.find('pc-import:b').props.checked") is True and ctx.eval("MR.find('pc-import:a').props.checked") is False, "and what was ticked is kept"
    ctx.eval("MR.click('pc-register-back')")
    _type(ctx, "pc-field:url", "http://other.test/v1")
    _route(ctx, {"POST /llm_providers/_discover_models": _models({"name": "z", "context_length": 5})})
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert len(_calls(ctx, "POST", "/llm_providers/_discover_models")) == 2, "another connection is asked"
    assert js(ctx, "ALL('pc-import').filter(function (n) { return n.type === 'label' && textOf(n) !== 'Select all'; }).map(textOf)") == ["z"]


# ---- the import --------------------------------------------------------------------------------------------------------------------------------------------


def test_import_posts_the_sanitised_profile_id(ctx) -> None:
    """Review Focus 3: ``anthropic/claude-3.5:beta`` is a real OpenRouter name and carries the two characters the id rule refuses. The profile's id is the provider's and the
    model's parts joined with ``--``, and the row is addressable."""
    _open(ctx)
    _type(ctx, "pc-field:type", "openrouter")
    _route(ctx, {"POST /llm_providers/_discover_models": _models({"name": "anthropic/claude-3.5:beta", "context_length": 200000})})
    _type(ctx, "pc-field:id", "or")
    _type(ctx, "pc-field:api_key", "sk-or-1")
    _press(ctx, "pc-register-next")
    _tick(ctx, "anthropic/claude-3.5:beta")
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert _requests(ctx) == [("POST", "/llm_providers/_discover_models"), ("POST", "/llm_providers"), ("POST", "/model_profiles")]
    (profile,) = _calls(ctx, "POST", "/model_profiles")
    assert profile["body"] == {
        "id": "or--anthropic-claude-3.5-beta", "provider_id": "or", "model_name": "anthropic/claude-3.5:beta", "context_length": 200000,
        "description": "Imported from or",
    }
    assert isinstance(profile["body"]["context_length"], int) and not isinstance(profile["body"]["context_length"], bool)
    from primer.model.model_profile import ModelProfile

    made = ModelProfile.model_validate(profile["body"])
    assert made.id == "or--anthropic-claude-3.5-beta", "and the server takes it as a profile"
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Provider registered", "detail": "or: 1 profile imported"}]
    assert js(ctx, "SAVED")[0]["id"] == "or" and ctx.eval("CLOSED") == 1


def test_import_creates_the_provider_then_every_profile_one_after_another(ctx) -> None:
    """The provider first, once; then the profiles in the order of the list, each posted when the one before it has answered (a hold on the first stops the second)."""
    _to_page_two(ctx, [{"name": "a", "context_length": 100}, {"name": "b", "context_length": 200}, {"name": "c", "context_length": 300}])
    _tick(ctx, "a", "b", "c")
    _route(ctx, {"POST /model_profiles": [{"hold": "first"}, {"answer": {"id": "x"}}]})
    ctx.eval("press('pc-register-submit')")
    settle(ctx)
    posted = [(c["method"], c["path"], (c["body"] or {}).get("model_name")) for c in js(ctx, "CALLS")][1:]
    assert posted == [("POST", "/llm_providers", None), ("POST", "/model_profiles", "a")], "b waits for a"
    ctx.eval("HELD.first.ok({ id: 'lm--a' }); void 0;")
    settle(ctx)
    names = [c["body"]["model_name"] for c in _calls(ctx, "POST", "/model_profiles")]
    assert names == ["a", "b", "c"]
    assert [c["path"] for c in _calls(ctx, "POST")].count("/llm_providers") == 1
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Provider registered", "detail": "lm: 3 profiles imported"}]
    assert ctx.eval("CLOSED") == 1 and len(js(ctx, "SAVED")) == 1


def test_each_row_shows_its_result_as_it_is_answered(ctx, envelopes) -> None:
    _to_page_two(ctx, [{"name": "a", "context_length": 100}, {"name": "b", "context_length": 200}])
    _tick(ctx, "a", "b")
    _route(ctx, {"POST /model_profiles": [{"answer": {"id": "x"}}, {"hold": "second"}]})
    ctx.eval("press('pc-register-submit')")
    settle(ctx)
    assert _status(ctx, "a") == "imported", "a is shown done while b is still out"
    assert ctx.eval("MR.find('pc-import:a').props.disabled") is True
    assert ctx.eval("MR.find('pc-import:b').props.disabled") is True, "nothing in the table can change while the import runs"
    assert ctx.eval("MR.find('pc-import-context:b').props.readOnly") is True
    assert ctx.eval("text('pc-register-submit')") == "Import 2 models" and ctx.eval("ariaBusy('pc-register-submit')") is True, "still the button that was pressed, busy"
    assert ctx.eval("MR.find('pc-register-retry-failed') === null") is True, "it says Retry failed when a round has ended, not while the first one runs"
    assert ctx.eval("MR.find('pc-register-close') !== null && MR.find('pc-register-back') === null") is True, "the provider exists: Close, not Back"
    assert ctx.eval("ariaDisabled('pc-register-close')") is True, "and Close waits for the round"
    ctx.eval("HELD.second.no(new window.primerApi.ApiError(" + json.dumps(envelopes["profile_broke"]) + ")); void 0;")
    settle(ctx)
    assert _status(ctx, "b").startswith("failed: ")
    assert ctx.eval("text('pc-register-retry-failed')") == "Retry failed" and ctx.eval("ariaDisabled('pc-register-close')") is False


def test_partial_import_keeps_the_overlay_open_and_retries_only_failed_rows(ctx, envelopes) -> None:
    """Master Review Focus 4. The provider is created, one profile already exists (409) and one profile creation fails (500): the 409 row reads "already imported", the
    failed row reads "failed" with its reason, the overlay stays open with "Retry failed", Retry posts only the failed row, and the provider is never posted twice."""
    _to_page_two(ctx, [{"name": "a", "context_length": 100}, {"name": "b", "context_length": 200}, {"name": "c", "context_length": 300}])
    _tick(ctx, "a", "b", "c")
    answers = {
        "a": {"answer": {"id": "lm--a"}},
        "b": {"refuse": envelopes["profile_taken"]},
        "c": {"refuse": envelopes["profile_broke"]},
    }
    ctx.eval(
        "HANDLER = (function (inner) { return function (method, path, body) {"
        " if (method === 'GET' && path === '/model_profiles/lm--b') return Promise.resolve({ provider_id: 'lm', model_name: 'b' });"
        " if (method === 'POST' && path === '/model_profiles') { var answers = " + json.dumps(answers) + "; var r = answers[body.model_name];"
        " if (r.refuse) return refuse(r.refuse); return Promise.resolve(r.answer); }"
        " return inner(method, path, body); }; })(HANDLER);"
    )
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert _status(ctx, "a") == "imported" and _status(ctx, "b") == "already imported"
    broke = ctx.eval(f"window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(envelopes['profile_broke'])})).message")
    assert _status(ctx, "c") == "failed: " + broke
    assert ctx.eval("MR.find('pc-import-context:c').props['aria-invalid'] === undefined") is True, (
        "a request failure is not a problem with the context field"
    )
    assert ctx.eval("CLOSED") == 0 and js(ctx, "SAVED") == [] and js(ctx, "TOASTS") == [], "still open, nothing announced as done"
    assert ctx.eval("MR.find('pc-register-retry-failed') !== null && MR.find('pc-register-submit') === null") is True
    assert ctx.eval("text('pc-register-retry-failed')") == "Retry failed"
    assert ctx.eval("MR.find('pc-register-close') !== null") is True
    assert ctx.eval("MR.find('pc-register-back') === null") is True, "the provider exists: there is no going back to create it again"
    assert "1 profile could not be created" in ctx.eval("text('pc-import-summary')")
    posted_before = len(_calls(ctx, "POST", "/model_profiles"))
    assert posted_before == 3
    # the retry: c answers 201 this time
    answers["c"] = {"answer": {"id": "lm--c"}}
    ctx.eval(
        "HANDLER = (function (inner) { return function (method, path, body) {"
        " if (method === 'POST' && path === '/model_profiles') return Promise.resolve({ id: 'lm--c' });"
        " return inner(method, path, body); }; })(HANDLER);"
    )
    assert _press(ctx, "pc-register-retry-failed") == "fulfilled"
    retried = _calls(ctx, "POST", "/model_profiles")[posted_before:]
    assert [c["body"]["model_name"] for c in retried] == ["c"], "only the failed row is posted again"
    assert len(_calls(ctx, "POST", "/llm_providers")) == 1, "the provider is posted once, ever"
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Provider registered", "detail": "lm: 3 profiles imported"}]
    assert len(js(ctx, "SAVED")) == 1 and ctx.eval("CLOSED") == 1, "onSaved once, and the overlay closes once"


def test_close_after_a_partial_import_finishes_with_the_provider_that_exists(ctx, envelopes) -> None:
    _to_page_two(ctx, [{"name": "a", "context_length": 100}, {"name": "b", "context_length": 200}])
    _tick(ctx, "a", "b")
    _route(ctx, {"POST /model_profiles": [{"answer": {"id": "lm--a"}}, {"refuse": envelopes["profile_broke"]}]})
    _press(ctx, "pc-register-submit")
    assert ctx.eval("CLOSED") == 0
    ctx.eval("MR.click('pc-register-close')")
    assert js(ctx, "SAVED")[0]["id"] == "lm", "the caller hears the provider: it exists"
    assert ctx.eval("CLOSED") == 1, "closed once (onSaved closes the overlay)"
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Provider registered", "detail": "lm: 1 of 2 profiles imported"}]


def test_a_failed_row_can_have_its_context_window_corrected_before_the_retry(ctx, envelopes) -> None:
    _to_page_two(ctx, [{"name": "a", "context_length": 100}, {"name": "b", "context_length": 200}])
    _tick(ctx, "a", "b")
    _route(ctx, {"POST /model_profiles": [{"answer": {"id": "lm--a"}}, {"refuse": envelopes["profile_broke"]}, {"answer": {"id": "lm--b"}}]})
    _press(ctx, "pc-register-submit")
    assert ctx.eval("MR.find('pc-import-context:b').props.readOnly") is False and ctx.eval("MR.find('pc-import:b').props.disabled") is False
    assert ctx.eval("MR.find('pc-import-context:a').props.readOnly") is True and ctx.eval("MR.find('pc-import:a').props.disabled") is True
    _type(ctx, "pc-import-context:b", "")
    assert ctx.eval("disabled('pc-register-retry-failed')") is True and _status(ctx, "b").startswith("failed: ") or _status(ctx, "b") == "failed"
    _type(ctx, "pc-import-context:b", "4096")
    assert _press(ctx, "pc-register-retry-failed") == "fulfilled"
    assert _calls(ctx, "POST", "/model_profiles")[-1]["body"]["context_length"] == 4096


def test_a_failed_row_left_out_of_the_retry_is_not_posted(ctx, envelopes) -> None:
    _to_page_two(ctx, [{"name": "a", "context_length": 100}, {"name": "b", "context_length": 200}, {"name": "c", "context_length": 300}])
    _tick(ctx, "a", "b", "c")
    _route(ctx, {"POST /model_profiles": [{"answer": {"id": "x"}}, {"refuse": envelopes["profile_broke"]}, {"refuse": envelopes["profile_broke"]}, {"answer": {"id": "y"}}]})
    _press(ctx, "pc-register-submit")
    _tick(ctx, "b")
    assert ctx.eval("disabled('pc-register-retry-failed')") is False
    _press(ctx, "pc-register-retry-failed")
    assert [c["body"]["model_name"] for c in _calls(ctx, "POST", "/model_profiles")] == ["a", "b", "c", "c"], "b was left out of the retry"
    assert ctx.eval("CLOSED") == 1, "every ticked row is done"


def test_a_second_click_while_importing_posts_nothing_more(ctx) -> None:
    _to_page_two(ctx, [{"name": "a", "context_length": 100}, {"name": "b", "context_length": 200}])
    _tick(ctx, "a", "b")
    _route(ctx, {"POST /llm_providers": {"hold": "provider"}, "POST /model_profiles": [{"hold": "first"}, {"hold": "second"}]})
    ctx.eval("press('pc-register-submit')")
    settle(ctx)
    assert ctx.eval("ariaBusy('pc-register-submit')") is True, "the button says its request is out, and keeps its focus"
    for _ in range(2):
        ctx.eval("press('pc-register-submit')")
        settle(ctx)
    assert len(_calls(ctx, "POST", "/llm_providers")) == 1, "nor while the provider is out"
    ctx.eval("HELD.provider.ok({ id: 'lm' }); void 0;")
    settle(ctx)
    assert len(_calls(ctx, "POST", "/model_profiles")) == 1
    ctx.eval("press('pc-register-submit')")
    settle(ctx)
    assert len(_calls(ctx, "POST", "/model_profiles")) == 1 and len(_calls(ctx, "POST", "/llm_providers")) == 1, "nor while a profile is out"
    ctx.eval("HELD.first.ok({}); void 0;")
    settle(ctx)
    ctx.eval("HELD.second.ok({}); void 0;")
    settle(ctx)
    assert len(_calls(ctx, "POST", "/llm_providers")) == 1 and len(_calls(ctx, "POST", "/model_profiles")) == 2
    assert ctx.eval("CLOSED") == 1


def test_a_refused_provider_create_posts_no_profiles(ctx, envelopes) -> None:
    """Nothing but the provider is posted when the provider is refused; the refusal is on page two, where the operator is; Back reaches the id, and a fixed attempt goes on."""
    _to_page_two(ctx, [{"name": "a", "context_length": 100}])
    _tick(ctx, "a")
    _route(ctx, {"POST /llm_providers": [{"refuse": envelopes["provider_taken"]}, {"answer": {"id": "lm-2"}}]})
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert _calls(ctx, "POST", "/model_profiles") == []
    assert "already exists" in _error(ctx) and ctx.eval("MR.find('pc-llm-models') !== null") is True
    assert ctx.eval("CLOSED") == 0 and js(ctx, "TOASTS") == []
    assert ctx.eval("MR.find('pc-register-back') !== null") is True, "the provider was not created: Back is still there"
    ctx.eval("MR.click('pc-register-back')")
    _type(ctx, "pc-field:id", "lm-2")
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert ctx.eval("MR.find('pc-import:a').props.checked") is True, "the list and the tick were kept"
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert len(_calls(ctx, "POST", "/llm_providers")) == 2
    (profile,) = _calls(ctx, "POST", "/model_profiles")
    assert profile["body"]["id"] == "lm-2--a" and profile["body"]["provider_id"] == "lm-2", "the profile names the provider that exists"


def test_the_import_stops_when_the_overlay_is_closed(ctx) -> None:
    """Closing the overlay while the import runs is the operator's choice: the profiles not yet posted are not posted behind their back."""
    _to_page_two(ctx, [{"name": "a", "context_length": 100}, {"name": "b", "context_length": 200}])
    _tick(ctx, "a", "b")
    _route(ctx, {"POST /model_profiles": [{"hold": "first"}, {"answer": {"id": "x"}}]})
    ctx.eval("press('pc-register-submit')")
    settle(ctx)
    ctx.eval("MR.mount(function Closed() { return null; }, {});")
    ctx.eval("HELD.first.ok({}); void 0;")
    settle(ctx)
    assert [c["body"]["model_name"] for c in _calls(ctx, "POST", "/model_profiles")] == ["a"]
    assert js(ctx, "SAVED") == [] and js(ctx, "TOASTS") == []


# ---- editing -----------------------------------------------------------------------------------------------------------------------------------------------


def _openrouter_row() -> dict:
    """An OpenRouter provider as the server serves it: the key masked, the attribution fields set, all seven limits, and the probe stamps the page never shows."""
    from primer.model.providers.llm import LLMProvider

    row = LLMProvider.model_validate({
        "id": "or-main", "provider": "openrouter",
        "config": {"api_key": "sk-or-v1-abcdef1234", "app_name": "primer-prod", "app_url": "https://primer.example"},
        "limits": {"max_concurrency": 2, "request_timeout_seconds": 120.0, "connect_timeout_seconds": 5.0, "total_timeout_seconds": 600.0,
                   "max_retries": 3, "retry_backoff_seconds": 1.0, "retry_backoff_max_seconds": 16.0},
        "last_probe_at": "2026-10-18T10:00:00Z", "last_probe_ok": False, "last_error": "OpenRouter discover failed: HTTP 401",
    }).model_dump(mode="json")
    assert row["config"]["api_key"] != "sk-or-v1-abcdef1234", "the served row masks its key"
    assert set(row["limits"]) == {"max_concurrency", "request_timeout_seconds", "connect_timeout_seconds", "total_timeout_seconds", "max_retries",
                                  "retry_backoff_seconds", "retry_backoff_max_seconds"}
    return row


def _openchat_row(provider_id: str = "p", key: str | None = "sk-local-abcd1234") -> dict:
    from primer.model.providers.llm import LLMProvider

    config = {"url": URL, "flavor": "vllm"}
    if key:
        config["api_key"] = key
    return LLMProvider.model_validate({"id": provider_id, "provider": "openchat", "config": config, "limits": {"max_concurrency": 3}}).model_dump(mode="json")


def test_edit_round_trips_every_unshown_field(ctx) -> None:
    """Master Review Focus 1. The page shows some of a row; the PUT carries all of it: the masked key as served, the attribution fields, the probe stamps, a field no
    version of the page knows (kept, not dropped), and every limit; only what the operator changed differs."""
    row = _openrouter_row()
    row["x_future"] = {"kept": True}
    row["config"]["x_future_knob"] = 7
    _open(ctx, editRow=row)
    assert ctx.eval("textOf(MR.find('pc-register-submit'))") == "Save"
    _type(ctx, "pc-limit:max_concurrency", "4")
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    (call,) = js(ctx, "CALLS")
    expected = json.loads(json.dumps(row))
    expected["limits"]["max_concurrency"] = 4
    assert call == {"method": "PUT", "path": "/llm_providers/or-main", "body": expected}
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Provider saved", "detail": "or-main"}]
    assert js(ctx, "SAVED") == [expected] and ctx.eval("CLOSED") == 1


def test_edit_shows_the_row_and_sends_the_row_back_untouched(ctx) -> None:
    """Pressing Save with nothing changed sends the served row as it was: not one field is added, dropped or turned into text."""
    row = _openrouter_row()
    _open(ctx, editRow=row)
    assert ctx.eval("value('pc-field:app_name')") == "primer-prod" and ctx.eval("value('pc-field:app_url')") == row["config"]["app_url"]
    assert [ctx.eval(f"value('pc-limit:{f}')") for f in ("max_concurrency", "request_timeout_seconds", "max_retries", "retry_backoff_seconds")] == ["2", "120", "3", "1"]
    ctx.eval("MR.click('pc-limits-more:toggle')")
    assert [ctx.eval(f"value('pc-limit:{f}')") for f in ("connect_timeout_seconds", "total_timeout_seconds", "retry_backoff_max_seconds")] == ["5", "600", "16"]
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert _calls(ctx)[0]["body"] == row


def test_edit_keeps_the_masked_key_as_served(ctx) -> None:
    row = _openchat_row()
    mask = row["config"]["api_key"]
    assert mask.startswith("**********") and mask != "sk-local-abcd1234"
    _open(ctx, editRow=row)
    assert ctx.eval("value('pc-field:api_key')") == mask, "the box shows the mask as served"
    assert "Stored secrets are shown masked; leave it as is to keep it." in _help(ctx, "pc-field:api_key")
    _press(ctx, "pc-register-submit")
    assert _calls(ctx)[0]["body"]["config"]["api_key"] == mask, "the server restores the stored key from its own mask"
    _type(ctx, "pc-field:api_key", "sk-new")
    _press(ctx, "pc-register-submit")
    assert _calls(ctx)[1]["body"]["config"]["api_key"] == "sk-new"
    _type(ctx, "pc-field:api_key", "")
    _press(ctx, "pc-register-submit")
    assert "api_key" not in _calls(ctx)[2]["body"]["config"], "a cleared optional key is left out (ticket T3), never sent as an empty string"


def test_edit_has_no_next_and_locks_the_id_and_the_type(ctx) -> None:
    """Review Focus 4: in edit mode there is nothing to probe with a draft. The id and the type are the row's, and the footer is Cancel, Import more models, Delete and Save."""
    row = _openchat_row()
    _open(ctx, editRow=row)
    assert ctx.eval("MR.find('pc-register-next') === null && MR.find('pc-register-without-models') === null") is True
    assert ctx.eval("MR.find('pc-field:id').props.readOnly") is True and ctx.eval("value('pc-field:id')") == "p"
    assert ctx.eval("MR.find('pc-field:type').props.disabled") is True and ctx.eval("value('pc-field:type')") == "openchat"
    assert js(ctx, "options('pc-field:type')") == ["openchat"], "the select names the row's own kind"
    assert "Edit p" in js(ctx, "MR.texts()")
    footer = js(ctx, "testids('pc-register')")
    order = [t for t in footer if t in ("pc-register-cancel", "pc-register-import-more", "pc-register-delete", "pc-register-submit")]
    assert order == ["pc-register-cancel", "pc-register-import-more", "pc-register-delete", "pc-register-submit"]
    assert ctx.eval("MR.find('pc-field:flavor') !== null") and ctx.eval("value('pc-field:flavor')") == "vllm"
    ctx.eval("MR.click('pc-register-cancel')")
    assert ctx.eval("CLOSED") == 1 and _requests(ctx) == []


def test_a_row_of_a_kind_the_install_does_not_serve_cannot_be_saved(ctx) -> None:
    _open(ctx, editRow={"id": "odd", "provider": "no-such-kind", "config": {}, "limits": {"max_concurrency": 1}})
    assert "does not serve the kind" in ctx.eval("textOf(MR.find('pc-register-kind-missing'))")
    assert ctx.eval("disabled('pc-register-submit')") is True


def test_import_more_uses_the_saved_row_route_never_the_draft_route(ctx) -> None:
    """Review Focus 4: the draft of an edit holds the MASK of the key, and a probe that replayed it would authenticate with ``**********``. The SAVED provider is probed
    (the server reads its stored secret); the draft route is never called, and nothing is written."""
    _open(ctx, editRow=_openchat_row())
    _route(ctx, {
        "GET /llm_providers/p/discovered_models": {"answer": {"models": [{"name": "a", "context_length": 100}, {"name": "b"}]}},
        "GET /llm_providers/p/models": {"answer": {"models": ["a"]}},
    })
    assert _press(ctx, "pc-register-import-more") == "fulfilled"
    reached = _requests(ctx)
    assert {("GET", "/llm_providers/p/discovered_models"), ("GET", "/llm_providers/p/models")} <= set(reached)
    assert {method for method, _ in reached} == {"GET"}, "it reads; it writes nothing"
    assert not any(path.endswith("_discover_models") for _, path in reached), "no draft is ever probed"
    assert ctx.eval("MR.find('pc-llm-models') !== null") is True
    assert "Models found at p" in js(ctx, "MR.texts()") and "✓ Connected; 2 models listed" in js(ctx, "MR.texts()")


def test_import_more_lists_only_models_without_a_profile(ctx) -> None:
    _open(ctx, editRow=_openchat_row())
    _route(ctx, {
        "GET /llm_providers/p/discovered_models": {"answer": {"models": [{"name": "a", "context_length": 100}, {"name": "b", "context_length": 200}, "c"]}},
        "GET /llm_providers/p/models": {"answer": {"models": ["a", "no-longer-listed"]}},
    })
    _press(ctx, "pc-register-import-more")
    assert js(ctx, "ALL('pc-import').filter(function (n) { return n.type === 'label' && textOf(n) !== 'Select all'; }).map(textOf)") == ["b", "c"]
    assert ctx.eval("text('pc-import-already')") == "Already imported: 1", "the listed models that already have a profile (a profile of a model the provider no longer lists is not counted)"
    assert [ctx.eval(f"MR.find('pc-import:{n}').props.checked") for n in ("b", "c")] == [False, False]
    assert ctx.eval("MR.find('pc-register-back') !== null") is True
    # import b: the profiles only, the provider is not touched, and the overlay says what was imported
    _tick(ctx, "b")
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert [(m, p) for m, p in _requests(ctx) if m != "GET"] == [("POST", "/model_profiles")], "no PUT, no POST of the provider"
    (profile,) = _calls(ctx, "POST", "/model_profiles")
    assert profile["body"] == {"id": "p--b", "provider_id": "p", "model_name": "b", "context_length": 200, "description": "Imported from p"}
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Profiles imported", "detail": "p: 1 profile imported"}]
    assert js(ctx, "SAVED")[0]["id"] == "p" and ctx.eval("CLOSED") == 1


def test_import_more_with_everything_already_imported_says_so(ctx) -> None:
    _open(ctx, editRow=_openchat_row())
    _route(ctx, {"GET /llm_providers/p/discovered_models": _models("a", "b"), "GET /llm_providers/p/models": {"answer": {"models": ["a", "b"]}}})
    _press(ctx, "pc-register-import-more")
    assert ctx.eval("text('pc-import-already')") == "Already imported: 2"
    assert "already has a profile" in ctx.eval("text('pc-import-none')")
    assert ctx.eval("disabled('pc-register-submit')") is True and ctx.eval("text('pc-register-submit')") == "Import 0 models"


def test_import_more_that_ends_in_a_failure_closes_with_the_provider_and_says_so(ctx, envelopes) -> None:
    """Edit mode has the same import: a profile refused leaves the overlay open with Retry failed and Close; Close hands the provider to the caller and the toast counts."""
    _open(ctx, editRow=_openchat_row())
    _route(ctx, {
        "GET /llm_providers/p/discovered_models": _models({"name": "a", "context_length": 1}, {"name": "b", "context_length": 2}),
        "GET /llm_providers/p/models": {"answer": {"models": []}},
        "POST /model_profiles": [{"answer": {"id": "p--a"}}, {"refuse": envelopes["profile_broke"]}],
    })
    _press(ctx, "pc-register-import-more")
    _tick(ctx, "a", "b")
    _press(ctx, "pc-register-submit")
    assert ctx.eval("CLOSED") == 0 and ctx.eval("text('pc-register-retry-failed')") == "Retry failed"
    assert ctx.eval("MR.find('pc-register-back') === null && MR.find('pc-register-close') !== null") is True
    assert [c["method"] for c in _calls(ctx) if c["method"] != "GET"] == ["POST", "POST"], "the provider is not written: two profiles, nothing else"
    ctx.eval("MR.click('pc-register-close')")
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": "Profiles imported", "detail": "p: 1 of 2 profiles imported"}]
    assert js(ctx, "SAVED")[0]["id"] == "p" and ctx.eval("CLOSED") == 1


def test_back_from_import_more_returns_to_the_edit_page_with_its_draft(ctx) -> None:
    _open(ctx, editRow=_openchat_row())
    _route(ctx, {"GET /llm_providers/p/discovered_models": _models({"name": "a", "context_length": 1}), "GET /llm_providers/p/models": {"answer": {"models": []}}})
    _press(ctx, "pc-register-import-more")
    assert ctx.eval("MR.find('pc-llm-models') !== null") is True
    ctx.eval("MR.click('pc-register-back')")
    assert ctx.eval("MR.find('pc-llm-settings') !== null && MR.find('pc-llm-models') === null") is True
    assert ctx.eval("textOf(MR.find('pc-register-submit'))") == "Save" and ctx.eval("value('pc-field:id')") == "p"
    assert ctx.eval("disabled('pc-register-import-more')") is False, "nothing was changed"


def test_import_more_that_lists_nothing_or_fails_stays_on_the_page(ctx, envelopes) -> None:
    _open(ctx, editRow=_openchat_row())
    _route(ctx, {"GET /llm_providers/p/discovered_models": {"answer": {"models": []}}, "GET /llm_providers/p/models": {"answer": {"models": []}}})
    assert _press(ctx, "pc-register-import-more") == "fulfilled"
    assert _error(ctx) == "The provider answered but listed no models." and ctx.eval("MR.find('pc-llm-models') === null") is True
    _route(ctx, {"GET /llm_providers/p/discovered_models": {"refuse": envelopes["probe_failed"]}, "GET /llm_providers/p/models": {"answer": {"models": []}}})
    assert _press(ctx, "pc-register-import-more") == "fulfilled"
    assert "All connection attempts failed" in _error(ctx) and ctx.eval("MR.find('pc-llm-models') === null") is True
    assert ctx.eval("ariaBusy('pc-register-import-more')") is False, "and it can be asked again"


def test_import_more_is_off_while_page_one_has_unsaved_changes(ctx) -> None:
    """The probe reads the SAVED provider, so it would list what the draft does not say: it is off, with the help, while the draft differs from the row."""
    _open(ctx, editRow=_openchat_row())
    assert ctx.eval("disabled('pc-register-import-more')") is False and ctx.eval("MR.find('pc-register-import-more-help') === null") is True
    original = ctx.eval("value('pc-field:url')")
    _type(ctx, "pc-field:url", "http://elsewhere.test/v1")
    assert ctx.eval("disabled('pc-register-import-more')") is True and ctx.eval("ariaDisabled('pc-register-import-more')") is True
    assert ctx.eval("text('pc-register-import-more-help')") == "Save your changes first"
    helper_id = ctx.eval("MR.find('pc-register-import-more-help').props.id")
    assert ctx.eval("nativeButton('pc-register-import-more').props['aria-describedby']") == helper_id, "the button is described by the help"
    ctx.eval("MR.find('pc-register-import-more').props.onClick({ preventDefault: function () {}, stopPropagation: function () {} }); MR.rerender();")
    assert _requests(ctx) == [], "a press while it is off asks nothing"
    _type(ctx, "pc-field:url", original)
    assert ctx.eval("disabled('pc-register-import-more')") is False, "typed back: the draft is the row again"
    assert ctx.eval("MR.find('pc-register-import-more-help') === null") is True
    _type(ctx, "pc-limit:max_concurrency", "9")
    assert ctx.eval("disabled('pc-register-import-more')") is True, "a limit is a change too"


def test_import_more_takes_the_probes_stamps_so_a_later_save_does_not_put_them_back(ctx, envelopes) -> None:
    """The probe of the saved provider stamps ``last_probe_*`` on the stored row (it works or it fails). The draft was made from the row as it opened, so a Save pressed after
    Back would send the OLD stamps and put the provider's reachability badge back. The page reads the row again and takes the new stamps, and only those: what was typed while
    the probe was out stays."""
    row = _openchat_row()
    row.update(last_probe_at="2026-10-01T08:00:00Z", last_probe_ok=False, last_error="earlier: HTTP 401")
    stamped = dict(row, last_probe_at="2026-10-18T12:00:00Z", last_probe_ok=True, last_error=None)
    _open(ctx, editRow=row)
    _route(ctx, {
        "GET /llm_providers/p/discovered_models": {"hold": "probe"},
        "GET /llm_providers/p/models": {"answer": {"models": []}},
        "GET /llm_providers/p": {"answer": stamped},
    })
    ctx.eval("press('pc-register-import-more')")
    settle(ctx)
    _type(ctx, "pc-limit:max_concurrency", "7")
    ctx.eval("HELD.probe.ok({ models: [{ name: 'a', context_length: 5 }] }); void 0;")
    settle(ctx)
    assert ctx.eval("MR.find('pc-llm-models') !== null") is True
    ctx.eval("MR.click('pc-register-back')")
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    (put,) = _calls(ctx, "PUT")
    assert put["body"] == dict(stamped, limits=dict(stamped["limits"], max_concurrency=7)), "the new stamps, the row otherwise as it was, and the edit made meanwhile"
    # and a probe that FAILS stamps the row too
    _open(ctx, editRow=row)
    _route(ctx, {"GET /llm_providers/p/discovered_models": {"refuse": envelopes["probe_failed"]}, "GET /llm_providers/p/models": {"answer": {"models": []}},
                 "GET /llm_providers/p": {"answer": dict(stamped, last_probe_ok=False, last_error="probe failed")}})
    _press(ctx, "pc-register-import-more")
    assert "All connection attempts failed" in _error(ctx)
    _press(ctx, "pc-register-submit")
    body = _calls(ctx, "PUT")[-1]["body"]
    assert body["last_probe_ok"] is False and body["last_error"] == "probe failed" and body["last_probe_at"] == "2026-10-18T12:00:00Z"


def test_an_untouched_draft_is_not_unsaved_when_the_caller_hands_the_row_again(ctx) -> None:
    """The page compares its draft with the row AS OPENED. A caller that reloads its list under the open overlay hands a new ``editRow`` (the probe changed the stamps); the
    draft nobody touched must not turn into "unsaved changes"."""
    row = _openchat_row()
    _open(ctx, editRow=row)
    assert ctx.eval("disabled('pc-register-import-more')") is False
    again = dict(row, last_probe_ok=True, last_probe_at="2026-10-18T12:00:00Z")
    ctx.eval(f"MR.rerender({json.dumps({'initialClass': 'llm', 'editRow': again})}); void 0;")
    assert ctx.eval("disabled('pc-register-import-more')") is False and ctx.eval("MR.find('pc-register-import-more-help') === null") is True


def test_a_refused_save_stays_open_with_the_reason(ctx, envelopes) -> None:
    _open(ctx, editRow=_openchat_row())
    _route(ctx, {"PUT /llm_providers/p": {"refuse": envelopes["put_422"]}})
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    reason = ctx.eval(f"window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(envelopes['put_422'])})).message")
    assert reason and _error(ctx) == reason
    assert ctx.eval("CLOSED") == 0 and js(ctx, "SAVED") == [] and js(ctx, "TOASTS") == []
    assert ctx.eval("ariaBusy('pc-register-submit')") is False and ctx.eval("disabled('pc-register-submit')") is False, "Save works again"


def test_the_page_is_busy_while_a_save_is_out_and_the_selector_stays_shut(ctx) -> None:
    _open(ctx, editRow=_openchat_row())
    _route(ctx, {"PUT /llm_providers/p": {"hold": "save"}})
    ctx.eval("press('pc-register-submit')")
    settle(ctx)
    assert ctx.eval("ariaBusy('pc-register-submit')") is True and ctx.eval("ariaDisabled('pc-register-import-more')") is True
    assert ctx.eval("MR.find('pc-register-delete') !== null && ariaDisabled('pc-register-delete')") is True, "the overlay holds Delete off while the page's request is out"
    ctx.eval("press('pc-register-submit')")
    settle(ctx)
    assert len(_calls(ctx, "PUT")) == 1, "a second Save is not a second request"
    ctx.eval("HELD.save.ok({ id: 'p' }); void 0;")
    settle(ctx)
    assert ctx.eval("CLOSED") == 1


def test_the_actions_are_held_off_while_a_delete_is_out(ctx, envelopes) -> None:
    """The overlay's Delete says the page's actions are ``blocked``: Save and Import more are held off (aria-disabled, not busy), a press sends nothing, and when the
    Delete is refused they work again."""
    _open(ctx, editRow=_openchat_row())
    _route(ctx, {"DELETE /llm_providers/p": {"hold": "delete"}})
    ctx.eval("press('pc-register-delete')")
    settle(ctx)
    for testid in ("pc-register-submit", "pc-register-import-more"):
        assert ctx.eval(f"ariaDisabled({json.dumps(testid)})") is True and ctx.eval(f"ariaBusy({json.dumps(testid)})") is False, testid
        ctx.eval(f"nativeButton({json.dumps(testid)}).props.onClick({{ preventDefault: function () {{}}, stopPropagation: function () {{}} }}); void 0;")
    settle(ctx)
    assert _requests(ctx) == [("DELETE", "/llm_providers/p")], "nothing but the delete was sent"
    ctx.eval("HELD.delete.no(new window.primerApi.ApiError(" + json.dumps(envelopes["provider_taken"]) + ")); void 0;")
    settle(ctx)
    assert ctx.eval("ariaDisabled('pc-register-submit')") is False and ctx.eval("ariaDisabled('pc-register-import-more')") is False


PAGE_STUBS = r"""
// The page on its own, with a frame that draws the parts it is handed: what the page asks of the overlay is read straight off the elements.
function frameFn(parts) {
  LAST = parts;
  return el("div", { "data-testid": "frame" },
    el("div", { "data-testid": "frame-body" }, parts.body),
    el("div", { "data-testid": "frame-left" }, parts.footerLeft),
    el("div", { "data-testid": "frame-right" }, parts.footerRight),
    parts.error ? el("div", { "data-testid": "frame-error" }, parts.error) : null);
}
var WORDS = [], LAST = null;
function mountPage(props) {
  MR.mount(function Host(p) {
    return el(PC_LlmRegister, Object.assign({
      klass: PC_classByKey("llm"), editRow: null, excludeKinds: null, frame: frameFn, blocked: false,
      onSaved: function (row, words) { SAVED.push(row); WORDS.push(words === undefined ? null : words); }, onClose: function () { CLOSED += 1; },
    }, p));
  }, props || {});
}
"""


def test_a_blocked_page_holds_every_create_action_off(ctx) -> None:
    """A page is handed ``blocked`` in either mode (the overlay's Delete is out): every action it has is held off (aria-disabled, not busy), and a press sends nothing, whether
    it goes through the button (which refuses it) or straight to the page's handler."""
    ctx.eval(PAGE_STUBS)
    ctx.eval("mountPage();")
    _serve(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": {"refuse": {"type": "/errors/bad-request", "title": "Bad Request", "status": 400, "detail": "no"}}})
    _fill(ctx)
    _press(ctx, "pc-register-next")
    assert ctx.eval("MR.find('pc-register-without-models') !== null") is True
    calls_before = len(js(ctx, "CALLS"))
    ctx.eval("MR.rerender({ blocked: true });")
    for testid in ("pc-register-next", "pc-register-without-models"):
        assert ctx.eval(f"ariaDisabled({json.dumps(testid)})") is True and ctx.eval(f"ariaBusy({json.dumps(testid)})") is False, testid
        _press(ctx, testid)
    assert len(js(ctx, "CALLS")) == calls_before, "nothing was sent"
    ctx.eval("MR.rerender({ blocked: false });")
    assert ctx.eval("ariaDisabled('pc-register-next')") is False and ctx.eval("ariaDisabled('pc-register-without-models')") is False


def test_the_import_hands_its_own_words_to_on_saved(ctx) -> None:
    """``onSaved(row, words)``: the overlay toasts the words the page gives it, and tells the caller only the row. Without words it says what it always did."""
    ctx.eval(PAGE_STUBS)
    ctx.eval("mountPage();")
    _serve(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": _models({"name": "m", "context_length": 5}), "POST /llm_providers": {"answer": {"id": "lm", "made": True}}})
    _fill(ctx)
    _press(ctx, "pc-register-next")
    _tick(ctx, "m")
    _press(ctx, "pc-register-submit")
    assert js(ctx, "SAVED") == [{"id": "lm", "made": True}], "the row the server answered"
    assert js(ctx, "WORDS") == [{"title": "Provider registered", "detail": "lm: 1 profile imported"}]


def test_a_rowless_provider_success_never_hands_the_typed_key_to_the_host(ctx) -> None:
    ctx.eval(PAGE_STUBS)
    ctx.eval("mountPage();")
    _serve(ctx)
    _route(ctx, {
        "POST /llm_providers/_discover_models": {"refuse": {"status": 400, "title": "Bad Request", "detail": "offline"}},
        "POST /llm_providers": {"answer": None},
    })
    _fill(ctx, key="sk-do-not-escape")
    _press(ctx, "pc-register-next")
    _press(ctx, "pc-register-without-models")
    assert js(ctx, "SAVED") == [{"id": "lm"}]


def test_the_selector_is_locked_while_a_request_is_out(ctx) -> None:
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": {"hold": "discover"}})
    _fill(ctx)
    assert ctx.eval("MR.find('pc-register-type').props['aria-disabled'] === undefined") is True
    ctx.eval("press('pc-register-next')")
    settle(ctx)
    assert ctx.eval("MR.find('pc-register-type').props['aria-disabled']") == "true" and ctx.eval("ariaBusy('pc-register-next')") is True
    ctx.eval("HELD.discover.ok({ models: [{ name: 'm', context_length: 5 }] }); void 0;")
    settle(ctx)
    assert ctx.eval("MR.find('pc-register-type').props['aria-disabled'] === undefined") is True and ctx.eval("MR.find('pc-llm-models') !== null") is True


def test_a_second_next_while_the_provider_is_answering_asks_nothing_more(ctx) -> None:
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": {"hold": "discover"}})
    _fill(ctx)
    for _ in range(3):
        ctx.eval("press('pc-register-next')")
        settle(ctx)
    assert len(_calls(ctx, "POST", "/llm_providers/_discover_models")) == 1
    ctx.eval("HELD.discover.ok({ models: [] }); void 0;")
    settle(ctx)


def test_a_discovery_answer_for_a_connection_that_has_since_changed_is_not_used(ctx) -> None:
    """A URL fixed while the first request was still out: its answer is for the old address. Showing it would register the new address with the old server's models."""
    _open(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": {"hold": "old"}})
    _fill(ctx)
    ctx.eval("press('pc-register-next')")
    settle(ctx)
    _type(ctx, "pc-field:url", "http://fixed.test/v1")
    ctx.eval("HELD.old.ok({ models: [{ name: 'from-the-old-server', context_length: 1 }] }); void 0;")
    settle(ctx)
    assert ctx.eval("MR.find('pc-llm-models') === null") is True, "the old list is not shown"
    assert "changed while the provider was answering" in _error(ctx)
    assert ctx.eval("value('pc-field:url')") == "http://fixed.test/v1"
    _route(ctx, {"POST /llm_providers/_discover_models": _models({"name": "from-the-new-server", "context_length": 1})})
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert js(ctx, "ALL('pc-import').filter(function (n) { return n.type === 'label' && textOf(n) !== 'Select all'; }).map(textOf)") == ["from-the-new-server"]


def test_the_kinds_a_caller_excludes_are_not_offered(ctx) -> None:
    _open(ctx, excludeKinds=["ollama", "anthropic"])
    assert js(ctx, "options('pc-field:type')") == ["openchat", "openresponses", "gemini", "openrouter"]


def test_cancel_closes_the_overlay(ctx) -> None:
    _open(ctx)
    ctx.eval("MR.click('pc-register-cancel')")
    assert ctx.eval("CLOSED") == 1 and _requests(ctx) == [] and js(ctx, "SAVED") == []


def test_the_page_says_while_the_kinds_are_loading_and_offers_no_next(ctx) -> None:
    ctx.eval("mountOverlay({ initialClass: 'llm' });")
    assert ctx.eval("textOf(MR.find('pc-register-loading'))") == "Loading the fields for this kind…"
    assert ctx.eval("MR.find('pc-register-next') !== null && disabled('pc-register-next')") is True, "the footer is there while the body loads"
    assert ctx.eval("MR.find('pc-register-cancel') !== null") is True
    _serve(ctx)
    assert ctx.eval("MR.find('pc-register-loading') === null") is True


def test_a_failed_kinds_request_is_an_alert_with_a_try_again_that_works(ctx, envelopes) -> None:
    key = "PC_typesKey('llm_providers')"
    ctx.eval("mountOverlay({ initialClass: 'llm' });")
    ctx.eval(f"fail({key}, {json.dumps(envelopes['put_422'])}); MR.rerender();")
    assert ctx.eval("MR.find('pc-register-types-error').props.role") == "alert"
    assert "Could not load the fields for this kind" in ctx.eval("textOf(MR.find('pc-register-types-error'))")
    assert ctx.eval("disabled('pc-register-next')") is True
    refetched = ctx.eval(f"RES[{key}].refetched")
    ctx.eval("MR.click('pc-register-types-retry')")
    assert ctx.eval(f"RES[{key}].refetched") == refetched + 1
    before = ctx.eval("MR.findProp('to', 'first').props.n")
    _serve(ctx)
    assert ctx.eval("MR.find('pc-register-types-error') === null && MR.find('pc-field:url') !== null") is True
    assert ctx.eval("MR.findProp('to', 'first').props.n") == before + 1, "the focus is handed to the first field, which is drawn now"


def test_the_pages_work_on_a_phone_sheet(ctx) -> None:
    ctx.eval("MOBILE = true;")
    _to_page_two(ctx, [{"name": "m", "context_length": 8192}])
    assert ctx.eval("MR.find('pc-register').props.className").split() == ["sheet", "pk-overlay"]
    _tick(ctx, "m")
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert [c["body"]["model_name"] for c in _calls(ctx, "POST", "/model_profiles")] == ["m"] and ctx.eval("CLOSED") == 1


# ---- the focus ---------------------------------------------------------------------------------------------------------------------------------------------


def test_a_page_change_asks_for_the_focus_to_be_handed_over(ctx) -> None:
    """The button that took the page forward or back is gone when the other page is drawn. The page asks its body (the only thing that can act when the body has been
    drawn) for a hand-off: to the heading of the models page going forward, to the first field going back."""
    _open(ctx)
    assert ctx.eval("MR.findProp('to', 'first').props.n") == 0, "opening is the dialog's own business: it takes the focus in"
    _route(ctx, {"POST /llm_providers/_discover_models": _models({"name": "m", "context_length": 1})})
    _fill(ctx)
    _press(ctx, "pc-register-next")
    head = ctx.eval("MR.findProp('to', 'head').props.n")
    assert head >= 1 and ctx.eval("MR.find('pc-import-head').props.tabIndex") == -1, "the heading can take the focus"
    ctx.eval("MR.click('pc-register-back')")
    assert ctx.eval("MR.findProp('to', 'first').props.n") == head + 1


def test_a_button_that_is_gone_is_not_reused_as_another_action(ctx) -> None:
    """React keeps the DOM node of an element with the same type and key at the same place. The Back button of the models page and the Cancel of the connection page are both
    the first thing on the left of the footer: without keys of their own they would be ONE node, and a focus left on Back would stay on a button now called Cancel, where a
    second Enter closes the overlay. The footer's elements differ in key from page to page, so a page change replaces them (and the hand-off finds the focus lost)."""
    ctx.eval(PAGE_STUBS)
    ctx.eval("mountPage();")
    _serve(ctx)
    _route(ctx, {"POST /llm_providers/_discover_models": _models({"name": "m", "context_length": 1})})
    _fill(ctx)
    one = js(ctx, "[LAST.footerLeft.key, LAST.footerRight.key]")
    _press(ctx, "pc-register-next")
    two = js(ctx, "[LAST.footerLeft.key, LAST.footerRight.key]")
    assert all(one) and all(two), (one, two)
    assert one[0] != two[0] and one[1] != two[1], "page one's Cancel and Next are not page two's Back and Import"
    ctx.eval("MR.click('pc-register-back')")
    assert js(ctx, "[LAST.footerLeft.key, LAST.footerRight.key]") == one, "and Back brings page one's back"
    # the Import button, on the other hand, is ONE element from Import to Retry failed (the focus is meant to stay on it)
    _press(ctx, "pc-register-next")
    _tick(ctx, "m")
    before = js(ctx, "[LAST.footerRight.key, LAST.footerRight.type.name || 'Btn']")
    _route(ctx, {"POST /model_profiles": {"refuse": {"type": "/errors/internal", "title": "Internal Server Error", "status": 500, "detail": "no", "instance": "/v1/model_profiles"}}})
    _press(ctx, "pc-register-submit")
    assert ctx.eval("MR.find('pc-register-retry-failed') !== null") is True
    assert js(ctx, "[LAST.footerRight.key, LAST.footerRight.type.name || 'Btn']") == before


FOCUS_STUB = r"""
var FOCUSED = [];
var HEADING = { focus: function () { FOCUSED.push('heading'); } };
var FIRST = { focus: function () { FOCUSED.push('first'); } };
var ROOT = { querySelector: function (selector) { return selector === '[data-pc-focus]' ? HEADING : FIRST; } };
var ELSEWHERE = { id: 'a-button-the-operator-chose' };
function focusing(props) { MR.mount(PC_LlmFocus, Object.assign({ rootRef: { current: ROOT } }, props)); }
"""


def test_the_hand_off_acts_only_when_the_focus_was_lost(ctx) -> None:
    """``PC_LlmFocus`` with a document of its own: it moves the focus when it is on ``<body>`` (the button it was on was removed), never when the operator has put it somewhere."""
    ctx.eval(FOCUS_STUB)
    ctx.eval("document.activeElement = document.body; focusing({ n: 1, to: 'head' });")
    assert js(ctx, "FOCUSED") == ["heading"]
    ctx.eval("FOCUSED = []; document.activeElement = document.body; focusing({ n: 2, to: 'first' });")
    assert js(ctx, "FOCUSED") == ["first"]
    ctx.eval("FOCUSED = []; document.activeElement = ELSEWHERE; focusing({ n: 3, to: 'head' });")
    assert js(ctx, "FOCUSED") == [], "a focus the operator put somewhere is left where it is"
    ctx.eval("FOCUSED = []; document.activeElement = document.body; focusing({ n: 0, to: 'head' });")
    assert js(ctx, "FOCUSED") == [], "n is 0 until a hand-off is asked for (the first draw of the page)"
    ctx.eval("FOCUSED = []; focusing({ n: 4, to: 'head' }); MR.rerender(); MR.rerender();")
    assert js(ctx, "FOCUSED") == ["heading"], "once per request, not once per render"
    ctx.eval("FOCUSED = []; MR.mount(PC_LlmFocus, { n: 5, to: 'head', rootRef: { current: null } });")
    assert js(ctx, "FOCUSED") == [], "a body with no root (not drawn) has nothing to focus"


# ---- the unit pieces ---------------------------------------------------------------------------------------------------------------------------------------


def test_import_profiles_posts_only_ticked_undone_rows_in_order(ctx, envelopes) -> None:
    ctx.eval(f"var ENV = {json.dumps(envelopes)};")
    ctx.eval(
        """
        var POSTED = [], PROGRESS = [];
        var ROWS = [
          { name: 'new', ticked: true, context: '100', status: 'pending' },
          { name: 'skip', ticked: false, context: '100', status: 'pending' },
          { name: 'done', ticked: true, context: '100', status: 'imported' },
          { name: 'again', ticked: true, context: '100', status: 'failed', error: 'old reason' },
          { name: 'dup', ticked: true, context: '100', status: 'pending' },
          { name: '///', ticked: true, context: '100', status: 'invalid', error: 'No id can be made from this name' },
          { name: 'odd', ticked: true, context: '7', status: 'pending' },
        ];
        var RESULT = null;
        PC_importProfiles({
          apiFetch: function (method, path, body) {
            POSTED.push([method, path, body]);
            if (method === 'GET' && path === '/model_profiles/p-1--dup') {
              return Promise.resolve({ provider_id: 'p/1', model_name: 'dup' });
            }
            if (body.model_name === 'dup') return refuse(ENV.profile_taken);
            if (body.model_name === 'odd') return refuse(ENV.profile_broke);
            return Promise.resolve({});
          },
          providerId: 'p/1', rows: ROWS, onProgress: function (r) { PROGRESS.push(r.map(function (x) { return x.status; })); },
        }).then(function (r) { RESULT = r; });
        void 0;
        """
    )
    ctx.eval("void 0;")
    posted = js(ctx, "POSTED")
    assert [p[2]["model_name"] for p in posted if p[0] == "POST"] == ["new", "again", "dup", "odd"]
    assert posted[0] == ["POST", "/model_profiles", {"id": "p-1--new", "provider_id": "p/1", "model_name": "new", "context_length": 100, "description": "Imported from p/1"}]
    result = js(ctx, "RESULT")
    assert [(r["name"], r["status"]) for r in result] == [
        ("new", "imported"), ("skip", "pending"), ("done", "imported"), ("again", "imported"), ("dup", "already"), ("///", "invalid"), ("odd", "failed")]
    assert "error" not in result[3], "a row that is imported no longer carries its old failure"
    assert result[6]["error"] == ctx.eval(f"window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(envelopes['profile_broke'])})).message")
    assert len(js(ctx, "PROGRESS")) == 4, "onProgress hears each answered row once"
    assert js(ctx, "ROWS")[3]["status"] == "failed", "the rows it was handed are not changed"


def test_import_profiles_stops_when_told_to(ctx) -> None:
    ctx.eval(
        """
        var POSTED = [], STOP = false, RESULT = null;
        PC_importProfiles({
          apiFetch: function (m, p, body) { POSTED.push(body.model_name); STOP = true; return Promise.resolve({}); },
          providerId: 'p', shouldStop: function () { return STOP; },
          rows: [{ name: 'a', ticked: true, context: '1', status: 'pending' }, { name: 'b', ticked: true, context: '1', status: 'pending' }],
        }).then(function (r) { RESULT = r; });
        void 0;
        """
    )
    ctx.eval("void 0;")
    assert js(ctx, "POSTED") == ["a"] and [r["status"] for r in js(ctx, "RESULT")] == ["imported", "pending"]


def test_llm_body_applies_the_wire_rules_to_a_draft(ctx) -> None:
    """``PC_llmBody``: ticket T3 (an emptied optional box is omitted, never "") with the kind's shape, and without it; `limits` and unshown fields untouched."""
    served = types_of("llm_providers")
    draft = {"id": "lm", "provider": "openchat", "config": {"url": URL, "api_key": "  ", "flavor": "lmstudio"}, "limits": {"max_concurrency": 2, "request_timeout_seconds": None},
             "last_probe_ok": True}
    ctx.eval(f"var DRAFT = {json.dumps(draft)}, SHAPE = {json.dumps(served['openchat'])};")
    assert js(ctx, "PC_llmBody(DRAFT, SHAPE)") == {
        "id": "lm", "provider": "openchat", "config": {"url": URL, "flavor": "lmstudio"}, "limits": {"max_concurrency": 2, "request_timeout_seconds": None}, "last_probe_ok": True}
    assert js(ctx, "PC_llmBody(DRAFT)") == js(ctx, "PC_llmBody(DRAFT, SHAPE)"), "a caller without the shape still sends no empty text"
    assert js(ctx, "DRAFT")["config"]["api_key"] == "  ", "the draft is not changed"
    keyless = {"id": "an", "provider": "anthropic", "config": {"api_key": ""}, "limits": {"max_concurrency": 1}}
    ctx.eval(f"var KEYLESS = {json.dumps(keyless)}, ASHAPE = {json.dumps(served['anthropic'])};")
    assert js(ctx, "PC_llmBody(KEYLESS, ASHAPE)")["config"] == {}, "a kind that serves config fields always posts the key (no key is a 422)"


def test_the_page_is_the_llm_page_of_the_overlay(ctx) -> None:
    assert ctx.eval("window.PC_REGISTER_PAGES.llm === PC_LlmRegister") is True
    assert js(ctx, "[typeof PC_llmBody, typeof PC_importProfiles]") == ["function", "function"]
    for key in ("PC_llmBody", "PC_importProfiles", "PC_LlmRegister"):
        assert ctx.eval(f"window.{key} === {key}") is True, key


def test_index_loads_the_llm_page_after_the_overlay_and_before_the_catalog() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    tag = 'src="components/providers/register-llm.jsx"'
    assert html.count(tag) == 1
    assert html.index(tag) > html.index('src="components/providers/register-overlay.jsx"')
    assert html.index(tag) > html.index('src="components/providers/register-common.jsx"')
    assert html.index(tag) < html.index('src="components/provider-catalog.jsx"')
