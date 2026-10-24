"""A picker offers every row, past the first 200 (ticket 01a16dfa): the trigger subscription dialog's workspace and agent dropdowns.

``TR_SubscriptionDialog`` (``ui/components/triggers.jsx``) read ``/workspaces?limit=200``, ``/agents?limit=200`` and ``/graphs?limit=200``, so a 201st
workspace or agent could not be the target of a subscription. It now reads every page with the foundation's ``PR_fetchAllPages``. The REAL dialog is
rendered on the hook runtime, transpiled the way the server bundles it, after the REAL ``ui/foundation/api.js``, against a fake ``apiFetch`` that pages
the way the server does and a ``useResource`` that runs each fetcher once and keeps its answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
TRIGGERS = ROOT / "ui" / "components" / "triggers.jsx"
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")

_PRELUDE = API + r"""
var __readRefusal = window.primerApi.readRefusal, __ApiError = window.primerApi.ApiError;
var document = { title: "", addEventListener: function () {}, removeEventListener: function () {} };
globalThis.Btn = function (props) { return React.createElement("button", props, props.children); };
globalThis.Icon = function () { return null; };
globalThis.Banner = function () { return null; };
globalThis.FormField = function (props) { return React.createElement("div", null, props.children); };
globalThis.Modal = function (props) { return React.createElement("div", { "data-testid": "modal" }, props.children, props.footer); };
var COUNTS = { workspaces: 450, agents: 450, graphs: 2 };
var PREFIX = { workspaces: "w", agents: "agent", graphs: "graph" };
var FAIL_FROM = {};
var CALLS = [];
var RES = {};
window.primerApi = {
  readRefusal: __readRefusal, ApiError: __ApiError,
  apiFetch: function (method, path) {
    CALLS.push(method + " " + path);
    var m = /^\/(workspaces|agents|graphs)\?limit=(\d+)&offset=(\d+)$/.exec(path);
    if (!m) return Promise.resolve({ items: [] });
    var name = m[1], limit = Number(m[2]), offset = Number(m[3]);
    if (FAIL_FROM[name] != null && offset >= FAIL_FROM[name]) {
      return Promise.reject(new __ApiError({ status: 502, title: "Bad Gateway", detail: "the worker went away" }));
    }
    var n = Math.max(0, Math.min(limit, COUNTS[name] - offset)), items = [];
    for (var i = 0; i < n; i++) items.push({ id: PREFIX[name] + "-" + (offset + i) });
    return Promise.resolve({ kind: "offset", offset: offset, length: n, total: COUNTS[name], items: items });
  },
  useResource: function (key, fetcher) {
    var e = RES[key];
    if (!e) {
      e = RES[key] = { data: undefined };
      fetcher(null).then(function (d) { e.data = d; }, function (err) { e.error = err; });
    }
    return { data: e.data, error: e.error || null, loading: e.data === undefined, refetch: function () {} };
  },
};
function options(id) {
  var select = MR.findProp("id", id);
  var out = [];
  (function walk(n) {
    if (n == null || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(walk); return; }
    if (n.__el && n.type === "option") out.push(n.props.value);
    if (n.__el) walk(n.children);
  })(select.children);
  return out;
}
"""


@pytest.fixture
def dialog():
    ctx = mini_react_context(transpile(TRIGGERS), _PRELUDE)
    try:
        yield ctx
    finally:
        ctx.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def settle(ctx) -> None:
    """Draw what the reads answered: each eval ends with V8's microtask queue drained, so a paged read's chain of pages lands between two evals."""
    for _ in range(3):
        ctx.eval("MR.rerender();")


def _mount(ctx) -> None:
    ctx.eval('MR.mount(TR_SubscriptionDialog, { triggerId: "t1", mode: "create", initial: null, onClose: function () {}, onSaved: function () {} });')
    settle(ctx)


def test_the_workspace_dropdown_offers_every_workspace(dialog):
    _mount(dialog)
    values = js(dialog, "options('tr-sub-workspace')")
    assert values[0] == "" and len(values) == 451 and values[-1] == "w-449"
    assert [c for c in js(dialog, "CALLS") if c.startswith("GET /workspaces")] == [
        "GET /workspaces?limit=200&offset=0", "GET /workspaces?limit=200&offset=200", "GET /workspaces?limit=200&offset=400"]
    assert js(dialog, "MR.find('tr-sub-workspaces-note')") is None


def test_the_agent_dropdown_offers_every_agent_once_a_workspace_is_picked(dialog):
    _mount(dialog)
    dialog.eval("MR.findProp('id', 'tr-sub-workspace').props.onChange({ target: { value: 'w-449' } });")
    settle(dialog)
    values = js(dialog, "options('tr-sub-agent')")
    assert len(values) == 451 and "agent-449" in values


def test_a_dropdown_cut_short_says_so_under_it(dialog):
    dialog.eval("FAIL_FROM.workspaces = 200;")
    _mount(dialog)
    assert len(js(dialog, "options('tr-sub-workspace')")) == 201
    assert js(dialog, "MR.find('tr-sub-workspaces-note').props.children") == (
        "Showing the first 200 workspaces; the rest did not load: the worker went away")


# ---- a dialog handed a list: the new-channel dialog's provider select ------------------------------------------------------------------------------------

CHANNELS = ROOT / "ui" / "components" / "channels.jsx"

_CHANNEL_PRELUDE = API + "\n" + (ROOT / "ui" / "foundation" / "use-mutation.js").read_text(encoding="utf-8") + r"""
globalThis.Modal = function (props) { return React.createElement("div", { "data-testid": "modal" }, props.children, props.footer); };
globalThis.Btn = function (props) { return React.createElement("button", { onClick: props.onClick }, props.children); };
globalThis.Icon = function () { return null; };
globalThis.FormField = function (props) { return React.createElement("div", null, props.children); };
window.primerApi.apiFetch = function () { return Promise.resolve({}); };
window.primerApi.useResource = function () { return { data: null, loading: false, error: null, refetch: function () {} }; };
"""


def test_the_new_channel_dialog_says_a_provider_list_cut_short():
    """The host (Platform > Channels' create, or the Channels page) reads every channel provider and hands the read over as ``providersList``: a read cut
    short is said under the provider select, a whole one says nothing."""
    ctx = mini_react_context(transpile(CHANNELS), _CHANNEL_PRELUDE)
    try:
        ctx.eval("MR.mount(NewChannelModal, { providers: [{ id: 'cp-1', provider: 'discord' }], providersList: { items: [{ id: 'cp-1' }], truncated: false },"
                 " onClose: function () {}, onCreated: function () {}, pushToast: function () {} });")
        assert js(ctx, "MR.find('ch-new-providers-note')") is None
        ctx.eval("MR.rerender({ providers: [{ id: 'cp-1', provider: 'discord' }], providersList: { items: [{ id: 'cp-1' }], truncated: true, serverTotal: null,"
                 " error: 'the store is down' }, onClose: function () {}, onCreated: function () {}, pushToast: function () {} });")
        assert js(ctx, "MR.find('ch-new-providers-note').props.children") == (
            "Showing the first 1 channel provider; the rest did not load: the store is down")
    finally:
        ctx.close()
