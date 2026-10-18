"""The MiniRacer harness of the Register provider overlay's unit tests (plan 02, Tasks 9 to 11).

A context holds the REAL ``ui/foundation/api.js`` (``ApiError``, ``readRefusal``), ``shared.jsx`` (``Modal``, ``Btn``, ``Icon``), ``shared/form-field.jsx``, the
platform kit, ``provider-form.jsx`` and the register files, transpiled the way the server bundles them, on the hook runtime of ``tests/ui/_mini_react.py``.
Only the network and the hooks that read the page around a component are stubbed (as ``tests/ui/test_provider_form_save.py`` does):

* ``useResource`` answers from a table keyed by the cache key (``serve(key, data)``, ``fail(key, envelope)``); ``KEYS`` records every key asked for, and
  ``RES[key].refetched`` counts the refetches;
* ``apiFetch`` records ``{method, path, body}`` in ``CALLS`` and answers through ``HANDLER(method, path, body)``, which a test may replace (the default echoes
  the body, as a create answers the row it stored);
* ``toastPush`` records in ``TOASTS``; ``window.confirmDialog`` records in ``CONFIRMS`` and answers ``CONFIRM_ANSWER``.

Driver helpers: ``setValue(testid, value)`` types into a control, ``press(testid)`` clicks a button and tracks the promise its handler returned (``SETTLED``),
``textOf(node)``, ``ALL(testid)`` (every element at or under one), ``fieldRow(rootTestid, controlTestid)`` (the direct children of the kit field that holds a
control: its label, the control, then the help and the error, in order).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
COMPONENTS = UI / "components"
API = (UI / "foundation" / "api.js").read_text(encoding="utf-8")

# Load order of the files under test, as ui/index.html loads them.
BASE_FILES = (
    COMPONENTS / "shared.jsx",
    COMPONENTS / "shared" / "form-field.jsx",
    COMPONENTS / "shared" / "platform-kit.jsx",
    COMPONENTS / "provider-form.jsx",
    COMPONENTS / "providers" / "register-common.jsx",
)
OVERLAY_FILE = COMPONENTS / "providers" / "register-overlay.jsx"

PRELUDE = r"""
var __layers = [];
var MOBILE = false;
window.primerApi = {
  useEscape: function (h, a) { __layers.push({ handler: h, active: a === undefined ? true : !!a }); },
  useFocusTrap: function () {}, useViewport: function () { return { isMobile: MOBILE }; },
};
var document = { addEventListener: function () {}, removeEventListener: function () {}, body: { style: {} }, activeElement: null };
window.addEventListener = function () {}; window.removeEventListener = function () {};
"""

DRIVER = r"""
var CALLS = [], TOASTS = [], CONFIRMS = [], KEYS = [];
var RES = {};
function __entry(key) {
  if (!RES[key]) RES[key] = { data: undefined, error: null, loading: true, refetched: 0 };
  return RES[key];
}
window.primerApi.useResource = function (key, fetcher) {
  KEYS.push(key);
  var e = __entry(key);
  e.fetcher = fetcher;
  return { data: e.data, error: e.error, loading: e.loading, refetch: function () { e.refetched += 1; } };
};
function serve(key, data) { var e = __entry(key); e.data = data; e.error = null; e.loading = false; }
function fail(key, envelope, extra) {
  var e = __entry(key);
  e.error = new window.primerApi.ApiError(envelope);
  e.loading = false;
  Object.assign(e, extra || {});
}
window.primerApi.useCapabilities = function () { return { data: { extras: {} } }; };
window.primerApi.capabilityHint = function (x) { return String(x); };
window.primerApi.EXTRA_FOR_PROVIDER_TYPE = {};
var HANDLER = function (method, path, body) {
  return Promise.resolve(body == null ? { ok: true } : JSON.parse(JSON.stringify(body)));
};
window.primerApi.apiFetch = function (method, path, body) {
  CALLS.push({ method: method, path: path, body: body === undefined || body === null ? null : JSON.parse(JSON.stringify(body)) });
  return HANDLER(method, path, body);
};
window.primerApi.toastPush = function (t) { TOASTS.push(t); };
var CONFIRM_ANSWER = true;
window.confirmDialog = function (o) { CONFIRMS.push(o); return Promise.resolve(CONFIRM_ANSWER); };
function refuse(envelope) { return Promise.reject(new window.primerApi.ApiError(envelope)); }
function el(type, props) { return React.createElement.apply(null, [type, props || null].concat([].slice.call(arguments, 2))); }
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
function ALL(testid) {
  var out = [];
  (function walk(n) {
    if (n == null || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(walk); return; }
    if (!n.__el) return;
    out.push(n);
    if (typeof n.type === "function" && n.type !== React.Fragment) walk(n.out); else walk(n.children);
  })(MR.find(testid));
  return out;
}
function __flat(kids) {
  var out = [];
  (function add(k) {
    if (Array.isArray(k)) { k.forEach(add); return; }
    if (k && k.__el) out.push(k);
  })(kids);
  return out;
}
function fieldRow(rootTestid, testid) {
  var hit = null;
  ALL(rootTestid).forEach(function (n) {
    if (hit || n.type !== "div" || String(n.props.className || "").indexOf("pk-field") < 0) return;
    var kids = __flat(n.children);
    if (!kids.some(function (c) { return c.props && c.props["data-testid"] === testid; })) return;
    hit = kids.map(function (c) {
      return { type: typeof c.type === "string" ? c.type : "component", className: c.props.className || "", testid: c.props["data-testid"] || null,
               role: c.props.role || null, text: textOf(c) };
    });
  });
  return hit;
}
function setValue(testid, value) {
  var node = MR.find(testid);
  if (!node) throw new Error("no element with data-testid " + testid);
  node.props.onChange({ target: { value: value, checked: value }, preventDefault: function () {}, stopPropagation: function () {} });
  MR.rerender();
}
var SETTLED = null;
function press(testid) {
  var node = MR.find(testid);
  if (!node) throw new Error("no element with data-testid " + testid);
  SETTLED = "pending";
  var out = node.props.onClick({ preventDefault: function () {}, stopPropagation: function () {} });
  Promise.resolve(out).then(function () { SETTLED = "fulfilled"; }, function () { SETTLED = "rejected"; });
  MR.rerender();
}
"""


def transpiled(*paths: Path) -> str:
    """The files as the server's bundler would emit them, in the order given."""
    return "\n".join(transpile(p) for p in paths)


def base_code(with_overlay: bool = False) -> str:
    files = BASE_FILES + ((OVERLAY_FILE,) if with_overlay else ())
    return API + "\n" + transpiled(*files)


def context(code: str):
    """A fresh context: the hook runtime, the prelude, the code, then the stubs that replace the network."""
    ctx = mini_react_context(code, PRELUDE)
    ctx.eval(DRIVER)
    return ctx


def js(ctx, expr: str):
    """The value of a JS expression, through JSON."""
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def settle(ctx) -> str:
    """Let the promises a handler started run out, then render what they changed."""
    ctx.eval("void 0;")
    ctx.eval("MR.rerender();")
    ctx.eval("void 0;")
    ctx.eval("MR.rerender();")
    return ctx.eval("SETTLED")


def types_of(plural: str) -> dict:
    """What ``GET /v1/<plural>/_types`` answers: the route function itself, so a server-side change changes what the tests read."""
    import asyncio

    from primer.api.routers import artifact_storage, providers, semantic_search, speech, web_fetch, web_search

    routes = {
        "llm_providers": providers.list_llm_provider_types,
        "embedding_providers": providers.list_embedding_provider_types,
        "cross_encoder_providers": providers.list_cross_encoder_provider_types,
        "artifact_storage_providers": artifact_storage.list_artifact_storage_types,
        "ssp": semantic_search.list_semantic_search_provider_types,
        "stt_providers": speech.list_stt_types,
        "tts_providers": speech.list_tts_types,
        "web_fetch_providers": web_fetch.list_provider_types,
        "web_search_providers": web_search.list_provider_types,
    }
    return asyncio.run(routes[plural]())
