"""The console's provider settings form (PMS Task 7): the single profile's MODEL SETTINGS section, drawn by
``ui/components/model-profile-settings.jsx`` and wired into ``model-profiles.jsx``.

The section is the provider's own settings for the profile's ``config``: the Reasoning control (the model's
levels plus ``xhigh``, ``max`` and ``custom``, which opens a value field datalisted from the catalogue's
``custom_reasoning.suggestions``), one typed control per catalogue setting fetched from
``GET /v1/llm_providers/<provider>/model_settings`` (Task 5's route) when the chosen provider changes, a
"Server body fields" box on a wire with body passthrough, and the Extended (JSON) fold. One ``extended``
object is the source of truth: the typed controls and the fold both read and write it; the form pushes its
draft (``reasoning``, ``extended``, the client-side ``errors``) to the overlay, which gates Save on it and
builds the save body from it. A Save refused with a form-level 422 toasts the response's message on the
form's existing error toast path.

The real components (``shared.jsx``, the kit, ``foundation/api.js``, both ``model-profiles`` files) run on
the hook runtime of ``tests/ui/_mini_react.py`` the way ``tests/ui/test_llm_profile_overlays.py`` drives
them; the apiFetch stub records the calls and answers whatever the test sets on ``ANSWER``.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._mini_react import ROOT, mini_react_context, transpile

UI = ROOT / "ui"


def _src(path: str) -> str:
    """The file's text, or empty while it does not exist yet (the red run)."""
    p = UI / path
    return p.read_text(encoding="utf-8") if p.exists() else ""


SETTINGS_SRC = _src("components/model-profile-settings.jsx")
INDEX = _src("index.html")
PROFILES_SRC = _src("components/model-profiles.jsx")

PROVIDERS = [{"id": "lp-vllm", "provider": "openchat"}, {"id": "lp-b", "provider": "openchat"}]
ROWS = [
    {"id": "p-a", "kind": "single", "context_length": 32768},
    {"id": "p-b", "kind": "single", "context_length": 8192},
    {"id": "p-c", "kind": "single", "context_length": 131072},
]
FRESH = {"id": "p-a", "description": "d", "kind": "single", "provider_id": "lp-vllm", "model_name": "m",
         "context_length": 8192, "config": {"reasoning": None, "extended": {}}}
TUNED = {"id": "p-a", "description": "d", "kind": "single", "provider_id": "lp-vllm", "model_name": "m",
         "context_length": 8192, "config": {"reasoning": "high", "extended": {"top_k": 20}}}
CUSTOM_STORED = {"id": "p-a", "description": "d", "kind": "single", "provider_id": "lp-vllm", "model_name": "m",
                 "context_length": 8192, "config": {"reasoning": {"custom": "deep"}, "extended": {}}}
AGG = {"id": "agg-1", "description": "my words", "kind": "aggregated", "members": ["p-a", "p-b"],
       "config": {"reasoning": "low", "extended": {"k": 1}}}

# Task 5's fixed contract (the dispatch): a Setting is {key, type, description, wire_path, values, min, max,
# fields, open, set_by_reasoning}; the one per wire below exercises every type the route names.
CATALOGUE = {
    "provider_type": "openchat",
    "flavor": "vllm",
    "reasoning_levels": ["off", "minimal", "low", "medium", "high", "xhigh", "max"],
    "wire": "openchat/vllm",
    "settings": [
        {"key": "reasoning_effort", "type": "enum", "description": "How hard the model thinks.", "wire_path": "reasoning_effort",
         "values": ["low", "medium", "high"], "open": True, "set_by_reasoning": True},
        {"key": "chat_template_kwargs", "type": "object", "description": "Template knobs for the chat template.", "wire_path": "body.chat_template_kwargs",
         "values": [], "open": True, "set_by_reasoning": False,
         "fields": [{"key": "enable_thinking", "type": "boolean", "description": "Let the model think.",
                     "wire_path": "body.chat_template_kwargs.enable_thinking", "values": [], "open": False, "set_by_reasoning": True}]},
        {"key": "top_k", "type": "integer", "description": "Sample from the top k tokens.", "wire_path": "top_k",
         "values": [], "open": False, "set_by_reasoning": False, "min": 1, "max": 64},
        {"key": "min_p", "type": "number", "description": "Drop tokens below this fraction.", "wire_path": "min_p",
         "values": [], "open": False, "set_by_reasoning": False, "min": 0.0, "max": 1.0},
        {"key": "include_reasoning", "type": "boolean", "description": "Include the reasoning in the answer.", "wire_path": "include_reasoning",
         "values": [], "open": False, "set_by_reasoning": False},
    ],
    "body_passthrough": True,
    "custom_reasoning": {"key": "reasoning_effort", "wire_path": "reasoning_effort", "suggestions": ["low", "medium", "high"]},
}
NOBODY = {**CATALOGUE, "body_passthrough": False}
NOSETTINGS = {**CATALOGUE, "settings": []}
BAD_KEY_422 = {
    "type": "https://errors.primer.dev/extended-key-unknown",
    "title": "Unprocessable Entity",
    "status": 422,
    "detail": "extended.topk is not a setting for openchat/vllm; accepted: include_reasoning, min_p, reasoning_effort, top_k",
}
CUSTOM_RULE = "Use 1 to 32 letters, digits, dots, underscores or dashes."

PRELUDE = r"""
var CALLS = [], TOASTS = [], SAVED = [], CLOSED = [], PENDING = [], ASKED = [];
var LIST = { data: { items: [] }, error: null, loading: false };   // what window.primerApi.useResource answers
var ANSWER = null;                                                  // when set, apiFetch resolves with whatever it returns
var document = { addEventListener: function () {}, removeEventListener: function () {}, body: { style: {} } };
window.primerApi = {
  useEscape: function () {},
  useFocusTrap: function () {},
  useResource: function (key, fetcher, opts) {
    ASKED.push({ key: key, fetcher: fetcher, opts: opts || null });
    return { data: LIST.data, error: LIST.error, loading: LIST.loading, degraded: false, refetch: function () {} };
  },
  toastPush: function (t) { TOASTS.push(t); },
};
var SHR_UseShare = function () { return null; };
window.SHR_useBadge = function (access) { return access ? "Public" : null; };
window.SHR_UseShare = SHR_UseShare;
"""

# after api.js has loaded: it sets the real apiFetch, ApiError and readRefusal on window.primerApi, and only apiFetch is replaced
AFTER = r"""
window.primerApi.apiFetch = function (method, path, body) {
  CALLS.push([method, path, body === undefined ? null : JSON.parse(JSON.stringify(body))]);
  return ANSWER ? ANSWER() : Promise.resolve(body);
};
function typeInto(testid, value) {
  var el = MR.find(testid);
  if (!el) throw new Error("no element with data-testid " + testid);
  el.props.onChange({ target: value === true || value === false ? { checked: value } : { value: value } });
  MR.rerender();
}
function viaProp(testid, name, arg) {
  var el = MR.findProp("testid", testid);
  if (!el) throw new Error("no component with testid " + testid);
  el.props[name](arg);
  MR.rerender();
}
function pressHost(testid) {
  var out = [];
  (function walk(n) {
    if (n == null || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(walk); return; }
    if (!n.__el) return;
    out.push(n);
    if (typeof n.type === "function" && n.type !== React.Fragment) walk(n.out); else walk(n.children);
  })(MR.find(testid));
  var host = out.filter(function (n) { return n.type === "button"; })[0];
  if (!host) throw new Error("no button under " + testid);
  host.props.onClick({ preventDefault: function () {}, stopPropagation: function () {} });
  MR.rerender();
}
function textOf(node) {
  var out = [];
  (function collect(n) {
    if (n == null || typeof n === "boolean") return;
    if (typeof n === "string" || typeof n === "number") { out.push(String(n)); return; }
    if (Array.isArray(n)) { n.forEach(collect); return; }
    if (n.__el) collect(typeof n.type === "function" && n.type !== React.Fragment ? n.out : n.children);
  })(node);
  return out.join("");
}
function present(testid) { return MR.find(testid) !== null; }
function fieldError(label) { var f = MR.findProp("label", label); return f ? (f.props.error || null) : "no such field"; }
function fieldHelp(label) { var f = MR.findProp("label", label); return f ? (f.props.help || null) : "no such field"; }
"""

FILES = (
    UI / "components" / "shared.jsx",
    UI / "components" / "shared" / "form-field.jsx",
    UI / "components" / "shared" / "platform-kit.jsx",
    UI / "components" / "model-profile-settings.jsx",
)


def js(ctx, expr: str):
    """The value of a JS expression as Python data; ``undefined`` (a prop that was not set) is ``None``, as ``null`` is."""
    return json.loads(ctx.eval(f"(function () {{ var v = ({expr}); return JSON.stringify(v === undefined ? null : v); }})()"))


@pytest.fixture(scope="module")
def code() -> str:
    parts = [transpile(p) for p in FILES if p.exists()]
    parts.append((UI / "foundation" / "api.js").read_text(encoding="utf-8"))
    parts.append(transpile(UI / "components" / "model-profiles.jsx"))
    return "\n;\n".join(parts)


@pytest.fixture
def ctx(code) -> None:
    c = mini_react_context(code, PRELUDE)
    c.eval(AFTER)
    c.eval(f"var PROVIDERS = {json.dumps(PROVIDERS)};")
    try:
        yield c
    finally:
        c.close()


def mount(ctx, props: dict | None = None) -> None:
    """Mount ``MP_ProfileModal`` open, from an empty tree (a ``Nothing`` first, so the runtime does not keep a prior instance's state)."""
    ctx.eval(f"MR.mount(function Nothing() {{ return null; }}, {{}}); MR.mount(MP_ProfileModal, Object.assign("
             f"{{ open: true, existing: null, providers: [], onSaved: function (row) {{ SAVED.push(row); }}, "
             f"onClose: function () {{ CLOSED.push(1); }} }}, {json.dumps(props or {})}));")


def single(ctx, row: dict) -> None:
    mount(ctx, {"existing": row, "providers": PROVIDERS})


def answer(ctx, payload) -> None:
    """The catalogue fetch's answer: the row, or ``None`` for the stub's default (the request body, which a GET has none of)."""
    if payload is None:
        ctx.eval("ANSWER = null;")
    else:
        ctx.eval(f"ANSWER = function () {{ return Promise.resolve({json.dumps(payload)}); }};")


def calls(ctx) -> list:
    return js(ctx, "CALLS")


def toasts(ctx) -> list:
    return js(ctx, "TOASTS")


def settle(ctx) -> None:
    ctx.eval("MR.rerender()")


def typeInto(ctx, testid: str, value) -> None:
    """Type a control (or tick a checkbox with ``True``): the element's own onChange, then a render."""
    ctx.eval(f"typeInto({json.dumps(testid)}, {json.dumps(value)})")


def viaProp(ctx, testid: str, name: str, arg) -> None:
    ctx.eval(f"viaProp({json.dumps(testid)}, {json.dumps(name)}, {json.dumps(arg)})")


def pressHost(ctx, testid: str) -> None:
    """Press the real <button> a Btn draws (a busy Btn has swapped its handler for a refusal there)."""
    ctx.eval(f"pressHost({json.dumps(testid)})")


def present(ctx, testid: str) -> bool:
    return bool(js(ctx, f"present({json.dumps(testid)})"))


def fieldError(ctx, label: str):
    return js(ctx, f"fieldError({json.dumps(label)})")


# ---- the wiring and the plain helpers ---------------------------------------------------------------------------


def test_the_settings_file_loads_before_the_profiles_file() -> None:
    """The dispatch's bundle order: the settings form loads before the overlay that wires it."""
    assert 'src="components/model-profile-settings.jsx"' in INDEX
    assert INDEX.index("model-profile-settings.jsx") < INDEX.index("components/model-profiles.jsx")


def test_the_settings_file_declares_its_form_and_the_overlay_wires_it() -> None:
    assert "function MPS_SettingsForm(" in SETTINGS_SRC, "the form is declared in its own file"
    assert "MPS_SettingsForm" in PROFILES_SRC, "the overlay wires the form in"


def test_the_reasoning_options_gain_xhigh_max_and_custom(ctx) -> None:
    """Spec section 7: the presets include xhigh and max, and Custom opens the value field."""
    options = {o["value"]: o["label"] for o in js(ctx, "MP_REASONING_OPTIONS")}
    assert options["xhigh"] == "XHigh" and options["max"] == "Max" and options["custom"] == "Custom"
    assert set(options) == {"", "off", "minimal", "low", "medium", "high", "xhigh", "max", "custom"}


def test_the_reasoning_body_helper(ctx) -> None:
    """The draft's reasoning is the wire value: a preset as itself, Custom as {custom}, the vendor default as null."""
    assert js(ctx, 'MP_reasoningBody("high", "")') == "high"
    assert js(ctx, 'MP_reasoningBody("", "")') is None
    assert js(ctx, 'MP_reasoningBody("custom", "deep")') == {"custom": "deep"}
    assert js(ctx, 'MP_reasoningBody("custom", "a.b_c-d")') == {"custom": "a.b_c-d"}


def test_the_custom_reasoning_rule(ctx) -> None:
    """The same rule the server holds (spec section 4): 1 to 32 characters from [A-Za-z0-9_.-]."""
    for ok in ("high", "a", "a.b_c-d", "x" * 32):
        assert js(ctx, f"MP_customReasoningError({json.dumps(ok)})") is None, ok
    for bad in ("", "a b", "x" * 33, "a:b", "hïgh"):
        assert js(ctx, f"MP_customReasoningError({json.dumps(bad)})") == CUSTOM_RULE, bad


# ---- the catalogue fetch ----------------------------------------------------------------------------------------


def test_the_catalogue_is_fetched_on_open_and_when_the_provider_changes(ctx) -> None:
    answer(ctx, CATALOGUE)
    single(ctx, TUNED)
    settle(ctx)
    fetched = {(m, p) for m, p, _ in calls(ctx)}
    assert ("GET", "/v1/llm_providers/lp-vllm/model_settings") in fetched
    typeInto(ctx, "mp-single-provider", "lp-b")
    settle(ctx)
    fetched = {(m, p) for m, p, _ in calls(ctx)}
    assert ("GET", "/v1/llm_providers/lp-b/model_settings") in fetched


def test_the_loading_and_error_states_of_the_catalogue_fetch(ctx) -> None:
    ctx.eval("ANSWER = function () { return new Promise(function (resolve) { PENDING.push(resolve); }); };")
    single(ctx, TUNED)
    settle(ctx)
    assert present(ctx, "mp-settings-loading"), "while the fetch is out, the note is on screen"

    ctx.eval(f"ANSWER = function () {{ return Promise.reject(new window.primerApi.ApiError({json.dumps(BAD_KEY_422)})); }};")
    single(ctx, TUNED)
    settle(ctx)
    assert present(ctx, "mp-settings-error"), "a refused fetch is a Banner, not a silent form"
    # The JSON fold still works when the fetch failed: type the box and save through it.
    typeInto(ctx, "mp-single-extended", '{"top_k": 5}')
    pressHost(ctx, "mp-single-submit")
    settle(ctx)
    put = [body for m, p, body in calls(ctx) if m == "PUT"]
    assert put and put[0]["config"]["extended"] == {"top_k": 5}, put


def test_a_catalogue_with_no_settings_says_so(ctx) -> None:
    answer(ctx, NOSETTINGS)
    single(ctx, FRESH)
    settle(ctx)
    assert present(ctx, "mp-settings-empty")
    assert "This provider takes no extra settings" in js(ctx, "MR.texts()")


# ---- the typed controls ------------------------------------------------------------------------------------------


def test_one_control_per_setting_type(ctx) -> None:
    answer(ctx, CATALOGUE)
    single(ctx, FRESH)
    settle(ctx)
    for key in ("reasoning_effort", "chat_template_kwargs", "top_k", "min_p", "include_reasoning"):
        assert present(ctx, f"mp-setting-{key}"), key
    top_k = js(ctx, 'MR.find("mp-setting-top_k").props')
    assert top_k["type"] == "number" and top_k["min"] == 1 and top_k["max"] == 64 and top_k["step"] == 1
    assert js(ctx, 'MR.find("mp-setting-min_p").props.type') == "number"
    assert js(ctx, 'MR.find("mp-setting-include_reasoning").props.type') == "checkbox"
    # the open enum is a free entry datalisted from the catalogue's values; the object setting draws its field's sub-control
    effort = js(ctx, 'MR.find("mp-setting-reasoning_effort").props')
    assert effort["list"] == "mps-enum-reasoning_effort"
    assert js(ctx, 'MR.find("mps-enum-reasoning_effort").props.children.map(function (o) { return o.props.value; })') == ["low", "medium", "high"]
    assert present(ctx, "mp-setting-chat_template_kwargs-enable_thinking")


def test_the_control_help_names_its_wire_path_and_the_reasoning_writer(ctx) -> None:
    answer(ctx, CATALOGUE)
    single(ctx, FRESH)
    settle(ctx)
    assert "Sent to body.chat_template_kwargs." in js(ctx, 'fieldHelp("chat_template_kwargs")')
    assert "The Reasoning control also writes this." in js(ctx, 'fieldHelp("reasoning_effort")')
    assert "Sent to" not in js(ctx, 'fieldHelp("top_k")'), "a setting that lands on its own key says no path"


def test_the_body_box_is_drawn_only_when_the_wire_passes_the_body(ctx) -> None:
    answer(ctx, CATALOGUE)
    single(ctx, FRESH)
    settle(ctx)
    assert present(ctx, "mp-settings-body")
    answer(ctx, NOBODY)
    single(ctx, FRESH)
    settle(ctx)
    assert not present(ctx, "mp-settings-body")


# ---- the draft the save sends ------------------------------------------------------------------------------------


def test_saving_sends_the_custom_reasoning_and_the_typed_settings(ctx) -> None:
    answer(ctx, CATALOGUE)
    single(ctx, FRESH)
    settle(ctx)
    typeInto(ctx, "mp-setting-top_k", "20")
    typeInto(ctx, "mp-setting-min_p", "0.5")
    typeInto(ctx, "mp-setting-include_reasoning", True)
    typeInto(ctx, "mp-setting-reasoning_effort", "high")
    viaProp(ctx, "mp-single-reasoning", "onChange", "custom")
    typeInto(ctx, "mp-reasoning-custom", "deep")
    pressHost(ctx, "mp-single-submit")
    settle(ctx)
    put = [body for m, p, body in calls(ctx) if m == "PUT"]
    assert put, "the Save pressed"
    assert put[0]["config"] == {
        "reasoning": {"custom": "deep"},
        "extended": {"top_k": 20, "min_p": 0.5, "include_reasoning": True, "reasoning_effort": "high"},
    }, put[0]["config"]


def test_an_empty_control_removes_its_key(ctx) -> None:
    answer(ctx, CATALOGUE)
    single(ctx, TUNED)
    settle(ctx)
    assert js(ctx, 'MR.find("mp-setting-top_k").props.value') == "20", "the control starts from the stored value"
    typeInto(ctx, "mp-setting-top_k", "")
    pressHost(ctx, "mp-single-submit")
    settle(ctx)
    put = [body for m, p, body in calls(ctx) if m == "PUT"]
    assert put[0]["config"]["extended"] == {}, "an empty control removes its key"
    assert put[0]["config"]["reasoning"] == "high", "the level the form was not asked to change goes back as stored"


def test_the_fold_keeps_keys_the_catalogue_does_not_name(ctx) -> None:
    """One extended object: the fold still shows everything, including keys the typed form does not draw."""
    answer(ctx, CATALOGUE)
    single(ctx, TUNED)
    settle(ctx)
    typeInto(ctx, "mp-single-extended", '{"top_k": 5, "unknown_key": 1}')
    pressHost(ctx, "mp-single-submit")
    settle(ctx)
    put = [body for m, p, body in calls(ctx) if m == "PUT"]
    assert put[0]["config"]["extended"] == {"top_k": 5, "unknown_key": 1}
    assert not present(ctx, "mp-setting-unknown_key"), "the form draws only the keys the catalogue names"


def test_the_typed_controls_and_the_fold_edit_one_object(ctx) -> None:
    answer(ctx, CATALOGUE)
    single(ctx, TUNED)
    settle(ctx)
    typeInto(ctx, "mp-setting-top_k", "30")
    assert js(ctx, 'MR.find("mp-single-extended").props.value') == '{\n  "top_k": 30\n}', "the fold follows the form"
    typeInto(ctx, "mp-single-extended", '{"top_k": 40, "seed": 7}')
    assert js(ctx, 'MR.find("mp-setting-top_k").props.value') == "40", "the form follows the fold"


def test_the_body_box_writes_the_body_key(ctx) -> None:
    answer(ctx, CATALOGUE)
    single(ctx, FRESH)
    settle(ctx)
    typeInto(ctx, "mp-settings-body", '{"chat_template_kwargs": {"enable_thinking": false}}')
    pressHost(ctx, "mp-single-submit")
    settle(ctx)
    put = [body for m, p, body in calls(ctx) if m == "PUT"]
    assert put[0]["config"]["extended"] == {"body": {"chat_template_kwargs": {"enable_thinking": False}}}
    ctx.eval('MR.click("mp-single-extended-fold:toggle")')
    settle(ctx)
    assert js(ctx, 'MR.find("mp-single-extended").props.value') == (
        '{\n  "body": {\n    "chat_template_kwargs": {\n      "enable_thinking": false\n    }\n  }\n}'
    ), "the fold follows the body box"


# ---- the custom reasoning value ----------------------------------------------------------------------------------


def test_the_custom_input_shows_the_suggestions_and_a_stored_custom_is_selected(ctx) -> None:
    answer(ctx, CATALOGUE)
    single(ctx, FRESH)
    settle(ctx)
    viaProp(ctx, "mp-single-reasoning", "onChange", "custom")
    assert present(ctx, "mp-reasoning-custom"), "choosing Custom opens the value field"
    assert js(ctx, 'MR.find("mp-reasoning-suggestions").props.children.map(function (o) { return o.props.value; })') == ["low", "medium", "high"]

    single(ctx, CUSTOM_STORED)
    settle(ctx)
    assert js(ctx, 'MR.findProp("testid", "mp-single-reasoning").props.value') == "custom"
    assert js(ctx, 'MR.find("mp-reasoning-custom").props.value') == "deep", "a stored {custom} selects Custom and fills the input"
    pressHost(ctx, "mp-single-submit")
    settle(ctx)
    put = [body for m, p, body in calls(ctx) if m == "PUT"]
    assert put[0]["config"]["reasoning"] == {"custom": "deep"}


def test_an_invalid_custom_value_blocks_save(ctx) -> None:
    answer(ctx, CATALOGUE)
    single(ctx, FRESH)
    settle(ctx)
    viaProp(ctx, "mp-single-reasoning", "onChange", "custom")
    typeInto(ctx, "mp-reasoning-custom", "a b")
    assert fieldError(ctx, "Custom reasoning value") == CUSTOM_RULE
    assert js(ctx, 'MR.find("mp-single-submit").props.disabled') is True, "the value the server would refuse does not reach the wire"
    typeInto(ctx, "mp-reasoning-custom", "high")
    assert fieldError(ctx, "Custom reasoning value") is None
    assert js(ctx, 'MR.find("mp-single-submit").props.disabled') is False


# ---- the refusal toast and the aggregated form -------------------------------------------------------------------


def test_a_refused_save_toasts_the_message_and_keeps_the_draft(ctx) -> None:
    answer(ctx, CATALOGUE)
    single(ctx, FRESH)
    settle(ctx)
    ctx.eval(f"ANSWER = function () {{ return Promise.reject(new window.primerApi.ApiError({json.dumps(BAD_KEY_422)})); }};")
    pressHost(ctx, "mp-single-submit")
    settle(ctx)
    message = js(ctx, f"window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(BAD_KEY_422)})).message")
    assert toasts(ctx) == [{"kind": "error", "title": "Not saved", "detail": message}]
    assert js(ctx, 'textOf(MR.find("mp-single-error"))') == message, "the line above the footer stays the way refusals land"
    assert js(ctx, 'MR.findProp("testid", "mp-single-reasoning").props.value') == "", "the draft is kept"


def test_the_aggregated_form_has_no_model_settings_and_sends_no_config(ctx) -> None:
    ctx.eval(f"LIST = {{ data: {{ items: {json.dumps(ROWS)} }}, error: null, loading: false }};")
    mount(ctx)
    typeInto(ctx, "mp-aggregate-name", "fast-with-fallback")
    viaProp(ctx, "mp-aggregate-members", "onToggle", "p-a")
    viaProp(ctx, "mp-aggregate-members", "onToggle", "p-b")
    assert not present(ctx, "mp-single-settings"), "an aggregate shows no model settings"
    assert not present(ctx, "mp-single-reasoning") and not present(ctx, "mp-settings-body")
    pressHost(ctx, "mp-aggregate-submit")
    settle(ctx)
    post = [body for m, p, body in calls(ctx) if m == "POST"]
    assert post and "config" not in post[0], "an aggregated profile sends no config"


def test_the_aggregated_edit_sends_no_config_even_when_the_row_stored_one(ctx) -> None:
    ctx.eval(f"LIST = {{ data: {{ items: {json.dumps(ROWS)} }}, error: null, loading: false }};")
    mount(ctx, {"existing": AGG, "providers": PROVIDERS})
    pressHost(ctx, "mp-aggregate-submit")
    settle(ctx)
    put = [body for m, p, body in calls(ctx) if m == "PUT"]
    assert put, "the edit Save pressed"
    assert "config" not in put[0], "the stored row's config is dropped: the model's kind refuses a config on an aggregate"
    assert put[0]["members"] == ["p-a", "p-b"], "every other field the form draws goes back"
