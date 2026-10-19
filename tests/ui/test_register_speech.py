"""The ASR and TTS pages of the Register provider overlay (plan 02, Task 15; design 6.6 and 13.1; master Review Focus 1 and 4).

A speech provider is registered in two pages that need only a URL and a key: the connection (id, URL, key, and for TTS the default model; the limits the adapter reads), whose
"Next" tests the connection with those two fields alone (``POST /stt_providers/_test`` and ``/tts_providers/_test`` accept a partial draft), and the page that picks ONE model (ASR,
saved as ``default_model``) or ONE voice (TTS, saved as ``default_voice``) from what the server listed, as a radio list. Editing is the first page again with the id locked, the
default model (and for TTS the default voice) as text, and "Save".

Everything runs for real in V8 (``tests/ui/_register_harness.py``): the real kit, form, foundation and overlay, the real ``register-speech.jsx``, the ``_types`` maps the route
functions answer, the bodies the page sends are taken by the server's own models and by the real ``_test`` route functions, and the rows it edits are built by the server's own
models. Only the network is a script: ``route`` says what each ``METHOD /path`` answers.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._register_harness import COMPONENTS, UI, base_code, context, js, settle, transpiled, types_of

SPEECH_FILE = COMPONENTS / "providers" / "register-speech.jsx"

PLURALS = {"stt": "stt_providers", "tts": "tts_providers"}
URLS = {"stt": "http://asr.test:8006/v1", "tts": "http://tts.test:8004/v1"}
CHOICES = {"stt": ["whisper-1", "large-v3"], "tts": ["alloy", "af_heart"]}

STUBS = r"""
var SAVED = [], WORDS = [], CLOSED = 0;
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
function limitInputs() { return testids("pc-limits").filter(function (t) { return t.indexOf("pc-limit:") === 0; }); }
function pickRadio(v) { MR.find("pc-pick:" + v).props.onChange({ target: { checked: true } }); MR.rerender(); }
function picked() {
  var found = null;
  ALL("pc-pick").forEach(function (n) { if (n.type === "input" && n.props.type === "radio" && n.props.checked) found = n.props.value; });
  return found;
}
function radios() {
  return ALL("pc-pick").filter(function (n) { return n.type === "input" && n.props.type === "radio"; }).map(function (n) { return n.props.value; });
}
"""


@pytest.fixture(scope="module")
def code() -> str:
    return base_code(with_overlay=True) + "\n" + transpiled(SPEECH_FILE)


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
    """The refusals the console reads, answered by the real error handlers (and, for the 422, by the real speech router)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from primer.api.errors import register_error_handlers
    from primer.api.routers import speech
    from primer.model.except_ import ConflictError

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(speech.stt_providers_router, prefix="/v1")
    app.dependency_overrides[speech._stt_storage] = lambda: object()

    @app.post("/v1/provider_taken")
    def provider_taken():
        raise ConflictError("SpeechToTextProvider with id 'asr' already exists")

    @app.post("/v1/broke")
    def broke():
        raise RuntimeError("the database went away")

    client = TestClient(app, raise_server_exceptions=False)
    made = {
        "provider_taken": client.post("/v1/provider_taken"),
        "broke": client.post("/v1/broke"),
        # a body the real router refuses before it touches storage: no url, no limits
        "put_422": client.put("/v1/stt_providers/asr", json={"id": "asr", "provider": "openai"}),
    }
    expected = {"provider_taken": 409, "broke": 500, "put_422": 422}
    for name, answer in made.items():
        assert answer.status_code == expected[name], (name, answer.status_code, answer.text)
    return {name: answer.json() for name, answer in made.items()}


def _route(ctx, table: dict) -> None:
    ctx.eval(f"ROUTES = {json.dumps(table)};")


def _serve(ctx, klass: str) -> None:
    plural = PLURALS[klass]
    ctx.eval(f"serve(PC_typesKey({json.dumps(plural)}), {json.dumps(types_of(plural))}); MR.rerender();")


def _open(ctx, klass: str, **props) -> None:
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


def _fill(ctx, klass: str, provider_id: str = "sp", key: str | None = None, model: str | None = "kokoro") -> None:
    _type(ctx, "pc-field:id", provider_id)
    _type(ctx, "pc-field:url", URLS[klass])
    if key is not None:
        _type(ctx, "pc-field:api_key", key)
    if klass == "tts" and model is not None:
        _type(ctx, "pc-field:default_model", model)


def _listed(klass: str, names=None) -> dict:
    names = CHOICES[klass] if names is None else names
    return {"answer": {"ok": True, "models" if klass == "stt" else "voices": list(names)}}


def _test_path(klass: str) -> str:
    return f"POST /{PLURALS[klass]}/_test"


def _error(ctx) -> str | None:
    return ctx.eval("MR.find('pc-register-error') ? textOf(MR.find('pc-register-error')) : null")


def _help(ctx, testid: str) -> str:
    row = js(ctx, f"fieldRow('pc-speech-settings', {json.dumps(testid)})")
    return " ".join(c["text"] for c in row if c["className"] == "field-help" and not c["role"])


def _calls(ctx, method: str | None = None, path: str | None = None) -> list[dict]:
    return js(ctx, f"calls({json.dumps(method)}, {json.dumps(path)})")


def _requests(ctx) -> list[tuple[str, str]]:
    return [(c["method"], c["path"]) for c in js(ctx, "CALLS")]


def _to_page_two(ctx, klass: str, names=None, **fill) -> None:
    _open(ctx, klass)
    _route(ctx, {_test_path(klass): _listed(klass, names)})
    _fill(ctx, klass, **fill)
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert ctx.eval("MR.find('pc-speech-pick') !== null") is True, "the choice page is up"


def _validate(klass: str, body: dict):
    from primer.model.providers.speech import SpeechToTextProvider, TextToSpeechProvider

    return {"stt": SpeechToTextProvider, "tts": TextToSpeechProvider}[klass].model_validate(body)


# ---- page one ----------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("klass", ["stt", "tts"])
def test_page_one_is_an_id_a_url_a_key_and_the_limits_the_adapter_reads(ctx, klass: str) -> None:
    """One kind (``openai``, OpenAI-compatible) is served, so there is no Type to pick; the URL is required and the key is optional, as served."""
    served = types_of(PLURALS[klass])
    assert list(served) == ["openai"], "the premise: one kind"
    _open(ctx, klass)
    assert ctx.eval("MR.find('pc-field:type') === null") is True and ctx.eval("MR.find('pc-field:flavor') === null") is True
    assert js(ctx, "fieldRow('pc-speech-settings', 'pc-field:url')")[0]["text"] == "URL *", "the label alone on its line, with the required mark"
    assert ctx.eval("MR.find('pc-field:url').props['aria-required']") == "true" and ctx.eval("MR.find('pc-field:url').props.type") == "url"
    assert js(ctx, "fieldRow('pc-speech-settings', 'pc-field:api_key')")[0]["text"] == "API key"
    assert ctx.eval("MR.find('pc-field:api_key').props['aria-required'] === undefined") is True and ctx.eval("MR.find('pc-field:api_key').props.type") == "password"
    assert _help(ctx, "pc-field:api_key") == "Optional for local servers"
    assert "API base" in _help(ctx, "pc-field:url")
    assert ctx.eval("MR.find('pc-field:default_model') !== null") is (klass == "tts")
    assert ctx.eval("MR.find('pc-field:default_voice') === null") is True, "the voice is picked on the next page"
    assert js(ctx, "fieldRow('pc-speech-settings', 'pc-field:id')")[0]["text"].startswith("Id")


def test_the_tts_page_asks_for_the_default_model_and_says_what_it_is(ctx) -> None:
    """Design 13.1: a TTS draft has no model listing, so the model is typed on page one. It is required."""
    _open(ctx, "tts")
    assert js(ctx, "fieldRow('pc-speech-settings', 'pc-field:default_model')")[0]["text"].startswith("Default model")
    assert ctx.eval("MR.find('pc-field:default_model').props['aria-required']") == "true"
    assert ctx.eval("MR.find('pc-field:default_model').props.type") == "text"
    assert _help(ctx, "pc-field:default_model"), "it says what the model is for"


def test_speech_limits_show_only_what_the_adapter_reads(ctx) -> None:
    """ASR reads the two timeouts, TTS the timeouts, the retries and the backoffs; no speech adapter reads max concurrency, which the model still requires, so it is sent
    and never shown."""
    _open(ctx, "stt")
    assert js(ctx, "limitInputs()") == ["pc-limit:request_timeout_seconds"], "the connect timeout is folded away"
    _click(ctx, "pc-limits-more:toggle")
    assert js(ctx, "limitInputs()") == ["pc-limit:request_timeout_seconds", "pc-limit:connect_timeout_seconds"]
    assert "LIMITS" in ctx.eval("textOf(MR.find('pc-limits'))") and "RETRIES" not in ctx.eval("textOf(MR.find('pc-limits'))")
    _open(ctx, "tts")
    assert "LIMITS AND RETRIES" in ctx.eval("textOf(MR.find('pc-limits'))")
    _click(ctx, "pc-limits-more:toggle")
    assert sorted(js(ctx, "limitInputs()")) == sorted([
        "pc-limit:request_timeout_seconds", "pc-limit:max_retries", "pc-limit:retry_backoff_seconds", "pc-limit:connect_timeout_seconds", "pc-limit:retry_backoff_max_seconds"])
    assert ctx.eval("MR.find('pc-limit:max_concurrency') === null") is True, "no Max concurrency box"
    # what an empty timeout means is the adapter's own default, not "no limit"
    assert "Empty means the default, 300 s" in ctx.eval("textOf(MR.find('pc-limits'))") and "Empty means the default, 5 s" in ctx.eval("textOf(MR.find('pc-limits'))")


@pytest.mark.parametrize("klass", ["stt", "tts"])
def test_the_post_still_carries_max_concurrency(ctx, klass: str) -> None:
    _to_page_two(ctx, klass)
    ctx.eval(f"pickRadio({json.dumps(CHOICES[klass][0])})")
    _press(ctx, "pc-register-submit")
    (made,) = _calls(ctx, "POST", f"/{PLURALS[klass]}")
    assert made["body"]["limits"]["max_concurrency"] == 1
    assert "max_concurrency" in made["body"]["limits"]


@pytest.mark.parametrize("klass", ["stt", "tts"])
def test_next_needs_only_url_and_key(ctx, klass: str, monkeypatch) -> None:
    """Design 13.1, shared rule: Next works with only the connection fields filled in. The POST holds ``provider`` and ``config`` and nothing else (no id, no limits, no model),
    and the real ``_test`` route takes it."""
    import asyncio

    from primer.api.routers import speech

    _open(ctx, klass)
    _route(ctx, {_test_path(klass): _listed(klass)})
    _fill(ctx, klass, key="sk-asr-1")
    assert _press(ctx, "pc-register-next") == "fulfilled"
    (call,) = _calls(ctx)
    assert call == {"method": "POST", "path": f"/{PLURALS[klass]}/_test", "body": {"provider": "openai", "config": {"url": URLS[klass], "api_key": "sk-asr-1"}}}
    assert set(call["body"]) == {"provider", "config"}
    # the real route takes exactly this body and answers a list
    seen = {}

    async def fake(url, api_key=None):
        seen.update(url=url, api_key=api_key)
        return list(CHOICES[klass])

    monkeypatch.setattr(speech, "list_models" if klass == "stt" else "list_voices", fake)
    draft = (speech._SttDraft if klass == "stt" else speech._TtsDraft).model_validate(call["body"])
    answer = asyncio.run((speech.test_stt_provider if klass == "stt" else speech.test_tts_provider)(draft))
    assert answer == {"ok": True, "models" if klass == "stt" else "voices": CHOICES[klass]} and seen["api_key"] == "sk-asr-1"
    # a blank key is omitted, never sent as an empty string
    _open(ctx, klass)
    _route(ctx, {_test_path(klass): _listed(klass)})
    _fill(ctx, klass)
    _press(ctx, "pc-register-next")
    assert _calls(ctx)[-1]["body"] == {"provider": "openai", "config": {"url": URLS[klass]}}


def test_next_is_off_until_the_connection_can_be_tested(ctx) -> None:
    """Off while the id breaks the id rule, the URL is blank, a limit is wrong, or (TTS) the default model is blank. ``disabled`` (not the in-flight ``busy``) says so."""
    _open(ctx, "tts")
    assert ctx.eval("disabled('pc-register-next')") is True, "nothing typed"
    _type(ctx, "pc-field:id", "a/b")
    _type(ctx, "pc-field:url", URLS["tts"])
    _type(ctx, "pc-field:default_model", "kokoro")
    assert ctx.eval("disabled('pc-register-next')") is True, "'a/b' is not an id the server can address"
    _type(ctx, "pc-field:id", "tts-1")
    assert ctx.eval("disabled('pc-register-next')") is False
    _type(ctx, "pc-field:url", "   ")
    assert ctx.eval("disabled('pc-register-next')") is True, "a blank URL is blank"
    _type(ctx, "pc-field:url", URLS["tts"])
    _type(ctx, "pc-field:default_model", "  ")
    assert ctx.eval("disabled('pc-register-next')") is True, "TTS needs its default model"
    _type(ctx, "pc-field:default_model", "kokoro")
    _type(ctx, "pc-limit:max_retries", "-1")
    assert ctx.eval("disabled('pc-register-next')") is True and "whole number of 0 or more" in ctx.eval("textOf(MR.find('pc-limits'))")
    _type(ctx, "pc-limit:max_retries", "2")
    assert ctx.eval("disabled('pc-register-next')") is False
    _open(ctx, "stt")
    _type(ctx, "pc-field:id", "asr-1")
    _type(ctx, "pc-field:url", URLS["stt"])
    assert ctx.eval("disabled('pc-register-next')") is False, "ASR needs no model on page one"


# ---- page two ----------------------------------------------------------------------------------------------------------------------------------------------


def test_asr_page_two_picks_one_model_saved_as_default_model(ctx) -> None:
    _to_page_two(ctx, "stt", ["whisper-1", "large-v3", "distil-large"])
    assert js(ctx, "radios()") == ["whisper-1", "large-v3", "distil-large"], "the server's order"
    assert ctx.eval("picked()") is None, "nothing is picked"
    assert ctx.eval("textOf(MR.find('pc-import-head'))") == "Models found at sp✓ Connected; 3 models listed"
    assert "MODEL" in ctx.eval("textOf(MR.find('pc-pick'))").upper()
    ctx.eval("pickRadio('large-v3')")
    assert ctx.eval("picked()") == "large-v3"
    ctx.eval("pickRadio('whisper-1')")
    assert ctx.eval("picked()") == "whisper-1", "one choice: picking another replaces it"
    ctx.eval("pickRadio('large-v3')")
    from primer.model.providers.speech import SpeechToTextProvider

    answer = SpeechToTextProvider.model_validate({
        "id": "sp", "provider": "openai", "default_model": "large-v3", "config": {"url": URLS["stt"]}, "limits": {"max_concurrency": 1}}).model_dump(mode="json")
    _route(ctx, {"POST /stt_providers": {"answer": answer}})
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert _requests(ctx) == [("POST", "/stt_providers/_test"), ("POST", "/stt_providers")]
    (made,) = _calls(ctx, "POST", "/stt_providers")
    assert made["body"] == {
        "id": "sp", "provider": "openai", "default_model": "large-v3", "config": {"url": URLS["stt"]},
        "limits": {"max_concurrency": 1, "request_timeout_seconds": 300},
    }
    _validate("stt", made["body"])
    assert js(ctx, "SAVED") == [answer] and ctx.eval("CLOSED") == 1


def test_tts_page_two_picks_one_voice_saved_as_default_voice(ctx) -> None:
    _to_page_two(ctx, "tts", ["alloy", "af_heart", "bm_lewis"], key="sk-tts-1", model="kokoro")
    assert js(ctx, "radios()") == ["alloy", "af_heart", "bm_lewis"]
    assert ctx.eval("textOf(MR.find('pc-import-head'))") == "Voices found at sp✓ Connected; 3 voices listed"
    assert "VOICE" in ctx.eval("textOf(MR.find('pc-pick'))").upper()
    ctx.eval("pickRadio('af_heart')")
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    (made,) = _calls(ctx, "POST", "/tts_providers")
    assert made["body"] == {
        "id": "sp", "provider": "openai", "default_model": "kokoro", "default_voice": "af_heart", "config": {"url": URLS["tts"], "api_key": "sk-tts-1"},
        "limits": {"max_concurrency": 1, "request_timeout_seconds": 300, "max_retries": 2, "retry_backoff_seconds": 0.5, "retry_backoff_max_seconds": 8},
    }
    _validate("tts", made["body"])
    assert ctx.eval("CLOSED") == 1


def test_one_voice_is_said_in_the_singular(ctx) -> None:
    _to_page_two(ctx, "tts", ["alloy"])
    assert ctx.eval("textOf(MR.find('pc-import-head'))") == "Voices found at sp✓ Connected; 1 voice listed"


@pytest.mark.parametrize("klass", ["stt", "tts"])
def test_register_is_off_until_a_choice_is_made(ctx, klass: str) -> None:
    _to_page_two(ctx, klass)
    assert ctx.eval("text('pc-register-submit')") == "Register" and ctx.eval("disabled('pc-register-submit')") is True
    _press(ctx, "pc-register-submit")
    assert _calls(ctx, "POST", f"/{PLURALS[klass]}") == [], "a press while it is off posts nothing"
    ctx.eval(f"pickRadio({json.dumps(CHOICES[klass][1])})")
    assert ctx.eval("disabled('pc-register-submit')") is False
    assert ctx.eval("text('pc-register-back')") == "‹ Back"


def test_a_model_listed_twice_is_one_choice(ctx) -> None:
    _to_page_two(ctx, "stt", ["a", "b", "a", ""])
    assert js(ctx, "radios()") == ["a", "b"]
    assert "✓ Connected; 2 models listed" in js(ctx, "MR.texts()")


def test_a_choice_with_odd_characters_can_be_picked(ctx) -> None:
    _to_page_two(ctx, "tts", ["voices/en US:warm", "x y"])
    ctx.eval("pickRadio('voices/en US:warm')")
    _press(ctx, "pc-register-submit")
    assert _calls(ctx, "POST", "/tts_providers")[0]["body"]["default_voice"] == "voices/en US:warm"


# ---- failures and the connection ---------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("klass", ["stt", "tts"])
def test_a_failed_test_stays_on_page_one_with_the_reason(ctx, klass: str) -> None:
    """``_test`` answers 200 with ``ok: false`` and the reason (its text is already scrubbed of the key): the page says it on the overlay's error line and stays."""
    _open(ctx, klass)
    reason = "ConnectError: All connection attempts failed"
    _route(ctx, {_test_path(klass): [{"answer": {"ok": False, "error": reason}}, _listed(klass)]})
    _fill(ctx, klass)
    assert _press(ctx, "pc-register-next") == "fulfilled", "the click handler's promise never rejects"
    assert ctx.eval("MR.find('pc-speech-settings') !== null && MR.find('pc-speech-pick') === null") is True, "still page one"
    assert _error(ctx) == reason and ctx.eval("MR.find('pc-register-error').props.role") == "alert"
    assert ctx.eval("ariaBusy('pc-register-next')") is False, "Next retries"
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert ctx.eval("MR.find('pc-speech-pick') !== null") is True and _error(ctx) is None


def test_an_invalid_draft_is_said_as_the_server_says_it(ctx) -> None:
    _open(ctx, "stt")
    _route(ctx, {_test_path("stt"): {"answer": {"ok": False, "error": "invalid draft: config.url: Input should be a valid URL"}}})
    _fill(ctx, "stt")
    _press(ctx, "pc-register-next")
    assert _error(ctx) == "invalid draft: config.url: Input should be a valid URL"


def test_a_refused_request_is_said_in_the_one_readers_words(ctx, envelopes) -> None:
    _open(ctx, "stt")
    _route(ctx, {_test_path("stt"): {"refuse": envelopes["broke"]}})
    _fill(ctx, "stt")
    assert _press(ctx, "pc-register-next") == "fulfilled"
    reason = ctx.eval(f"window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(envelopes['broke'])})).message")
    assert reason and _error(ctx) == reason and ctx.eval("MR.find('pc-speech-pick') === null") is True


@pytest.mark.parametrize(("klass", "words"), [("stt", "The server listed no models."), ("tts", "The server listed no voices.")])
def test_an_empty_list_stays_on_page_one_and_says_so(ctx, klass: str, words: str) -> None:
    _open(ctx, klass)
    _route(ctx, {_test_path(klass): _listed(klass, [])})
    _fill(ctx, klass)
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert _error(ctx) == words and ctx.eval("MR.find('pc-speech-pick') === null") is True
    _route(ctx, {_test_path(klass): {"answer": {"ok": True}}})
    _press(ctx, "pc-register-next")
    assert _error(ctx) == words, "an answer with no list at all is an empty one"


def test_an_answer_that_is_not_an_object_is_a_failed_test(ctx) -> None:
    _open(ctx, "stt")
    _route(ctx, {_test_path("stt"): {"answer": None}})
    _fill(ctx, "stt")
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert _error(ctx) and ctx.eval("MR.find('pc-speech-pick') === null") is True


def test_a_refused_register_stays_on_page_two_with_the_reason_and_can_be_pressed_again(ctx, envelopes) -> None:
    _to_page_two(ctx, "stt")
    ctx.eval("pickRadio('large-v3')")
    _route(ctx, {"POST /stt_providers": [{"refuse": envelopes["provider_taken"]}, {"answer": {"id": "sp-2"}}]})
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert "already exists" in _error(ctx) and ctx.eval("MR.find('pc-speech-pick') !== null") is True
    assert ctx.eval("CLOSED") == 0 and js(ctx, "SAVED") == []
    assert ctx.eval("ariaBusy('pc-register-submit')") is False and ctx.eval("disabled('pc-register-submit')") is False, "it works again"
    _click(ctx, "pc-register-back")
    assert ctx.eval("value('pc-field:id')") == "sp"
    _type(ctx, "pc-field:id", "sp-2")
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert len(_calls(ctx, "POST", "/stt_providers/_test")) == 1, "the same connection is not tested again"
    assert ctx.eval("picked()") == "large-v3", "the list and the choice were kept"
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert [c["body"]["id"] for c in _calls(ctx, "POST", "/stt_providers")] == ["sp", "sp-2"]
    assert ctx.eval("CLOSED") == 1 and js(ctx, "SAVED") == [{"id": "sp-2"}]


def test_a_second_click_while_registering_posts_nothing_more(ctx) -> None:
    _to_page_two(ctx, "stt")
    ctx.eval("pickRadio('large-v3')")
    _route(ctx, {"POST /stt_providers": {"hold": "create"}})
    ctx.eval("press('pc-register-submit')")
    settle(ctx)
    assert ctx.eval("ariaBusy('pc-register-submit')") is True
    for _ in range(2):
        ctx.eval("press('pc-register-submit')")
        settle(ctx)
    assert len(_calls(ctx, "POST", "/stt_providers")) == 1
    assert ctx.eval("ariaDisabled('pc-register-back')") is True, "and Back waits for the answer"
    ctx.eval("HELD.create.ok({ id: 'sp' }); void 0;")
    settle(ctx)
    assert ctx.eval("CLOSED") == 1 and len(js(ctx, "SAVED")) == 1


def test_the_caller_hears_the_row_the_server_answered_never_the_request(ctx) -> None:
    _to_page_two(ctx, "stt", key="sk-typed-in-the-clear")
    ctx.eval("pickRadio('large-v3')")
    ctx.eval("HANDLER = function () { return Promise.resolve(null); };")
    _press(ctx, "pc-register-submit")
    assert js(ctx, "SAVED") == [{"id": "sp"}]
    assert "sk-typed-in-the-clear" not in json.dumps(js(ctx, "SAVED"))


def test_back_keeps_the_list_and_the_choice_for_the_same_connection_and_drops_them_for_another(ctx) -> None:
    _to_page_two(ctx, "stt", ["a", "b"], key="sk-1")
    ctx.eval("pickRadio('b')")
    _click(ctx, "pc-register-back")
    assert ctx.eval("value('pc-field:id')") == "sp" and ctx.eval("value('pc-field:url')") == URLS["stt"] and ctx.eval("value('pc-field:api_key')") == "sk-1"
    _type(ctx, "pc-limit:request_timeout_seconds", "120")
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert len(_calls(ctx, "POST", "/stt_providers/_test")) == 1, "a limit is not part of the connection"
    assert ctx.eval("picked()") == "b"
    _click(ctx, "pc-register-back")
    _type(ctx, "pc-field:url", "http://other.test/v1")
    _route(ctx, {_test_path("stt"): _listed("stt", ["z"])})
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert len(_calls(ctx, "POST", "/stt_providers/_test")) == 2 and js(ctx, "radios()") == ["z"] and ctx.eval("picked()") is None


def test_a_second_next_while_the_server_is_answering_asks_nothing_more(ctx) -> None:
    _open(ctx, "stt")
    _route(ctx, {_test_path("stt"): {"hold": "test"}})
    _fill(ctx, "stt")
    for _ in range(3):
        ctx.eval("press('pc-register-next')")
        settle(ctx)
    assert len(_calls(ctx, "POST", "/stt_providers/_test")) == 1
    ctx.eval("HELD.test.ok({ ok: true, models: [] }); void 0;")
    settle(ctx)


def test_an_answer_for_a_connection_that_has_since_changed_is_not_used(ctx) -> None:
    _open(ctx, "stt")
    _route(ctx, {_test_path("stt"): {"hold": "old"}})
    _fill(ctx, "stt")
    ctx.eval("press('pc-register-next')")
    settle(ctx)
    _type(ctx, "pc-field:url", "http://fixed.test/v1")
    ctx.eval("HELD.old.ok({ ok: true, models: ['from-the-old-server'] }); void 0;")
    settle(ctx)
    assert ctx.eval("MR.find('pc-speech-pick') === null") is True, "the old list is not shown"
    assert "changed while the server was answering" in _error(ctx)
    _route(ctx, {_test_path("stt"): _listed("stt", ["from-the-new-server"])})
    assert _press(ctx, "pc-register-next") == "fulfilled"
    assert js(ctx, "radios()") == ["from-the-new-server"]


def test_the_default_model_of_a_tts_provider_can_change_without_testing_again(ctx) -> None:
    """The voices do not depend on the model: only the connection (``provider`` and ``config``) is what Next asks about."""
    _to_page_two(ctx, "tts", ["alloy"], model="kokoro")
    ctx.eval("pickRadio('alloy')")
    _click(ctx, "pc-register-back")
    _type(ctx, "pc-field:default_model", "kokoro-v2")
    _press(ctx, "pc-register-next")
    assert len(_calls(ctx, "POST", "/tts_providers/_test")) == 1 and ctx.eval("picked()") == "alloy"
    _press(ctx, "pc-register-submit")
    assert _calls(ctx, "POST", "/tts_providers")[0]["body"]["default_model"] == "kokoro-v2"


# ---- editing -----------------------------------------------------------------------------------------------------------------------------------------------


def _stt_row(provider_id: str = "asr") -> dict:
    """An ASR provider as the server serves it: the key masked, and limits the page does not show (``max_concurrency`` 3, ``max_retries`` 5)."""
    from primer.model.providers.speech import SpeechToTextProvider

    row = SpeechToTextProvider.model_validate({
        "id": provider_id, "provider": "openai", "default_model": "whisper-1", "config": {"url": URLS["stt"], "api_key": "sk-asr-abcd1234"},
        "limits": {"max_concurrency": 3, "request_timeout_seconds": 120.0, "connect_timeout_seconds": 4.0, "max_retries": 5, "retry_backoff_seconds": 1.5},
    }).model_dump(mode="json")
    assert row["config"]["api_key"] != "sk-asr-abcd1234", "the served row masks its key"
    return row


def _tts_row(provider_id: str = "tts") -> dict:
    from primer.model.providers.speech import TextToSpeechProvider

    return TextToSpeechProvider.model_validate({
        "id": provider_id, "provider": "openai", "default_model": "kokoro", "default_voice": "af_heart", "config": {"url": URLS["tts"]},
        "limits": {"max_concurrency": 2, "request_timeout_seconds": 60.0, "max_retries": 3},
    }).model_dump(mode="json")


def test_edit_round_trips_every_unshown_field(ctx) -> None:
    """Master Review Focus 1. An ASR row with ``max_concurrency`` 3 and ``max_retries`` 5 (neither is shown for ASR) keeps both after a URL edit; a field no version of the page
    knows is kept too; the masked key goes back as served; only the URL differs."""
    row = _stt_row()
    row["x_future"] = {"kept": True}
    row["config"]["x_future_knob"] = 7
    _open(ctx, "stt", editRow=row)
    assert ctx.eval("textOf(MR.find('pc-register-submit'))") == "Save"
    _type(ctx, "pc-field:url", "http://asr-2.test:8006/v1")
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    (call,) = [c for c in js(ctx, "CALLS") if c["method"] != "GET"]
    expected = json.loads(json.dumps(row))
    expected["config"]["url"] = "http://asr-2.test:8006/v1"
    assert call == {"method": "PUT", "path": "/stt_providers/asr", "body": expected}
    assert call["body"]["limits"]["max_concurrency"] == 3 and call["body"]["limits"]["max_retries"] == 5
    assert ctx.eval("CLOSED") == 1 and js(ctx, "SAVED") == [expected]


@pytest.mark.parametrize("klass", ["stt", "tts"])
def test_edit_shows_the_row_and_sends_it_back_untouched(ctx, klass: str) -> None:
    row = _stt_row() if klass == "stt" else _tts_row()
    _open(ctx, klass, editRow=row)
    assert ctx.eval("value('pc-field:url')") == URLS[klass] and ctx.eval("value('pc-field:default_model')") == row["default_model"]
    assert ctx.eval("MR.find('pc-field:default_voice') !== null") is (klass == "tts")
    if klass == "tts":
        assert ctx.eval("value('pc-field:default_voice')") == "af_heart" and ctx.eval("MR.find('pc-field:default_voice').props.type") == "text"
    assert ctx.eval("value('pc-limit:request_timeout_seconds')") == str(int(row["limits"]["request_timeout_seconds"]))
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert _calls(ctx, "PUT")[0]["body"] == row, "not one field is added, dropped or turned into text"


def test_edit_can_change_the_default_model_and_voice_as_text(ctx) -> None:
    row = _tts_row()
    _open(ctx, "tts", editRow=row)
    _type(ctx, "pc-field:default_model", "kokoro-v2")
    _type(ctx, "pc-field:default_voice", "bm_lewis")
    _press(ctx, "pc-register-submit")
    body = _calls(ctx, "PUT")[0]["body"]
    assert body["default_model"] == "kokoro-v2" and body["default_voice"] == "bm_lewis"
    _validate("tts", body)


def test_edit_keeps_the_masked_key_as_served_and_a_new_one_replaces_it(ctx) -> None:
    row = _stt_row()
    mask = row["config"]["api_key"]
    _open(ctx, "stt", editRow=row)
    assert ctx.eval("value('pc-field:api_key')") == mask and "Stored secrets are shown masked; leave it as is to keep it." in _help(ctx, "pc-field:api_key")
    _press(ctx, "pc-register-submit")
    assert _calls(ctx, "PUT")[0]["body"]["config"]["api_key"] == mask, "the server restores the stored key from its own mask"
    _type(ctx, "pc-field:api_key", "sk-new")
    _press(ctx, "pc-register-submit")
    assert _calls(ctx, "PUT")[1]["body"]["config"]["api_key"] == "sk-new"
    _type(ctx, "pc-field:api_key", "")
    _press(ctx, "pc-register-submit")
    assert "api_key" not in _calls(ctx, "PUT")[2]["body"]["config"], "a cleared optional key is left out (ticket T3), never sent as an empty string"


def test_edit_is_off_while_a_required_field_is_blank(ctx) -> None:
    _open(ctx, "tts", editRow=_tts_row())
    assert ctx.eval("disabled('pc-register-submit')") is False
    for testid in ("pc-field:url", "pc-field:default_model", "pc-field:default_voice"):
        original = ctx.eval(f"value({json.dumps(testid)})")
        _type(ctx, testid, "  ")
        assert ctx.eval("disabled('pc-register-submit')") is True, testid
        _type(ctx, testid, original)
        assert ctx.eval("disabled('pc-register-submit')") is False, testid


@pytest.mark.parametrize("klass", ["stt", "tts"])
def test_edit_mode_has_no_next(ctx, klass: str) -> None:
    """Review Focus 4: in edit mode there is nothing to test with a draft. The draft holds the MASK of the key, and a test would authenticate with ``**********``. The id is
    the row's, and the footer is Cancel, Delete and Save."""
    row = _stt_row() if klass == "stt" else _tts_row()
    _open(ctx, klass, editRow=row)
    assert ctx.eval("MR.find('pc-register-next') === null && MR.find('pc-register-back') === null") is True
    assert ctx.eval("MR.find('pc-field:id').props.readOnly") is True and ctx.eval("value('pc-field:id')") == row["id"]
    footer = js(ctx, "testids('pc-register')")
    assert [t for t in footer if t in ("pc-register-cancel", "pc-register-delete", "pc-register-submit")] == ["pc-register-cancel", "pc-register-delete", "pc-register-submit"]
    assert "Edit " + row["id"] in js(ctx, "MR.texts()")
    _press(ctx, "pc-register-submit")
    assert not any(path.endswith("/_test") for _, path in _requests(ctx)), "an edit probes nothing"
    assert {method for method, _ in _requests(ctx)} == {"PUT"}
    assert ctx.eval("MR.find('pc-register-invalidate') === null") is True, "the speech classes do not cache an adapter the console can invalidate"


def test_a_refused_save_stays_open_with_the_reason(ctx, envelopes) -> None:
    _open(ctx, "stt", editRow=_stt_row())
    _route(ctx, {"PUT /stt_providers/asr": {"refuse": envelopes["put_422"]}})
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    reason = ctx.eval(f"window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(envelopes['put_422'])})).message")
    assert reason and _error(ctx) == reason
    assert ctx.eval("CLOSED") == 0 and js(ctx, "SAVED") == []
    assert ctx.eval("ariaBusy('pc-register-submit')") is False and ctx.eval("disabled('pc-register-submit')") is False, "Save works again"


def test_a_row_of_a_kind_the_install_does_not_serve_cannot_be_saved(ctx) -> None:
    row = _stt_row()
    row["provider"] = "no-such-kind"
    _open(ctx, "stt", editRow=row)
    assert "does not serve the kind" in ctx.eval("textOf(MR.find('pc-register-kind-missing'))")
    assert ctx.eval("disabled('pc-register-submit')") is True


# ---- the radio list ----------------------------------------------------------------------------------------------------------------------------------------

RADIO_STUB = r"""
var CHOSEN = [];
function mountRadios(props) {
  MR.mount(function Host(p) {
    return el("div", { "data-testid": "host" }, el(PC_RadioList, Object.assign({ onChange: function (v) { CHOSEN.push(v); } }, p)));
  }, props);
}
"""


def _radios(ctx, **props) -> None:
    ctx.eval(RADIO_STUB)
    props.setdefault("name", "Model")
    props.setdefault("testid", "pc-pick")
    props.setdefault("options", [{"value": "a", "label": "Alpha"}, {"value": "b", "label": "Beta"}])
    props.setdefault("value", "")
    ctx.eval(f"mountRadios({json.dumps(props)});")


def test_a_radio_list_is_a_kit_section_holding_one_radio_per_option(ctx) -> None:
    """The kit has no single-choice list (flagged to the lead): this is the one local control, built from ``PK_Section``."""
    _radios(ctx)
    assert ctx.eval("MR.find('pc-pick').props.role") == "group" and "pk-section" in ctx.eval("MR.find('pc-pick').props.className")
    assert ctx.eval("textOf(MR.find('pc-pick'))").startswith("Model")
    assert js(ctx, "radios()") == ["a", "b"]
    node = js(ctx, "(function () { var n = MR.find('pc-pick:a'); return { type: n.props.type, value: n.props.value, checked: n.props.checked }; })()")
    assert node == {"type": "radio", "value": "a", "checked": False}
    labels = js(ctx, "ALL('pc-pick').filter(function (n) { return n.type === 'label'; }).map(textOf)")
    assert labels == ["Alpha", "Beta"], "each radio is named by its label"


def test_the_radios_of_a_list_are_one_group_and_one_is_checked(ctx) -> None:
    _radios(ctx, value="b")
    names = js(ctx, "ALL('pc-pick').filter(function (n) { return n.type === 'input'; }).map(function (n) { return n.props.name; })")
    assert len(names) == 2 and names[0] == names[1] and names[0], "one name: the browser lets one of them be chosen, and arrow keys move between them"
    assert [ctx.eval(f"MR.find('pc-pick:{v}').props.checked") for v in ("a", "b")] == [False, True]
    ctx.eval("MR.find('pc-pick:a').props.onChange({ target: { checked: true } });")
    assert js(ctx, "CHOSEN") == ["a"]


def test_two_lists_on_a_page_are_two_groups(ctx) -> None:
    ctx.eval(RADIO_STUB)
    ctx.eval(
        "MR.mount(function Host() { return el('div', { 'data-testid': 'host' },"
        " el(PC_RadioList, { name: 'Model', testid: 'one', options: [{ value: 'a', label: 'A' }], value: '', onChange: function () {} }),"
        " el(PC_RadioList, { name: 'Voice', testid: 'two', options: [{ value: 'a', label: 'A' }], value: '', onChange: function () {} })); }, {});"
    )
    one = ctx.eval("MR.find('one:a').props.name")
    two = ctx.eval("MR.find('two:a').props.name")
    assert one and two and one != two


# ---- the page in the overlay -------------------------------------------------------------------------------------------------------------------------------

PAGE_STUBS = r"""
function frameFn(parts) {
  LAST = parts;
  return el("div", { "data-testid": "frame" },
    el("div", { "data-testid": "frame-body" }, parts.body),
    el("div", { "data-testid": "frame-left" }, parts.footerLeft),
    el("div", { "data-testid": "frame-right" }, parts.footerRight),
    parts.error ? el("div", { "data-testid": "frame-error" }, parts.error) : null);
}
var LAST = null;
function mountPage(key, props) {
  MR.mount(function Host(p) {
    return el(PC_SpeechRegister, Object.assign({
      klass: PC_classByKey(key), editRow: null, excludeKinds: null, frame: frameFn, blocked: false,
      onSaved: function (row, words) { SAVED.push(row); WORDS.push(words === undefined ? null : words); }, onClose: function () { CLOSED += 1; },
    }, p));
  }, props || {});
}
"""


def _page(ctx, klass: str, **props) -> None:
    ctx.eval(PAGE_STUBS)
    ctx.eval(f"mountPage({json.dumps(klass)}, {json.dumps(props)});")
    _serve(ctx, klass)


def test_the_page_is_the_page_of_the_asr_and_tts_classes(ctx) -> None:
    assert ctx.eval("window.PC_REGISTER_PAGES.stt === PC_SpeechRegister && window.PC_REGISTER_PAGES.tts === PC_SpeechRegister") is True
    assert ctx.eval("window.PC_SpeechRegister === PC_SpeechRegister && window.PC_RadioList === PC_RadioList") is True
    assert ctx.eval("typeof window.PC_REGISTER_PAGES.embedding") == "undefined", "the merged registry keeps what other files put in it and adds nothing of theirs"


def test_a_blocked_page_holds_every_action_off(ctx) -> None:
    """A page is handed ``blocked`` (the overlay's Delete is out): every action it has is held off (aria-disabled, not busy), and a press sends nothing."""
    row = _stt_row()
    _page(ctx, "stt", editRow=row)
    ctx.eval("MR.rerender({ blocked: true, klass: PC_classByKey('stt'), editRow: " + json.dumps(row) + ", excludeKinds: null, frame: frameFn,"
             " onSaved: function () {}, onClose: function () {} });")
    assert ctx.eval("ariaDisabled('pc-register-submit')") is True and ctx.eval("ariaBusy('pc-register-submit')") is False
    _press(ctx, "pc-register-submit")
    assert js(ctx, "CALLS") == []
    _page(ctx, "stt")
    _fill(ctx, "stt")
    ctx.eval("MR.rerender({ blocked: true, klass: PC_classByKey('stt'), editRow: null, excludeKinds: null, frame: frameFn, onSaved: function () {}, onClose: function () {} });")
    assert ctx.eval("ariaDisabled('pc-register-next')") is True and ctx.eval("ariaBusy('pc-register-next')") is False
    _press(ctx, "pc-register-next")
    assert js(ctx, "CALLS") == []


def test_the_selector_is_locked_while_a_request_is_out(ctx) -> None:
    _open(ctx, "stt")
    _route(ctx, {_test_path("stt"): {"hold": "test"}})
    _fill(ctx, "stt")
    assert ctx.eval("MR.find('pc-register-type').props['aria-disabled'] === undefined") is True
    ctx.eval("press('pc-register-next')")
    settle(ctx)
    assert ctx.eval("MR.find('pc-register-type').props['aria-disabled']") == "true" and ctx.eval("ariaBusy('pc-register-next')") is True
    ctx.eval("HELD.test.ok({ ok: true, models: ['m'] }); void 0;")
    settle(ctx)
    assert ctx.eval("MR.find('pc-register-type').props['aria-disabled'] === undefined") is True and ctx.eval("MR.find('pc-speech-pick') !== null") is True


def test_cancel_closes_the_overlay(ctx) -> None:
    _open(ctx, "tts")
    _click(ctx, "pc-register-cancel")
    assert ctx.eval("CLOSED") == 1 and _requests(ctx) == [] and js(ctx, "SAVED") == []


def test_the_page_says_while_the_kinds_are_loading_and_offers_no_next(ctx) -> None:
    ctx.eval("mountOverlay({ initialClass: 'stt' });")
    assert ctx.eval("textOf(MR.find('pc-register-loading'))") == "Loading the fields for this kind…"
    assert ctx.eval("MR.find('pc-register-next') !== null && disabled('pc-register-next')") is True, "the footer is there while the body loads"
    _serve(ctx, "stt")
    assert ctx.eval("MR.find('pc-register-loading') === null") is True


def test_a_failed_kinds_request_is_an_alert_with_a_try_again_that_works(ctx, envelopes) -> None:
    key = "PC_typesKey('stt_providers')"
    ctx.eval("mountOverlay({ initialClass: 'stt' });")
    ctx.eval(f"fail({key}, {json.dumps(envelopes['put_422'])}); MR.rerender();")
    assert ctx.eval("MR.find('pc-register-types-error').props.role") == "alert" and ctx.eval("disabled('pc-register-next')") is True
    refetched = ctx.eval(f"RES[{key}].refetched")
    _click(ctx, "pc-register-types-retry")
    assert ctx.eval(f"RES[{key}].refetched") == refetched + 1
    before = ctx.eval("MR.findProp('to', 'first').props.n")
    _serve(ctx, "stt")
    assert ctx.eval("MR.find('pc-register-types-error') === null && MR.find('pc-field:url') !== null") is True
    assert ctx.eval("MR.findProp('to', 'first').props.n") == before + 1, "the focus is handed to the first field, which is drawn now"


def test_the_pages_work_on_a_phone_sheet(ctx) -> None:
    ctx.eval("MOBILE = true;")
    _to_page_two(ctx, "stt")
    assert ctx.eval("MR.find('pc-register').props.className").split() == ["sheet", "pk-overlay"]
    ctx.eval("pickRadio('whisper-1')")
    assert _press(ctx, "pc-register-submit") == "fulfilled"
    assert _calls(ctx, "POST", "/stt_providers")[0]["body"]["default_model"] == "whisper-1" and ctx.eval("CLOSED") == 1


# ---- the focus ---------------------------------------------------------------------------------------------------------------------------------------------


def test_a_page_change_asks_for_the_focus_to_be_handed_over(ctx) -> None:
    """The button that took the page forward or back is gone when the other page is drawn: the page asks its body for a hand-off, to the heading of the choice page going
    forward and to the first field going back."""
    _open(ctx, "stt")
    assert ctx.eval("MR.findProp('to', 'first').props.n") == 0, "opening is the dialog's own business: it takes the focus in"
    _route(ctx, {_test_path("stt"): _listed("stt")})
    _fill(ctx, "stt")
    _press(ctx, "pc-register-next")
    head = ctx.eval("MR.findProp('to', 'head').props.n")
    assert head >= 1 and ctx.eval("MR.find('pc-import-head').props.tabIndex") == -1, "the heading can take the focus"
    _click(ctx, "pc-register-back")
    assert ctx.eval("MR.findProp('to', 'first').props.n") == head + 1


def test_a_button_that_is_gone_is_not_reused_as_another_action(ctx) -> None:
    """Back (page two) and Cancel (page one) are both the first thing on the left of the footer: without keys of their own they would be ONE DOM node, and a focus left on Back
    would stay on a button now called Cancel, where a second Enter closes the overlay."""
    _page(ctx, "stt")
    _route(ctx, {_test_path("stt"): _listed("stt")})
    _fill(ctx, "stt")
    one = js(ctx, "[LAST.footerLeft.key, LAST.footerRight.key]")
    _press(ctx, "pc-register-next")
    two = js(ctx, "[LAST.footerLeft.key, LAST.footerRight.key]")
    assert all(one) and all(two), (one, two)
    assert one[0] != two[0] and one[1] != two[1]
    _click(ctx, "pc-register-back")
    assert js(ctx, "[LAST.footerLeft.key, LAST.footerRight.key]") == one


# ---- the unit pieces ---------------------------------------------------------------------------------------------------------------------------------------


def test_the_index_loads_the_speech_page_after_the_models_page_and_before_the_catalog() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    tag = 'src="components/providers/register-speech.jsx"'
    assert html.count(tag) == 1
    assert html.index(tag) > html.index('src="components/providers/register-overlay.jsx"')
    assert html.index(tag) > html.index('src="components/providers/register-common.jsx"')
    assert html.index(tag) > html.index('src="components/providers/register-models.jsx"')
    assert html.index(tag) < html.index('src="components/provider-catalog.jsx"')


def test_the_page_keeps_every_rule_of_the_overlays_pages() -> None:
    """The source rules of ``register-overlay.jsx``: no effect of the page reads or focuses body DOM, the page provides no context, and it returns ``frame(...)`` in every state."""
    src = SPEECH_FILE.read_text(encoding="utf-8")
    assert "createContext" not in src and ".Provider" not in src
    assert src.count("return frame({") >= 3, "every state returns the frame"
    assert "document.activeElement" not in src and ".focus()" not in src, "the focus is handed over by PC_PageFocus, a component inside the body"
    assert "PC_PageFocus" in src
    assert "startTransition" not in src and "useDeferredValue" not in src and "Suspense" not in src
    for voice in ("af_heart", "am_adam", "af_bella", "alloy"):
        assert voice not in src, f"{voice} is a voice name: voices come from the server"
