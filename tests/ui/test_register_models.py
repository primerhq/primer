"""The Embedding and Cross Encoding pages of the Register provider overlay (plan 02, Task 14; design 6.5, 13.1, 13.2; master Review Focus 1 and 4).

An embedding or cross-encoder provider is registered in two pages: the connection (id, type, flavor, URL, key or token, the models a Hugging Face provider names, the limits
its adapters read), whose "Next" finds out which models can be registered (OpenAI and Gemini list them; Hugging Face and a cross encoder are asked of the Hub, one verdict per
typed model), and the page of models, whose "Register with N models" saves the provider with the ticked ones. Editing is the first page again, with the id and the type locked,
the models as a table of every kind, and "Save".

Everything runs for real in V8 (``tests/ui/_register_harness.py``): the real kit, form, foundation and overlay, the real ``register-models.jsx``, the ``_types`` maps the
route functions answer, the bodies the page sends are validated by the server's own models and request bodies, and the answers it reads are built by the server's own models
(``HfModelCheck``, ``EmbeddingProvider``, ``CrossEncoderProvider``). Only the network is a script: ``route`` says what each ``METHOD /path`` answers.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._register_harness import COMPONENTS, UI, base_code, context, js, settle, transpiled, types_of

MODELS_FILE = COMPONENTS / "providers" / "register-models.jsx"

PLURALS = {"embedding": "embedding_providers", "cross_encoder": "cross_encoder_providers"}

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
function checked(name) { return MR.find("pc-import:" + name).props.checked; }
function rowNames() {
  return ALL("pc-import").filter(function (n) { return n.type === "label" && textOf(n) !== "Select all"; }).map(textOf);
}
function headTone() {
  var sub = ALL("pc-import-head").filter(function (n) { return n.props && n.props.className === "pc-import-sub"; })[0];
  return sub ? sub.props["data-tone"] : null;
}
"""

URL = "http://emb.test/v1"
HF_A = "BAAI/bge-small-en-v1.5"
HF_B = "nope/missing"

BODIES = {
    "discover": "POST /embedding_providers/_discover_models",
    "hub": "POST /huggingface/_check_models",
    "create": "POST /embedding_providers",
    "create_ce": "POST /cross_encoder_providers",
}


@pytest.fixture(scope="module")
def code() -> str:
    return base_code(with_overlay=True) + "\n" + transpiled(MODELS_FILE)


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
    """The refusals the console reads, answered by the real error handlers."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from primer.api.errors import register_error_handlers
    from primer.api.routers import providers
    from primer.api.routers.providers import _probe_failure
    from primer.model.except_ import ConflictError

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(providers.embedding_provider_router, prefix="/v1")
    app.dependency_overrides[providers.get_embedding_provider_storage] = lambda: object()

    @app.post("/v1/probe_failed")
    def probe_failed():
        raise _probe_failure("openai-compatible probe failed: ConnectError: All connection attempts failed")

    @app.post("/v1/provider_taken")
    def provider_taken():
        raise ConflictError("EmbeddingProvider with id 'emb' already exists")

    @app.post("/v1/broke")
    def broke():
        raise RuntimeError("the database went away")

    client = TestClient(app, raise_server_exceptions=False)
    made = {
        "probe_failed": client.post("/v1/probe_failed"),
        "provider_taken": client.post("/v1/provider_taken"),
        "broke": client.post("/v1/broke"),
        # a body the real router refuses before it touches storage: no models and no limits
        "put_422": client.put("/v1/embedding_providers/emb", json={"id": "emb", "provider": "openai"}),
    }
    expected = {"probe_failed": 400, "provider_taken": 409, "broke": 500, "put_422": 422}
    for name, answer in made.items():
        assert answer.status_code == expected[name], (name, answer.status_code, answer.text)
    return {name: answer.json() for name, answer in made.items()}


def _route(ctx, table: dict) -> None:
    ctx.eval(f"ROUTES = {json.dumps(table)};")


def _serve(ctx, klass: str = "embedding") -> None:
    plural = PLURALS[klass]
    ctx.eval(f"serve(PC_typesKey({json.dumps(plural)}), {json.dumps(types_of(plural))}); MR.rerender();")


def _open(ctx, klass: str = "embedding", **props) -> None:
    props.setdefault("initialClass", klass)
    ctx.eval(f"mountOverlay({json.dumps(props)});")
    _serve(ctx, klass)


def _type(ctx, testid: str, text: str) -> None:
    ctx.eval(f"setValue({json.dumps(testid)}, {json.dumps(text)})")


def _press(ctx, testid: str) -> str:
    ctx.eval(f"press({json.dumps(testid)})")
    return settle(ctx)


def _click(ctx, testid: str) -> None:
    ctx.eval(f"MR.click({json.dumps(testid)})")


def _fill_openai(ctx, provider_id: str = "emb", url: str | None = URL, key: str | None = None) -> None:
    _type(ctx, "pc-field:id", provider_id)
    if url is not None:
        _type(ctx, "pc-field:url", url)
    if key is not None:
        _type(ctx, "pc-field:api_key", key)


def _fill_models(ctx, *names: str, max_pair: dict[int, str] | None = None) -> None:
    """Type the model names into the MODELS table (the first row is there; the others are added)."""
    for i, name in enumerate(names):
        if i > 0:
            _click(ctx, "pc-models:add")
        _type(ctx, f"pc-models:{i}:name", name)
    for i, length in (max_pair or {}).items():
        _type(ctx, f"pc-models:{i}:max_pair_length", length)


def _listed(*entries) -> dict:
    return {"answer": {"models": [{"name": e} if isinstance(e, str) else e for e in entries]}}


def _hub(*verdicts) -> dict:
    """The Hub check's answer, built by the server's own model: ``(name, status)`` or ``(name, status, detail)``."""
    from primer.common.hf_hub import HfModelCheck

    details = {
        "ok": "Can be pulled", "gated": "Gated: accept the model's terms on the Hub with the token's account", "disabled": "Disabled on the Hub",
        "bad_token": "The Hub refused the token", "not_found": "Not found, or private and the token cannot see it",
        "invalid_name": "Not a Hugging Face model id (namespace/name)", "error": "The Hub answered HTTP 500",
    }
    out = []
    for verdict in verdicts:
        name, status, *rest = verdict
        out.append(HfModelCheck(name=name, ok=status == "ok", status=status, detail=rest[0] if rest else details[status]).model_dump())
    return {"answer": {"models": out}}


def _error(ctx) -> str | None:
    return ctx.eval("MR.find('pc-register-error') ? textOf(MR.find('pc-register-error')) : null")


def _status(ctx, name: str) -> str:
    return ctx.eval(f"text({json.dumps('pc-import-status:' + name)})")


def _help(ctx, testid: str) -> str:
    """The help line under a control of the connection page (empty when it has none)."""
    row = js(ctx, f"fieldRow('pc-models-settings', {json.dumps(testid)})")
    return " ".join(c["text"] for c in row if c["className"] == "field-help" and not c["role"])


def _calls(ctx, method: str | None = None, path: str | None = None) -> list[dict]:
    return js(ctx, f"calls({json.dumps(method)}, {json.dumps(path)})")


def _requests(ctx) -> list[tuple[str, str]]:
    return [(c["method"], c["path"]) for c in js(ctx, "CALLS")]


def _to_page_two_openai(ctx, models, **fill) -> None:
    """Open the overlay on an OpenAI-compatible embedding server that lists ``models``, press Next, and land on the models page."""
    _open(ctx)
    _route(ctx, {BODIES["discover"]: _listed(*models)})
    _fill_openai(ctx, **fill)
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert ctx.eval("MR.find('pc-models-pick') !== null") is True, "the models page is up"


def _to_page_two_hub(ctx, klass: str, verdicts, names, token: str | None = None, max_pair: dict[int, str] | None = None) -> None:
    """Open the overlay on a Hugging Face provider with ``names`` typed, press Next, and land on the page of the Hub's verdicts."""
    _open(ctx, klass)
    if klass == "embedding":
        _type(ctx, "pc-field:type", "huggingface")
    _route(ctx, {BODIES["hub"]: _hub(*verdicts)})
    _type(ctx, "pc-field:id", "hf-1")
    _fill_models(ctx, *names, max_pair=max_pair)
    if token is not None:
        _type(ctx, "pc-field:token", token)
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert ctx.eval("MR.find('pc-models-pick') !== null") is True, "the models page is up"


def _validate(model_name: str, body: dict):
    """The body, taken by the server's own model: what a real create would accept."""
    from primer.model.providers.cross_encoder import CrossEncoderProvider
    from primer.model.providers.embedding import EmbeddingProvider

    model = {"embedding": EmbeddingProvider, "cross_encoder": CrossEncoderProvider}[model_name]
    return model.model_validate(body)


# ---- page one ----------------------------------------------------------------------------------------------------------------------------------------------


def test_the_type_select_lists_the_three_kinds_in_the_designs_order_and_starts_on_openai(ctx) -> None:
    _open(ctx)
    assert js(ctx, "options('pc-field:type')") == ["openai", "gemini", "huggingface"]
    assert ctx.eval("value('pc-field:type')") == "openai"
    labels = js(ctx, "ALL('pc-field:type').filter(function (n) { return n.type === 'option'; }).map(function (n) { return textOf(n); })")
    assert labels == ["OpenAI", "Gemini", "Hugging Face"]


def test_url_is_offered_only_for_openai(ctx) -> None:
    """The server says what a kind takes (``_types``): the URL box is there exactly when the kind serves a ``url`` field. A Gemini connection is a key and nothing else."""
    served = types_of("embedding_providers")
    _open(ctx)
    for kind, has_url in (("openai", True), ("gemini", False), ("huggingface", False)):
        assert has_url == any(f["key"] == "url" for f in served[kind]["config_fields"]), "the premise is what the server serves"
        _type(ctx, "pc-field:type", kind)
        assert ctx.eval("MR.find('pc-field:url') !== null") is has_url, kind
    _type(ctx, "pc-field:type", "openai")
    assert ctx.eval("MR.find('pc-field:url').props['aria-required']") == "true"
    assert js(ctx, "fieldRow('pc-models-settings', 'pc-field:url')")[0]["text"].startswith("URL")


def test_flavor_is_for_openai_only_and_starts_on_the_models_default(ctx) -> None:
    served = types_of("embedding_providers")
    options = next(f for f in served["openai"]["config_fields"] if f["key"] == "flavor")["options"]
    _open(ctx)
    assert js(ctx, "options('pc-field:flavor')") == options
    assert ctx.eval("value('pc-field:flavor')") == "other", "the model's own default, so what the box shows is what is sent"
    labels = js(ctx, "ALL('pc-field:flavor').filter(function (n) { return n.type === 'option'; }).map(function (n) { return textOf(n); })")
    assert labels == ["OpenAI", "LM Studio", "vLLM", "Other"]
    for kind in ("gemini", "huggingface"):
        _type(ctx, "pc-field:type", kind)
        assert ctx.eval("MR.find('pc-field:flavor') === null") is True, kind


def test_the_key_box_is_a_key_for_openai_and_gemini_and_a_token_for_hugging_face(ctx) -> None:
    _open(ctx)
    assert js(ctx, "fieldRow('pc-models-settings', 'pc-field:api_key')")[0]["text"] == "API key"
    assert _help(ctx, "pc-field:api_key") == "Optional for local servers", "a kind with a URL is a server that may run open"
    _type(ctx, "pc-field:type", "gemini")
    served = next(f for f in types_of("embedding_providers")["gemini"]["config_fields"] if f["key"] == "api_key")
    assert js(ctx, "fieldRow('pc-models-settings', 'pc-field:api_key')")[0]["text"] == "API key"
    assert _help(ctx, "pc-field:api_key") == served["help"], "a hosted kind keeps the words the server serves for it"
    _type(ctx, "pc-field:type", "huggingface")
    assert ctx.eval("MR.find('pc-field:api_key') === null") is True, "Hugging Face has a token, not a key"
    assert js(ctx, "fieldRow('pc-models-settings', 'pc-field:token')")[0]["text"] == "API token"
    assert _help(ctx, "pc-field:token") == "Used to pull the models; optional for public ones"
    assert ctx.eval("MR.find('pc-field:token').props['aria-required'] === undefined") is True, "the token is optional"
    assert ctx.eval("MR.find('pc-field:token').props.type") == "password"


def test_hugging_face_shows_a_models_table_and_an_optional_token(ctx) -> None:
    """The model list of a Hugging Face provider is typed (there is no inventory to list), so its MODELS table is on page one, with one empty row to start."""
    _open(ctx)
    assert ctx.eval("MR.find('pc-models') === null") is True, "OpenAI lists its models; there is no table to type them in"
    _type(ctx, "pc-field:type", "huggingface")
    assert ctx.eval("MR.find('pc-models') !== null") is True
    table = ctx.eval("textOf(MR.find('pc-models'))")
    assert "MODELS" in table and "+ Add" in table
    assert js(ctx, "[MR.find('pc-models:0:name') !== null, MR.find('pc-models:1:name') === null]") == [True, True], "one empty row to start"
    assert ctx.eval("MR.find('pc-models:0:name').props['aria-label']") == "Model 1"
    assert ctx.eval("MR.find('pc-models:0:max_pair_length') === null") is True, "an embedding model has a name and nothing else"
    assert ctx.eval("MR.find('pc-field:token') !== null && MR.find('pc-field:url') === null") is True
    _click(ctx, "pc-models:add")
    assert ctx.eval("MR.find('pc-models:1:name') !== null") is True
    _click(ctx, "pc-models:remove:0")
    assert ctx.eval("MR.find('pc-models:1:name') === null") is True


def test_the_cross_encoder_page_has_a_token_models_with_a_pair_length_and_one_limit(ctx) -> None:
    served = types_of("cross_encoder_providers")
    assert list(served) == ["huggingface"], "the premise: one kind, so there is no Type to pick"
    _open(ctx, "cross_encoder")
    assert ctx.eval("MR.find('pc-field:type') === null") is True and ctx.eval("MR.find('pc-field:url') === null") is True
    assert js(ctx, "fieldRow('pc-models-settings', 'pc-field:token')")[0]["text"] == "API token"
    assert _help(ctx, "pc-field:token") == "Used to pull the models; optional for public ones"
    table = ctx.eval("textOf(MR.find('pc-models'))")
    assert "MODELS" in table and "Model" in table and "Max pair length" in table
    assert ctx.eval("MR.find('pc-models:0:max_pair_length').props.type") == "number"
    assert js(ctx, "testids('pc-limits')").count("pc-limit:max_concurrency") == 1
    assert [t for t in js(ctx, "testids('pc-limits')") if t.startswith("pc-limit:")] == ["pc-limit:max_concurrency"]


def test_limits_follow_the_kind(ctx) -> None:
    """Only the limits the kind's adapters read (``PC_LIMIT_FIELDS``): max concurrency and the connect timeout for OpenAI and Gemini, max concurrency alone for Hugging Face, and
    no Retries box anywhere (the embedders never retry)."""
    _open(ctx)
    for kind in ("openai", "gemini"):
        _type(ctx, "pc-field:type", kind)
        assert "LIMITS" in ctx.eval("textOf(MR.find('pc-limits'))") and "RETRIES" not in ctx.eval("textOf(MR.find('pc-limits'))")
        assert [t for t in js(ctx, "testids('pc-limits')") if t.startswith("pc-limit:")] == ["pc-limit:max_concurrency"], kind
        assert ctx.eval("MR.find('pc-limits-more') !== null") is True, "the connect timeout is folded away"
        _click(ctx, "pc-limits-more:toggle")
        assert [t for t in js(ctx, "testids('pc-limits')") if t.startswith("pc-limit:")] == ["pc-limit:max_concurrency", "pc-limit:connect_timeout_seconds"], kind
        _click(ctx, "pc-limits-more:toggle")
    _type(ctx, "pc-field:type", "huggingface")
    assert [t for t in js(ctx, "testids('pc-limits')") if t.startswith("pc-limit:")] == ["pc-limit:max_concurrency"]
    assert ctx.eval("MR.find('pc-limits-more') === null") is True, "no fold when there is nothing to fold"
    for field in ("max_retries", "retry_backoff_seconds", "request_timeout_seconds", "total_timeout_seconds", "retry_backoff_max_seconds"):
        assert ctx.eval(f"MR.find('pc-limit:{field}') === null") is True, field
    assert ctx.eval("value('pc-limit:max_concurrency')") == "1"


def test_next_is_off_until_the_connection_can_be_checked(ctx) -> None:
    """Off while the id breaks the id rule, a required served field is blank, a limit is wrong, or (Hugging Face) a MODELS row is blank. ``disabled`` (not the in-flight ``busy``) says so."""
    _open(ctx)
    assert ctx.eval("disabled('pc-register-next')") is True, "nothing typed"
    _type(ctx, "pc-field:id", "a/b")
    _type(ctx, "pc-field:url", URL)
    assert ctx.eval("disabled('pc-register-next')") is True, "'a/b' is not an id the server can address"
    _type(ctx, "pc-field:id", "emb")
    assert ctx.eval("disabled('pc-register-next')") is False
    _type(ctx, "pc-field:url", "   ")
    assert ctx.eval("disabled('pc-register-next')") is True, "a blank URL is blank"
    _type(ctx, "pc-field:url", URL)
    _type(ctx, "pc-limit:max_concurrency", "0")
    assert ctx.eval("disabled('pc-register-next')") is True and "whole number of 1 or more" in ctx.eval("textOf(MR.find('pc-limits'))")
    _type(ctx, "pc-limit:max_concurrency", "4")
    assert ctx.eval("disabled('pc-register-next')") is False
    # Gemini: a key is optional, so a connection with only an id can be asked
    _type(ctx, "pc-field:type", "gemini")
    assert ctx.eval("disabled('pc-register-next')") is False
    # Hugging Face: the models are the connection
    _type(ctx, "pc-field:type", "huggingface")
    assert ctx.eval("disabled('pc-register-next')") is True, "no model typed yet"
    _type(ctx, "pc-models:0:name", HF_A)
    assert ctx.eval("disabled('pc-register-next')") is False
    _click(ctx, "pc-models:add")
    assert ctx.eval("disabled('pc-register-next')") is True, "a blank row is not a model"
    _type(ctx, "pc-models:1:name", "  ")
    assert ctx.eval("disabled('pc-register-next')") is True, "and spaces are blank"
    _click(ctx, "pc-models:remove:1")
    assert ctx.eval("disabled('pc-register-next')") is False


def test_a_model_typed_twice_or_too_many_models_keep_next_off_and_say_why(ctx) -> None:
    _open(ctx)
    _type(ctx, "pc-field:type", "huggingface")
    _type(ctx, "pc-field:id", "hf-1")
    _fill_models(ctx, HF_A, f"  {HF_A}  ")
    assert ctx.eval("disabled('pc-register-next')") is True
    problem = ctx.eval("text('pc-models-problem')")
    assert HF_A in problem and "twice" in problem
    assert ctx.eval("MR.find('pc-models-problem').props.role") == "alert"
    _click(ctx, "pc-models:remove:1")
    assert ctx.eval("disabled('pc-register-next')") is False and ctx.eval("MR.find('pc-models-problem') === null") is True
    # the Hub is asked about 50 names at most (the route refuses a longer list)
    for i in range(1, 51):
        _click(ctx, "pc-models:add")
        _type(ctx, f"pc-models:{i}:name", f"org/model-{i}")
    assert ctx.eval("disabled('pc-register-next')") is True
    assert "50" in ctx.eval("text('pc-models-problem')")


def test_a_pair_length_must_be_a_whole_number_of_one_or_more(ctx) -> None:
    """``CrossEncoderModel.max_pair_length`` is a positive integer or nothing."""
    _open(ctx, "cross_encoder")
    _type(ctx, "pc-field:id", "ce-1")
    _type(ctx, "pc-models:0:name", "BAAI/bge-reranker-v2-m3")
    for bad in ("0", "-5", "12.5", "1e3", "abc"):
        _type(ctx, "pc-models:0:max_pair_length", bad)
        assert ctx.eval("disabled('pc-register-next')") is True, bad
        assert "whole number of 1 or more" in ctx.eval("text('pc-models-problem')"), bad
    _type(ctx, "pc-models:0:max_pair_length", "512")
    assert ctx.eval("disabled('pc-register-next')") is False and ctx.eval("MR.find('pc-models-problem') === null") is True
    _type(ctx, "pc-models:0:max_pair_length", "")
    assert ctx.eval("disabled('pc-register-next')") is False, "the length is optional"


def test_next_discovers_for_openai_and_gemini(ctx) -> None:
    from primer.api.routers.providers import _DiscoverModelsBody

    _open(ctx)
    _route(ctx, {BODIES["discover"]: _listed("m")})
    _fill_openai(ctx)
    assert _press(ctx, "pc-register-next") == "fulfilled"
    (call,) = _calls(ctx)
    assert call == {"method": "POST", "path": "/embedding_providers/_discover_models", "body": {"provider": "openai", "config": {"url": URL, "flavor": "other"}}}
    assert "api_key" not in call["body"]["config"], "a blank optional key is omitted, never sent as an empty string (ticket T3)"
    _DiscoverModelsBody.model_validate(call["body"])
    # a typed key and a chosen flavor go with it
    _open(ctx)
    _type(ctx, "pc-field:flavor", "lmstudio")
    _fill_openai(ctx, key="sk-1")
    _press(ctx, "pc-register-next")
    assert _calls(ctx, "POST", "/embedding_providers/_discover_models")[-1]["body"] == {
        "provider": "openai", "config": {"url": URL, "flavor": "lmstudio", "api_key": "sk-1"}}
    # Gemini: a key and nothing else
    _open(ctx)
    _type(ctx, "pc-field:type", "gemini")
    _type(ctx, "pc-field:id", "gem")
    _type(ctx, "pc-field:api_key", "AIza-test-KEY-9876")
    _press(ctx, "pc-register-next")
    assert _calls(ctx, "POST", "/embedding_providers/_discover_models")[-1]["body"] == {"provider": "gemini", "config": {"api_key": "AIza-test-KEY-9876"}}
    assert [r for r in _requests(ctx) if r[1].endswith("_check_models")] == [], "an inventory is listed, not looked up on the Hub"


def test_next_checks_the_hub_with_the_typed_token(ctx) -> None:
    from primer.api.routers.hf_hub import _HfCheckBody

    _to_page_two_hub(ctx, "embedding", [(HF_A, "ok"), (HF_B, "not_found")], [f" {HF_A} ", HF_B], token="hf_secret_1234")
    (call,) = _calls(ctx)
    assert call == {"method": "POST", "path": "/huggingface/_check_models", "body": {"models": [HF_A, HF_B], "token": "hf_secret_1234"}}
    _HfCheckBody.model_validate(call["body"])
    assert [r for r in _requests(ctx) if r[1].endswith("_discover_models")] == [], "Hugging Face has no inventory to list"
    # no token typed: the route's null, never an empty string
    _to_page_two_hub(ctx, "embedding", [(HF_A, "ok")], [HF_A])
    assert _calls(ctx, "POST", "/huggingface/_check_models")[-1]["body"] == {"models": [HF_A], "token": None}
    # the cross encoder asks the same question the same way
    _to_page_two_hub(ctx, "cross_encoder", [("BAAI/bge-reranker-v2-m3", "ok")], ["BAAI/bge-reranker-v2-m3"], token="  hf_x  ")
    assert _calls(ctx, "POST", "/huggingface/_check_models")[-1]["body"] == {"models": ["BAAI/bge-reranker-v2-m3"], "token": "hf_x"}, "the token is trimmed"


# ---- page two ----------------------------------------------------------------------------------------------------------------------------------------------


def test_page_two_lists_embedding_models_first_and_ticks_them(ctx) -> None:
    """Design 13.2: OpenAI's ``/models`` lists everything, chat models included; the names with "embed" in them come first and are ticked, the rest follow, unticked."""
    _to_page_two_openai(ctx, ["gpt-4o", "text-embedding-3-small", "nomic-embed-text", "whisper"])
    assert js(ctx, "rowNames()") == ["text-embedding-3-small", "nomic-embed-text", "gpt-4o", "whisper"]
    assert [ctx.eval(f"checked({json.dumps(n)})") for n in ("text-embedding-3-small", "nomic-embed-text", "gpt-4o", "whisper")] == [True, True, False, False]
    assert ctx.eval("text('pc-register-submit')") == "Register with 2 models" and ctx.eval("disabled('pc-register-submit')") is False
    assert ctx.eval("text('pc-register-back')") == "‹ Back"
    assert ctx.eval("textOf(MR.find('pc-import-head'))") == "Models found at emb✓ Connected; 4 models listed"
    assert "Context window" not in ctx.eval("textOf(MR.find('pc-import'))"), "an embedding model has no context window to ask for"
    ctx.eval("tick('gpt-4o')")
    assert ctx.eval("text('pc-register-submit')") == "Register with 3 models"
    ctx.eval("tick('gpt-4o'); tick('text-embedding-3-small'); tick('nomic-embed-text')")
    assert ctx.eval("text('pc-register-submit')") == "Register with 0 models" and ctx.eval("disabled('pc-register-submit')") is True


def test_one_model_is_said_in_the_singular(ctx) -> None:
    _to_page_two_openai(ctx, ["text-embedding-3-small"])
    assert "✓ Connected; 1 model listed" in js(ctx, "MR.texts()") and ctx.eval("text('pc-register-submit')") == "Register with 1 model"


def test_nothing_with_embed_in_its_name_means_nothing_is_ticked(ctx) -> None:
    _to_page_two_openai(ctx, ["gpt-4o", "whisper"])
    assert js(ctx, "rowNames()") == ["gpt-4o", "whisper"] and ctx.eval("disabled('pc-register-submit')") is True
    assert ctx.eval("text('pc-register-submit')") == "Register with 0 models"


def test_a_model_listed_twice_is_one_row(ctx) -> None:
    _to_page_two_openai(ctx, ["dup", "other-embed", "dup"])
    assert js(ctx, "rowNames()") == ["other-embed", "dup"]
    assert "✓ Connected; 2 models listed" in js(ctx, "MR.texts()")


def test_hub_results_are_ticked_only_when_ok(ctx) -> None:
    """Page two of a Hugging Face provider lists the typed models in the order they were typed, each with the Hub's verdict. A model the Hub confirmed is ticked; one it did not
    is not, but can be (a mirror the API pod cannot reach, a model pulled in by hand: the worker pulls it the first time it is used)."""
    _to_page_two_hub(ctx, "embedding", [(HF_A, "ok"), (HF_B, "not_found"), ("gated/one", "gated")], [HF_A, HF_B, "gated/one"])
    assert js(ctx, "rowNames()") == [HF_A, HF_B, "gated/one"], "the order they were typed in"
    assert [ctx.eval(f"checked({json.dumps(n)})") for n in (HF_A, HF_B, "gated/one")] == [True, False, False]
    assert _status(ctx, HF_A) == "Can be pulled"
    assert _status(ctx, HF_B) == "Not found, or private and the token cannot see it"
    assert _status(ctx, "gated/one").startswith("Gated: accept the model's terms")
    tones = js(ctx, f"[MR.find('pc-import-status:{HF_A}').props['data-tone'], MR.find('pc-import-status:{HF_B}').props['data-tone']]")
    assert tones == ["ok", "error"]
    assert ctx.eval(f"MR.find('pc-import:{HF_B}').props.disabled") is not True, "a model the check could not confirm can still be ticked"
    assert ctx.eval("text('pc-register-submit')") == "Register with 1 model"
    assert ctx.eval("textOf(MR.find('pc-import-head'))") == "Models checked on the Hugging Face Hub1 of 3 models can be pulled"
    assert ctx.eval("headTone()") == "warn", "some models could not be confirmed: the second line is not green"
    ctx.eval(f"tick({json.dumps(HF_B)})")
    assert ctx.eval("text('pc-register-submit')") == "Register with 2 models"
    assert "Hub check" in ctx.eval("textOf(MR.find('pc-import'))"), "the column says what the text in it is"


def test_a_hub_page_where_every_model_passed_says_so_in_green(ctx) -> None:
    _to_page_two_hub(ctx, "embedding", [(HF_A, "ok"), ("BAAI/bge-m3", "ok")], [HF_A, "BAAI/bge-m3"])
    assert ctx.eval("textOf(MR.find('pc-import-head'))") == "Models checked on the Hugging Face Hub✓ 2 of 2 models can be pulled"
    assert ctx.eval("headTone()") == "ok"


def test_a_verdict_the_hub_did_not_give_is_an_error_row_not_a_missing_one(ctx) -> None:
    """The route answers one verdict per name; if one is missing the row is still there, and says so."""
    _open(ctx)
    _type(ctx, "pc-field:type", "huggingface")
    _route(ctx, {BODIES["hub"]: _hub((HF_A, "ok"))})
    _type(ctx, "pc-field:id", "hf-1")
    _fill_models(ctx, HF_A, "BAAI/bge-m3")
    _press(ctx, "pc-register-next")
    assert js(ctx, "rowNames()") == [HF_A, "BAAI/bge-m3"]
    assert ctx.eval("checked('BAAI/bge-m3')") is False and "no answer" in _status(ctx, "BAAI/bge-m3").lower()


def test_the_pair_length_is_shown_on_the_hub_page(ctx) -> None:
    _to_page_two_hub(ctx, "cross_encoder", [("BAAI/bge-reranker-v2-m3", "ok"), ("cross-encoder/ms-marco-MiniLM-L-6-v2", "ok")],
                     ["BAAI/bge-reranker-v2-m3", "cross-encoder/ms-marco-MiniLM-L-6-v2"], max_pair={0: "512"})
    assert "Max pair length 512" in _status(ctx, "BAAI/bge-reranker-v2-m3") and "Max pair length" not in _status(ctx, "cross-encoder/ms-marco-MiniLM-L-6-v2")


# ---- register ----------------------------------------------------------------------------------------------------------------------------------------------


def test_register_saves_the_ticked_models_on_the_provider(ctx) -> None:
    from primer.model.providers.embedding import EmbeddingProvider

    models = ["gpt-4o", "text-embedding-3-small", "nomic-embed-text"]
    _to_page_two_openai(ctx, models, key="sk-typed-1234")
    answer = EmbeddingProvider.model_validate({
        "id": "emb", "provider": "openai", "config": {"url": URL, "flavor": "other", "api_key": "sk-typed-1234"},
        "models": [{"name": "text-embedding-3-small"}, {"name": "nomic-embed-text"}], "limits": {"max_concurrency": 1}}).model_dump(mode="json")
    assert answer["config"]["api_key"] != "sk-typed-1234", "the served row masks its key"
    _route(ctx, {BODIES["create"]: {"answer": answer}})
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert _requests(ctx) == [("POST", "/embedding_providers/_discover_models"), ("POST", "/embedding_providers")]
    (made,) = _calls(ctx, "POST", "/embedding_providers")
    assert made["body"] == {
        "id": "emb", "provider": "openai", "config": {"url": URL, "flavor": "other", "api_key": "sk-typed-1234"},
        "models": [{"name": "text-embedding-3-small"}, {"name": "nomic-embed-text"}], "limits": {"max_concurrency": 1},
    }, "the ticked models, in the order shown, as {name}; limits are the ones the kind shows"
    _validate("embedding", made["body"])
    assert js(ctx, "SAVED") == [answer] and ctx.eval("CLOSED") == 1
    assert "sk-typed-1234" not in json.dumps(js(ctx, "SAVED")), "the caller is told about the row the server answered, never the request"


def test_the_limits_sent_are_the_ones_the_kind_shows(ctx) -> None:
    """A limit typed for OpenAI's connect timeout is not sent for a Hugging Face provider the operator switched to: its adapter never reads it."""
    _open(ctx)
    _type(ctx, "pc-field:id", "emb")
    _type(ctx, "pc-field:url", URL)
    _click(ctx, "pc-limits-more:toggle")
    _type(ctx, "pc-limit:connect_timeout_seconds", "7")
    _type(ctx, "pc-limit:max_concurrency", "3")
    _type(ctx, "pc-field:type", "huggingface")
    _route(ctx, {BODIES["hub"]: _hub((HF_A, "ok"))})
    _fill_models(ctx, HF_A)
    _press(ctx, "pc-register-next")
    _press(ctx, "pc-register-submit")
    (made,) = _calls(ctx, "POST", "/embedding_providers")
    assert made["body"]["limits"] == {"max_concurrency": 3}
    _validate("embedding", made["body"])


def test_a_hugging_face_provider_with_no_token_still_posts_a_config(ctx) -> None:
    """The token is optional, and the server answers ``body.config: Field required`` to a body with no ``config`` at all (the lead's note on #758): an untouched token box
    posts ``config: {}``, for an embedding provider and for a cross encoder."""
    _to_page_two_hub(ctx, "embedding", [(HF_A, "ok")], [HF_A])
    _press(ctx, "pc-register-submit")
    (made,) = _calls(ctx, "POST", "/embedding_providers")
    assert made["body"] == {"id": "hf-1", "provider": "huggingface", "config": {}, "models": [{"name": HF_A}], "limits": {"max_concurrency": 1}}
    _validate("embedding", made["body"])
    _to_page_two_hub(ctx, "cross_encoder", [("BAAI/bge-reranker-v2-m3", "ok")], ["BAAI/bge-reranker-v2-m3"])
    _press(ctx, "pc-register-submit")
    (made,) = _calls(ctx, "POST", "/cross_encoder_providers")
    assert made["body"]["config"] == {} and "config" in made["body"]
    _validate("cross_encoder", made["body"])


def test_a_typed_token_goes_with_the_provider(ctx) -> None:
    _to_page_two_hub(ctx, "embedding", [(HF_A, "ok")], [HF_A], token="hf_secret_1234")
    _press(ctx, "pc-register-submit")
    assert _calls(ctx, "POST", "/embedding_providers")[0]["body"]["config"] == {"token": "hf_secret_1234"}


def test_gemini_registers_with_its_key_and_no_url(ctx) -> None:
    _open(ctx)
    _type(ctx, "pc-field:type", "gemini")
    _route(ctx, {BODIES["discover"]: _listed("gemini-embedding-001", "other-model")})
    _type(ctx, "pc-field:id", "gem")
    _type(ctx, "pc-field:api_key", "AIza-test-KEY-9876")
    _press(ctx, "pc-register-next")
    assert js(ctx, "rowNames()") == ["gemini-embedding-001", "other-model"] and ctx.eval("checked('gemini-embedding-001')") is True
    _press(ctx, "pc-register-submit")
    (made,) = _calls(ctx, "POST", "/embedding_providers")
    assert made["body"] == {"id": "gem", "provider": "gemini", "config": {"api_key": "AIza-test-KEY-9876"}, "models": [{"name": "gemini-embedding-001"}],
                            "limits": {"max_concurrency": 1}}
    _validate("embedding", made["body"])


def test_cross_encoder_sends_max_pair_length_only_when_set(ctx) -> None:
    names = ["BAAI/bge-reranker-v2-m3", "cross-encoder/ms-marco-MiniLM-L-6-v2"]
    _to_page_two_hub(ctx, "cross_encoder", [(names[0], "ok"), (names[1], "ok")], names, max_pair={0: "512"})
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    (made,) = _calls(ctx, "POST", "/cross_encoder_providers")
    assert made["body"] == {"id": "hf-1", "provider": "huggingface", "config": {}, "models": [{"name": names[0], "max_pair_length": 512}, {"name": names[1]}],
                            "limits": {"max_concurrency": 1}}
    assert isinstance(made["body"]["models"][0]["max_pair_length"], int), "a number, not the text that was typed"
    parsed = _validate("cross_encoder", made["body"])
    assert [m.max_pair_length for m in parsed.models] == [512, None]
    assert ctx.eval("CLOSED") == 1


def test_a_model_left_unticked_is_not_registered(ctx) -> None:
    _to_page_two_hub(ctx, "embedding", [(HF_A, "ok"), (HF_B, "not_found")], [HF_A, HF_B])
    _press(ctx, "pc-register-submit")
    assert [m["name"] for m in _calls(ctx, "POST", "/embedding_providers")[0]["body"]["models"]] == [HF_A]


def test_a_refused_register_stays_on_page_two_with_the_reason_and_can_be_pressed_again(ctx, envelopes) -> None:
    _to_page_two_openai(ctx, ["text-embedding-3-small"])
    _route(ctx, {BODIES["create"]: [{"refuse": envelopes["provider_taken"]}, {"answer": {"id": "emb-2"}}]})
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert "already exists" in _error(ctx) and ctx.eval("MR.find('pc-models-pick') !== null") is True
    assert ctx.eval("CLOSED") == 0 and js(ctx, "SAVED") == []
    assert ctx.eval("ariaBusy('pc-register-submit')") is False and ctx.eval("disabled('pc-register-submit')") is False, "it works again"
    # Back reaches the id; a fixed attempt goes on, and the list and the ticks were kept
    _click(ctx, "pc-register-back")
    assert ctx.eval("value('pc-field:id')") == "emb"
    _type(ctx, "pc-field:id", "emb-2")
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert len(_calls(ctx, "POST", "/embedding_providers/_discover_models")) == 1, "the same connection is not asked again"
    assert ctx.eval("checked('text-embedding-3-small')") is True
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert [c["body"]["id"] for c in _calls(ctx, "POST", "/embedding_providers")] == ["emb", "emb-2"]
    assert ctx.eval("CLOSED") == 1 and js(ctx, "SAVED") == [{"id": "emb-2"}]


def test_a_second_click_while_registering_posts_nothing_more(ctx) -> None:
    _to_page_two_openai(ctx, ["text-embedding-3-small"])
    _route(ctx, {BODIES["create"]: {"hold": "create"}})
    ctx.eval("press('pc-register-submit')")
    settle(ctx)
    assert ctx.eval("ariaBusy('pc-register-submit')") is True, "the button says its request is out, and keeps its focus"
    for _ in range(2):
        ctx.eval("press('pc-register-submit')")
        settle(ctx)
    assert len(_calls(ctx, "POST", "/embedding_providers")) == 1
    assert ctx.eval("ariaDisabled('pc-register-back')") is True, "and Back waits for the answer"
    ctx.eval("HELD.create.ok({ id: 'emb' }); void 0;")
    settle(ctx)
    assert ctx.eval("CLOSED") == 1 and len(js(ctx, "SAVED")) == 1


def test_the_answer_of_a_save_the_server_gives_no_row_for_hands_the_caller_the_id_only(ctx) -> None:
    _to_page_two_openai(ctx, ["text-embedding-3-small"], key="sk-typed-1234")
    ctx.eval("HANDLER = function () { return Promise.resolve(null); };")
    _press(ctx, "pc-register-submit")
    assert js(ctx, "SAVED") == [{"id": "emb"}], "never the request body, which holds the typed key"


# ---- failures and the connection ---------------------------------------------------------------------------------------------------------------------------


def test_a_failed_listing_stays_on_page_one(ctx, envelopes) -> None:
    """A refusal or an empty list leaves the operator on page one, with the reason on the overlay's error line. There is no "Register without models" here: the ``models``
    list of an embedding provider needs at least one entry."""
    _open(ctx)
    _route(ctx, {BODIES["discover"]: [{"refuse": envelopes["probe_failed"]}, {"answer": {"models": []}}, _listed("text-embedding-3-small")]})
    _fill_openai(ctx)
    assert _press(ctx, "pc-register-next") == "fulfilled", "the click handler's promise never rejects"
    assert ctx.eval("MR.find('pc-models-settings') !== null && MR.find('pc-models-pick') === null") is True, "still page one"
    reason = ctx.eval(f"window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(envelopes['probe_failed'])})).message")
    assert "All connection attempts failed" in reason and _error(ctx) == reason
    assert ctx.eval("MR.find('pc-register-error').props.role") == "alert"
    assert ctx.eval("MR.find('pc-register-without-models') === null") is True, "an embedding provider cannot be registered without a model"
    assert ctx.eval("ariaBusy('pc-register-next')") is False, "Next retries"
    # an empty list is a reason of its own
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert _error(ctx) == "The provider answered but listed no models." and ctx.eval("MR.find('pc-models-pick') === null") is True
    # and the next try works, and clears the line
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert ctx.eval("MR.find('pc-models-pick') !== null") is True and _error(ctx) is None


def test_a_refused_hub_check_stays_on_page_one(ctx, envelopes) -> None:
    _open(ctx, "cross_encoder")
    _route(ctx, {BODIES["hub"]: {"refuse": envelopes["broke"]}})
    _type(ctx, "pc-field:id", "ce-1")
    _fill_models(ctx, "BAAI/bge-reranker-v2-m3")
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert ctx.eval("MR.find('pc-models-pick') === null") is True and _error(ctx)
    assert ctx.eval("value('pc-models:0:name')") == "BAAI/bge-reranker-v2-m3", "what was typed is still there"


def test_the_hub_is_asked_again_every_time_next_is_pressed(ctx) -> None:
    """A verdict can change while the operator is away (a gated model's terms accepted on the Hub): Back and Next asks again. Ticks are the new verdicts'."""
    _to_page_two_hub(ctx, "embedding", [(HF_A, "gated")], [HF_A])
    assert ctx.eval("checked(" + json.dumps(HF_A) + ")") is False
    _click(ctx, "pc-register-back")
    _route(ctx, {BODIES["hub"]: _hub((HF_A, "ok"))})
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert len(_calls(ctx, "POST", "/huggingface/_check_models")) == 2
    assert ctx.eval("checked(" + json.dumps(HF_A) + ")") is True and _status(ctx, HF_A) == "Can be pulled"


def test_back_returns_to_page_one_with_the_draft_and_keeps_the_list_for_the_same_connection(ctx) -> None:
    _to_page_two_openai(ctx, ["text-embedding-3-small", "other-embed", "gpt-4o"], key="sk-1")
    ctx.eval("tick('other-embed')")
    _click(ctx, "pc-register-back")
    assert ctx.eval("MR.find('pc-models-settings') !== null && MR.find('pc-models-pick') === null") is True
    assert ctx.eval("value('pc-field:id')") == "emb" and ctx.eval("value('pc-field:url')") == URL and ctx.eval("value('pc-field:api_key')") == "sk-1"
    _type(ctx, "pc-limit:max_concurrency", "3")
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert len(_calls(ctx, "POST", "/embedding_providers/_discover_models")) == 1, "the same connection is not asked again"
    assert ctx.eval("checked('other-embed')") is False and ctx.eval("checked('text-embedding-3-small')") is True, "and what was ticked is kept"
    _click(ctx, "pc-register-back")
    _type(ctx, "pc-field:url", "http://other.test/v1")
    _route(ctx, {BODIES["discover"]: _listed("z-embed")})
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert len(_calls(ctx, "POST", "/embedding_providers/_discover_models")) == 2, "another connection is asked"
    assert js(ctx, "rowNames()") == ["z-embed"]


def test_a_second_next_while_the_provider_is_answering_asks_nothing_more(ctx) -> None:
    _open(ctx)
    _route(ctx, {BODIES["discover"]: {"hold": "discover"}})
    _fill_openai(ctx)
    for _ in range(3):
        ctx.eval("press('pc-register-next')")
        settle(ctx)
    assert len(_calls(ctx, "POST", "/embedding_providers/_discover_models")) == 1
    ctx.eval("HELD.discover.ok({ models: [] }); void 0;")
    settle(ctx)


def test_a_discovery_answer_for_a_connection_that_has_since_changed_is_not_used(ctx) -> None:
    """A URL fixed while the first request was still out: its answer is for the old address. Showing it would register the new address with the old server's models."""
    _open(ctx)
    _route(ctx, {BODIES["discover"]: {"hold": "old"}})
    _fill_openai(ctx)
    ctx.eval("press('pc-register-next')")
    settle(ctx)
    _type(ctx, "pc-field:url", "http://fixed.test/v1")
    ctx.eval("HELD.old.ok({ models: [{ name: 'from-the-old-server-embed' }] }); void 0;")
    settle(ctx)
    assert ctx.eval("MR.find('pc-models-pick') === null") is True, "the old list is not shown"
    assert "changed while the provider was answering" in _error(ctx)
    _route(ctx, {BODIES["discover"]: _listed("from-the-new-server-embed")})
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert js(ctx, "rowNames()") == ["from-the-new-server-embed"]


def test_a_hub_answer_for_models_that_have_since_changed_is_not_used(ctx) -> None:
    _open(ctx)
    _type(ctx, "pc-field:type", "huggingface")
    _route(ctx, {BODIES["hub"]: {"hold": "old"}})
    _type(ctx, "pc-field:id", "hf-1")
    _fill_models(ctx, HF_A)
    ctx.eval("press('pc-register-next')")
    settle(ctx)
    _type(ctx, "pc-models:0:name", "BAAI/bge-m3")
    ctx.eval("HELD.old.ok(" + json.dumps(_hub((HF_A, "ok"))["answer"]) + "); void 0;")
    settle(ctx)
    assert ctx.eval("MR.find('pc-models-pick') === null") is True and "changed while" in _error(ctx)


def test_changing_the_type_starts_its_config_over_and_forgets_a_failed_listing(ctx, envelopes) -> None:
    _open(ctx)
    _route(ctx, {BODIES["discover"]: {"refuse": envelopes["probe_failed"]}})
    _fill_openai(ctx, key="sk-old")
    _press(ctx, "pc-register-next")
    assert _error(ctx)
    _type(ctx, "pc-field:type", "gemini")
    assert _error(ctx) is None, "the failure was about another connection"
    assert ctx.eval("value('pc-field:api_key')") == "" and ctx.eval("MR.find('pc-field:url') === null") is True
    _type(ctx, "pc-field:type", "openai")
    assert ctx.eval("value('pc-field:url')") == "" and ctx.eval("value('pc-field:flavor')") == "other"
    assert ctx.eval("value('pc-field:id')") == "emb", "the id is the page's, not the kind's"


def test_the_kinds_a_caller_excludes_are_not_offered(ctx) -> None:
    _open(ctx, excludeKinds=["gemini"])
    assert js(ctx, "options('pc-field:type')") == ["openai", "huggingface"]


# ---- editing -----------------------------------------------------------------------------------------------------------------------------------------------


def _embedding_row(kind: str = "openai", provider_id: str = "p") -> dict:
    """An embedding provider as the server serves it: the secret masked, every limit set, a flavor the page shows and fields it does not."""
    from primer.model.providers.embedding import EmbeddingProvider

    config = {
        "openai": {"url": URL, "flavor": "lmstudio", "api_key": "sk-emb-abcd1234"},
        "gemini": {"api_key": "AIza-embed-abcd1234"},
        "huggingface": {"token": "hf_secret_abcd1234"},
    }[kind]
    row = EmbeddingProvider.model_validate({
        "id": provider_id, "provider": kind, "config": config, "models": [{"name": "text-embedding-3-small"}, {"name": "nomic-embed-text"}],
        "limits": {"max_concurrency": 3, "request_timeout_seconds": 120.0, "connect_timeout_seconds": 5.0, "total_timeout_seconds": 600.0,
                   "max_retries": 4, "retry_backoff_seconds": 1.0, "retry_backoff_max_seconds": 16.0},
    }).model_dump(mode="json")
    secret = next(v for k, v in row["config"].items() if k in ("api_key", "token"))
    assert secret.startswith("**********") and not secret.endswith(config.get("api_key", config.get("token", ""))), "the served row masks its secret"
    assert set(row["limits"]) == {"max_concurrency", "request_timeout_seconds", "connect_timeout_seconds", "total_timeout_seconds", "max_retries",
                                  "retry_backoff_seconds", "retry_backoff_max_seconds"}
    return row


def _cross_encoder_row(provider_id: str = "ce") -> dict:
    from primer.model.providers.cross_encoder import CrossEncoderProvider

    return CrossEncoderProvider.model_validate({
        "id": provider_id, "provider": "huggingface", "config": {"token": "hf_ce_abcd1234"},
        "models": [{"name": "BAAI/bge-reranker-v2-m3", "max_pair_length": 512}, {"name": "cross-encoder/ms-marco-MiniLM-L-6-v2"}],
        "limits": {"max_concurrency": 2, "request_timeout_seconds": 90.0},
    }).model_dump(mode="json")


def test_edit_round_trips_every_unshown_field(ctx) -> None:
    """Master Review Focus 1. The page shows some of a row; the PUT carries all of it: the masked key as served, the flavor, every limit the kind does not show, the models, and
    a field no version of the page knows (kept, not dropped); only what the operator changed differs."""
    row = _embedding_row()
    row["x_future"] = {"kept": True}
    row["config"]["x_future_knob"] = 7
    _open(ctx, editRow=row)
    assert ctx.eval("textOf(MR.find('pc-register-submit'))") == "Save"
    _click(ctx, "pc-limits-more:toggle")
    _type(ctx, "pc-limit:connect_timeout_seconds", "9")
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    (call,) = [c for c in js(ctx, "CALLS") if c["method"] != "GET"]
    expected = json.loads(json.dumps(row))
    expected["limits"]["connect_timeout_seconds"] = 9
    assert call == {"method": "PUT", "path": "/embedding_providers/p", "body": expected}
    assert ctx.eval("CLOSED") == 1 and js(ctx, "SAVED") == [expected]


def test_edit_shows_the_row_and_sends_the_row_back_untouched(ctx) -> None:
    """Pressing Save with nothing changed sends the served row as it was: not one field is added, dropped or turned into text."""
    row = _embedding_row()
    _open(ctx, editRow=row)
    assert ctx.eval("value('pc-field:url')") == URL and ctx.eval("value('pc-field:flavor')") == "lmstudio"
    assert ctx.eval("value('pc-field:api_key')") == row["config"]["api_key"], "the box shows the mask as served"
    assert "Stored secrets are shown masked; leave it as is to keep it." in _help(ctx, "pc-field:api_key")
    assert ctx.eval("value('pc-limit:max_concurrency')") == "3"
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert _calls(ctx, "PUT")[0]["body"] == row


def test_edit_shows_the_models_table_for_every_kind_prefilled(ctx) -> None:
    """In an edit the models are the row's own, as a table of every kind (OpenAI and Gemini included: the probe is a create's step, not an edit's)."""
    for kind in ("openai", "gemini", "huggingface"):
        _open(ctx, editRow=_embedding_row(kind))
        assert [ctx.eval(f"value('pc-models:{i}:name')") for i in (0, 1)] == ["text-embedding-3-small", "nomic-embed-text"], kind
        assert ctx.eval("MR.find('pc-models:2:name') === null") is True


def test_edit_models_can_be_added_removed_and_blank_rows_are_dropped(ctx) -> None:
    row = _embedding_row()
    _open(ctx, editRow=row)
    _click(ctx, "pc-models:remove:0")
    _click(ctx, "pc-models:add")
    _type(ctx, "pc-models:1:name", "  new-embed  ")
    _click(ctx, "pc-models:add")
    assert ctx.eval("disabled('pc-register-submit')") is False, "a blank row does not stop a save: it is dropped"
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert _calls(ctx, "PUT")[0]["body"]["models"] == [{"name": "nomic-embed-text"}, {"name": "new-embed"}]
    _validate("embedding", _calls(ctx, "PUT")[0]["body"])


def test_edit_is_off_when_no_model_is_left(ctx) -> None:
    _open(ctx, editRow=_embedding_row())
    _click(ctx, "pc-models:remove:1")
    _click(ctx, "pc-models:remove:0")
    assert ctx.eval("disabled('pc-register-submit')") is True
    _click(ctx, "pc-models:add")
    assert ctx.eval("disabled('pc-register-submit')") is True, "a blank row is not a model"
    _type(ctx, "pc-models:0:name", "x-embed")
    assert ctx.eval("disabled('pc-register-submit')") is False


def test_edit_keeps_the_masked_token_as_served_and_a_new_one_replaces_it(ctx) -> None:
    row = _embedding_row("huggingface")
    mask = row["config"]["token"]
    _open(ctx, editRow=row)
    assert ctx.eval("value('pc-field:token')") == mask
    _press(ctx, "pc-register-submit")
    assert _calls(ctx, "PUT")[0]["body"]["config"] == {"token": mask}, "the server restores the stored token from its own mask"
    _type(ctx, "pc-field:token", "hf_new")
    _press(ctx, "pc-register-submit")
    assert _calls(ctx, "PUT")[1]["body"]["config"] == {"token": "hf_new"}
    _type(ctx, "pc-field:token", "")
    _press(ctx, "pc-register-submit")
    assert _calls(ctx, "PUT")[2]["body"]["config"] == {}, "a cleared optional token is left out (ticket T3), never sent as an empty string; the key stays so the server takes it"


def test_edit_a_cross_encoder_round_trips_its_pair_lengths(ctx) -> None:
    row = _cross_encoder_row()
    _open(ctx, "cross_encoder", editRow=row)
    assert [ctx.eval(f"value('pc-models:{i}:max_pair_length')") for i in (0, 1)] == ["512", ""]
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert _calls(ctx, "PUT")[0]["body"] == row, "a model with no pair length keeps the null it was served with"
    _type(ctx, "pc-models:0:max_pair_length", "1024")
    _type(ctx, "pc-models:1:max_pair_length", "256")
    _press(ctx, "pc-register-submit")
    assert [m.get("max_pair_length") for m in _calls(ctx, "PUT")[1]["body"]["models"]] == [1024, 256]
    _type(ctx, "pc-models:0:max_pair_length", "")
    _press(ctx, "pc-register-submit")
    assert _calls(ctx, "PUT")[2]["body"]["models"][0]["max_pair_length"] is None, "a cleared length is no length"
    _validate("cross_encoder", _calls(ctx, "PUT")[2]["body"])


def test_edit_keeps_a_field_of_a_model_the_page_does_not_know(ctx) -> None:
    row = _embedding_row()
    row["models"][0]["x_dimensions"] = 1536
    _open(ctx, editRow=row)
    _type(ctx, "pc-models:1:name", "renamed-embed")
    _press(ctx, "pc-register-submit")
    models = _calls(ctx, "PUT")[0]["body"]["models"]
    assert models == [{"name": "text-embedding-3-small", "x_dimensions": 1536}, {"name": "renamed-embed"}]


def test_edit_has_no_next_and_probes_nothing(ctx) -> None:
    """Review Focus 4: in edit mode there is nothing to probe with a draft. The draft holds the MASK of the key or token, and a probe would authenticate with ``**********``.
    The id and the type are the row's, and the footer is Cancel, Delete and Save."""
    for klass, row in (("embedding", _embedding_row()), ("embedding", _embedding_row("huggingface")), ("cross_encoder", _cross_encoder_row())):
        _open(ctx, klass, editRow=row)
        assert ctx.eval("MR.find('pc-register-next') === null && MR.find('pc-register-back') === null") is True
        assert ctx.eval("MR.find('pc-field:id').props.readOnly") is True and ctx.eval("value('pc-field:id')") == row["id"]
        footer = js(ctx, "testids('pc-register')")
        assert [t for t in footer if t in ("pc-register-cancel", "pc-register-delete", "pc-register-submit")] == ["pc-register-cancel", "pc-register-delete", "pc-register-submit"]
        _press(ctx, "pc-register-submit")
        assert not any("_discover_models" in path or "_check_models" in path or path.endswith("/_test") for _, path in _requests(ctx)), klass
        assert {method for method, _ in _requests(ctx)} == {"PUT"}


def test_edit_locks_the_type_and_titles_the_overlay(ctx) -> None:
    _open(ctx, editRow=_embedding_row())
    assert ctx.eval("MR.find('pc-field:type').props.disabled") is True and ctx.eval("value('pc-field:type')") == "openai"
    assert js(ctx, "options('pc-field:type')") == ["openai"], "the select names the row's own kind"
    assert "Edit p" in js(ctx, "MR.texts()")
    _click(ctx, "pc-register-cancel")
    assert ctx.eval("CLOSED") == 1 and _requests(ctx) == []


def test_a_built_in_row_is_edited_like_any_other_but_cannot_be_deleted(ctx) -> None:
    row = _embedding_row("huggingface", provider_id="huggingface")
    _open(ctx, editRow=row)
    assert ctx.eval("MR.find('pc-register-delete') === null") is True and ctx.eval("MR.find('pc-register-submit') !== null") is True
    assert ctx.eval("MR.find('pc-register-invalidate') !== null") is True, "the shell's Invalidate is there for a class that caches adapters"


def test_a_refused_save_stays_open_with_the_reason(ctx, envelopes) -> None:
    _open(ctx, editRow=_embedding_row())
    _route(ctx, {"PUT /embedding_providers/p": {"refuse": envelopes["put_422"]}})
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    reason = ctx.eval(f"window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(envelopes['put_422'])})).message")
    assert reason and _error(ctx) == reason
    assert ctx.eval("CLOSED") == 0 and js(ctx, "SAVED") == []
    assert ctx.eval("ariaBusy('pc-register-submit')") is False and ctx.eval("disabled('pc-register-submit')") is False, "Save works again"


def test_a_row_of_a_kind_the_install_does_not_serve_cannot_be_saved(ctx) -> None:
    row = _embedding_row()
    row["provider"] = "no-such-kind"
    _open(ctx, editRow=row)
    assert "does not serve the kind" in ctx.eval("textOf(MR.find('pc-register-kind-missing'))")
    assert ctx.eval("disabled('pc-register-submit')") is True


# ---- the page in the overlay -------------------------------------------------------------------------------------------------------------------------------

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
function mountPage(key, props) {
  MR.mount(function Host(p) {
    return el(PC_ModelsRegister, Object.assign({
      klass: PC_classByKey(key), editRow: null, excludeKinds: null, frame: frameFn, blocked: false,
      onSaved: function (row, words) { SAVED.push(row); WORDS.push(words === undefined ? null : words); }, onClose: function () { CLOSED += 1; },
    }, p));
  }, props || {});
}
"""


def _page(ctx, klass: str = "embedding", **props) -> None:
    ctx.eval(PAGE_STUBS)
    ctx.eval(f"mountPage({json.dumps(klass)}, {json.dumps(props)});")
    _serve(ctx, klass)


def test_the_page_is_the_page_of_the_embedding_and_cross_encoder_classes(ctx) -> None:
    assert ctx.eval("window.PC_REGISTER_PAGES.embedding === PC_ModelsRegister && window.PC_REGISTER_PAGES.cross_encoder === PC_ModelsRegister") is True
    assert ctx.eval("window.PC_ModelsRegister === PC_ModelsRegister") is True
    assert ctx.eval("typeof window.PC_REGISTER_PAGES.tts") == "undefined", "the merged registry keeps what other files put in it and adds nothing of theirs"


def test_a_save_hands_its_own_words_to_on_saved(ctx) -> None:
    """``onSaved(row, words)``: the overlay toasts the words the page gives it ("<id>: 2 models"), and tells the caller only the row."""
    _page(ctx)
    _route(ctx, {BODIES["discover"]: _listed("a-embed", "b-embed"), BODIES["create"]: {"answer": {"id": "emb", "made": True}}})
    _fill_openai(ctx)
    _press(ctx, "pc-register-next")
    _press(ctx, "pc-register-submit")
    assert js(ctx, "SAVED") == [{"id": "emb", "made": True}], "the row the server answered"
    assert js(ctx, "WORDS") == [{"detail": "emb: 2 models"}]


def test_a_save_of_one_model_is_said_in_the_singular(ctx) -> None:
    _page(ctx)
    _route(ctx, {BODIES["discover"]: _listed("a-embed")})
    _fill_openai(ctx)
    _press(ctx, "pc-register-next")
    _press(ctx, "pc-register-submit")
    assert js(ctx, "WORDS") == [{"detail": "emb: 1 model"}]


def test_a_blocked_page_holds_every_action_off(ctx) -> None:
    """A page is handed ``blocked`` (the overlay's Delete is out): every action it has is held off (aria-disabled, not busy), and a press sends nothing."""
    _page(ctx, editRow=_embedding_row())
    ctx.eval("MR.rerender({ blocked: true, klass: PC_classByKey('embedding'), editRow: " + json.dumps(_embedding_row()) + ", excludeKinds: null, frame: frameFn,"
             " onSaved: function () {}, onClose: function () {} });")
    assert ctx.eval("ariaDisabled('pc-register-submit')") is True and ctx.eval("ariaBusy('pc-register-submit')") is False
    _press(ctx, "pc-register-submit")
    assert js(ctx, "CALLS") == []


def test_a_blocked_page_holds_next_off_too(ctx) -> None:
    _page(ctx)
    _route(ctx, {BODIES["discover"]: _listed("a-embed")})
    _fill_openai(ctx)
    ctx.eval("MR.rerender({ blocked: true, klass: PC_classByKey('embedding'), editRow: null, excludeKinds: null, frame: frameFn, onSaved: function () {}, onClose: function () {} });")
    assert ctx.eval("ariaDisabled('pc-register-next')") is True and ctx.eval("ariaBusy('pc-register-next')") is False
    _press(ctx, "pc-register-next")
    assert js(ctx, "CALLS") == []


def test_the_selector_is_locked_while_a_request_is_out(ctx) -> None:
    _open(ctx)
    _route(ctx, {BODIES["discover"]: {"hold": "discover"}})
    _fill_openai(ctx)
    assert ctx.eval("MR.find('pc-register-type').props['aria-disabled'] === undefined") is True
    ctx.eval("press('pc-register-next')")
    settle(ctx)
    assert ctx.eval("MR.find('pc-register-type').props['aria-disabled']") == "true" and ctx.eval("ariaBusy('pc-register-next')") is True
    ctx.eval("HELD.discover.ok({ models: [{ name: 'm-embed' }] }); void 0;")
    settle(ctx)
    assert ctx.eval("MR.find('pc-register-type').props['aria-disabled'] === undefined") is True and ctx.eval("MR.find('pc-models-pick') !== null") is True


def test_cancel_closes_the_overlay(ctx) -> None:
    _open(ctx)
    _click(ctx, "pc-register-cancel")
    assert ctx.eval("CLOSED") == 1 and _requests(ctx) == [] and js(ctx, "SAVED") == []


def test_the_page_says_while_the_kinds_are_loading_and_offers_no_next(ctx) -> None:
    ctx.eval("mountOverlay({ initialClass: 'embedding' });")
    assert ctx.eval("textOf(MR.find('pc-register-loading'))") == "Loading the fields for this kind…"
    assert ctx.eval("MR.find('pc-register-next') !== null && disabled('pc-register-next')") is True, "the footer is there while the body loads"
    _serve(ctx)
    assert ctx.eval("MR.find('pc-register-loading') === null") is True


def test_a_failed_kinds_request_is_an_alert_with_a_try_again_that_works(ctx, envelopes) -> None:
    key = "PC_typesKey('embedding_providers')"
    ctx.eval("mountOverlay({ initialClass: 'embedding' });")
    ctx.eval(f"fail({key}, {json.dumps(envelopes['put_422'])}); MR.rerender();")
    assert ctx.eval("MR.find('pc-register-types-error').props.role") == "alert"
    assert "Could not load the fields for this kind" in ctx.eval("textOf(MR.find('pc-register-types-error'))")
    assert ctx.eval("disabled('pc-register-next')") is True
    refetched = ctx.eval(f"RES[{key}].refetched")
    _click(ctx, "pc-register-types-retry")
    assert ctx.eval(f"RES[{key}].refetched") == refetched + 1
    before = ctx.eval("MR.findProp('to', 'first').props.n")
    _serve(ctx)
    assert ctx.eval("MR.find('pc-register-types-error') === null && MR.find('pc-field:url') !== null") is True
    assert ctx.eval("MR.findProp('to', 'first').props.n") == before + 1, "the focus is handed to the first field, which is drawn now"


def test_the_pages_work_on_a_phone_sheet(ctx) -> None:
    ctx.eval("MOBILE = true;")
    _to_page_two_openai(ctx, ["text-embedding-3-small"])
    assert ctx.eval("MR.find('pc-register').props.className").split() == ["sheet", "pk-overlay"]
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert [m["name"] for m in _calls(ctx, "POST", "/embedding_providers")[0]["body"]["models"]] == ["text-embedding-3-small"] and ctx.eval("CLOSED") == 1


# ---- the focus ---------------------------------------------------------------------------------------------------------------------------------------------


def test_a_page_change_asks_for_the_focus_to_be_handed_over(ctx) -> None:
    """The button that took the page forward or back is gone when the other page is drawn. The page asks its body (the only thing that can act when the body has been drawn)
    for a hand-off: to the heading of the models page going forward, to the first field going back."""
    _open(ctx)
    assert ctx.eval("MR.findProp('to', 'first').props.n") == 0, "opening is the dialog's own business: it takes the focus in"
    _route(ctx, {BODIES["discover"]: _listed("m-embed")})
    _fill_openai(ctx)
    _press(ctx, "pc-register-next")
    head = ctx.eval("MR.findProp('to', 'head').props.n")
    assert head >= 1 and ctx.eval("MR.find('pc-import-head').props.tabIndex") == -1, "the heading can take the focus"
    _click(ctx, "pc-register-back")
    assert ctx.eval("MR.findProp('to', 'first').props.n") == head + 1


def test_a_button_that_is_gone_is_not_reused_as_another_action(ctx) -> None:
    """React keeps the DOM node of an element with the same type and key at the same place. The Back button of the models page and the Cancel of the connection page are both
    the first thing on the left of the footer: without keys of their own they would be ONE node, and a focus left on Back would stay on a button now called Cancel, where a
    second Enter closes the overlay. The footer's elements differ in key from page to page, so a page change replaces them (and the hand-off finds the focus lost)."""
    _page(ctx)
    _route(ctx, {BODIES["discover"]: _listed("m-embed")})
    _fill_openai(ctx)
    one = js(ctx, "[LAST.footerLeft.key, LAST.footerRight.key]")
    _press(ctx, "pc-register-next")
    two = js(ctx, "[LAST.footerLeft.key, LAST.footerRight.key]")
    assert all(one) and all(two), (one, two)
    assert one[0] != two[0] and one[1] != two[1], "page one's Cancel and Next are not page two's Back and Register"
    _click(ctx, "pc-register-back")
    assert js(ctx, "[LAST.footerLeft.key, LAST.footerRight.key]") == one, "and Back brings page one's back"


# ---- the unit pieces ---------------------------------------------------------------------------------------------------------------------------------------


def test_the_index_loads_the_models_page_after_the_overlay_and_before_the_catalog() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    tag = 'src="components/providers/register-models.jsx"'
    assert html.count(tag) == 1
    assert html.index(tag) > html.index('src="components/providers/register-overlay.jsx"')
    assert html.index(tag) > html.index('src="components/providers/register-common.jsx"')
    assert html.index(tag) < html.index('src="components/provider-catalog.jsx"')


def test_the_page_keeps_every_rule_of_the_overlays_pages() -> None:
    """The source rules of ``register-overlay.jsx``: no effect of the page reads or focuses body DOM, the page provides no context, and it returns ``frame(...)`` in every state."""
    src = MODELS_FILE.read_text(encoding="utf-8")
    assert "createContext" not in src and "Provider value" not in src and ".Provider" not in src
    assert src.count("return frame(") + src.count("return frame({") >= 2, "every state returns the frame"
    assert "document.activeElement" not in src and ".focus()" not in src, "the focus is handed over by PC_PageFocus, a component inside the body"
    assert "PC_PageFocus" in src
    assert "startTransition" not in src and "useDeferredValue" not in src and "Suspense" not in src
