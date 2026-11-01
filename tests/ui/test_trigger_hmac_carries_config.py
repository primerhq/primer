"""UI-H4: setting or clearing a webhook's HMAC secret must not touch its interactive mode.

The service replaces the trigger's whole config on a PUT (primer/trigger/service.py) and the
model fills the defaults for every field a PUT omits (interactive=False,
wait_timeout_seconds=60). So the two HMAC write paths on the trigger detail page must send the
webhook config whole, the way the edit dialog does: before the fix, adding a secret to an
interactive webhook (interactive on, wait 180 s) silently turned it non-interactive with a 60 s
wait, and so did clearing one.

The Set-dialog test drives the real ``TR_HmacSecretDialog`` (register harness, the g11_ux
pattern); the Clear test renders the REAL ``TR_TriggerDetail`` the way
``test_trigger_detail_hmac_section.py`` does (real api.js loaded first, the refusal reader
handed to the stand-in primerApi).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile
from tests.ui._register_harness import PRELUDE, base_code, transpiled, COMPONENTS

TRIGGERS = COMPONENTS / "triggers.jsx"

OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while OPEN_CONTEXTS:
        OPEN_CONTEXTS.pop().close()


STUBS = r"""
var NAVIGATED = [], CALLS = [];
window.primerApi.useRouter = function () {
  return { navigate: function (to) { NAVIGATED.push(to); }, params: {} };
};
window.primerApi.apiFetch = function (m, p, b) {
  CALLS.push({ m: m, p: p, b: b });
  return Promise.resolve(b == null ? { ok: true } : JSON.parse(JSON.stringify(b)));
};
window.confirmDialog = function (o) { return Promise.resolve(true); };
window.primerApi.toastPush = function (t) {};
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
// The Modal's host carries role="dialog": the walkable anchor for a button by its visible label.
function findBtn(label) {
  var hit = null;
  (function walk(n) {
    if (!n || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(walk); return; }
    if (n.__el) {
      if (n.type === "button" && textOf(n.props && n.props.children) === label) hit = n;
      if (typeof n.type === "function" && n.type !== React.Fragment) walk(n.out); else walk(n.children);
    }
  })(MR.findProp("role", "dialog") || {});
  return hit;
}
function byId(id) {
  var n = MR.findProp("id", id);
  if (!n) throw new Error("no control with id " + id);
  return n;
}
function change(id, value) {
  byId(id).props.onChange({ target: { value: value, checked: value } });
  MR.rerender();
}
function clickBtn(label) {
  var b = findBtn(label);
  if (!b) throw new Error("no button " + label);
  b.props.onClick({ preventDefault: function () {}, stopPropagation: function () {} });
  MR.rerender();
}
"""


def _ctx():
    code = base_code() + "\n" + transpiled(TRIGGERS)
    ctx = mini_react_context(code, PRELUDE)
    ctx.eval(STUBS)
    OPEN_CONTEXTS.append(ctx)
    return ctx


def _put_bodies(ctx) -> list:
    calls = json.loads(ctx.eval("JSON.stringify(CALLS)"))
    return [c["b"] for c in calls if c["m"] == "PUT" and c["p"] == "/triggers/t-1"]


# ---- the Set dialog ------------------------------------------------------------------


def test_the_set_hmac_put_carries_the_interactive_mode_and_wait_bound() -> None:
    ctx = _ctx()
    ctx.eval(
        "MR.mount(window.TR_HmacSecretDialog, { triggerId: 't-1', "
        "config: { kind: 'webhook', token: 'tok-1', hmac_secret: null, "
        "interactive: true, wait_timeout_seconds: 180 }, "
        "onClose: function () {}, onSaved: function () {} });"
    )
    assert ctx.eval('MR.findProp("id", "tr-hmac-secret") != null'), "the dialog renders its secret field"
    ctx.eval('change("tr-hmac-secret", "s3cr3t");')
    ctx.eval('clickBtn("Save secret");')
    bodies = _put_bodies(ctx)
    assert bodies, f"the dialog PUTs the trigger: {ctx.eval('JSON.stringify(CALLS)')}"
    cfg = bodies[0]["config"]
    assert cfg["hmac_secret"] == "s3cr3t"
    assert cfg.get("interactive") is True, (
        f"the PUT must carry the trigger's current interactive mode, got config {cfg}"
    )
    assert cfg.get("wait_timeout_seconds") == 180, (
        f"the PUT must carry the trigger's current wait bound, got config {cfg}"
    )


# ---- the Clear button -----------------------------------------------------------------


API = (Path(__file__).resolve().parents[2] / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")

_DETAIL_PRELUDE = API + """
var __readRefusal = window.primerApi.readRefusal;
window.location = { origin: "http://x", search: "", hash: "", pathname: "/console/" };
var document = { title: "", addEventListener: function () {}, removeEventListener: function () {} };
var __trigger = { id: "t1", slug: "hook", name: "hook", enabled: true, config: { kind: "webhook", token: "a".repeat(32), hmac_secret: "set", interactive: true, wait_timeout_seconds: 180 }, owner: null };
var __bodies = [];
globalThis.confirmDialog = function () { return Promise.resolve(true); };
globalThis.Btn = function (props) { return React.createElement("button", props, props.children); };
globalThis.Icon = function () { return null; };
globalThis.Banner = function () { return null; };
globalThis.Modal = function (props) { return React.createElement("div", { "data-testid": "modal" }, props.children, props.footer); };
globalThis.SHR_useShareOpen = function () { return [false, function () {}]; };
globalThis.SHR_ShareDialog = function () { return null; };
globalThis.SHR_useUserNames = function () { return {}; };
globalThis.SHR_AccessMarkers = function () { return null; };
window.primerApi = {
  readRefusal: __readRefusal,
  apiFetch: function (method, path, body) {
    if (method === "PUT") __bodies.push(body);
    return Promise.resolve({});
  },
  useRouter: function () { return { navigate: function () {} }; },
  useResource: function (key) {
    return key.indexOf("trigger-subs:") === 0
      ? { data: { items: [] }, loading: false, error: null, refetch: function () {} }
      : { data: __trigger, loading: false, error: null, refetch: function () {} };
  },
};
"""


@pytest.fixture
def detail():
    ctx = mini_react_context(transpile(TRIGGERS), _DETAIL_PRELUDE)
    ctx.eval('MR.mount(TR_TriggerDetail, { id: "t1" })')
    yield ctx
    ctx.close()


def _click_clear(ctx) -> None:
    ctx.eval("MR.click('clear-hmac-btn')")
    ctx.eval("MR.rerender()")
    ctx.eval("MR.rerender()")


def test_a_clear_put_carries_the_interactive_mode_and_wait_bound(detail) -> None:
    _click_clear(detail)
    bodies = json.loads(detail.eval("JSON.stringify(__bodies)"))
    assert bodies, "the Clear button PUTs the trigger"
    cfg = bodies[0]["config"]
    assert cfg["hmac_secret"] is None, "the secret is cleared"
    assert cfg.get("interactive") is True, (
        f"clearing the secret must keep the current interactive mode, got config {cfg}"
    )
    assert cfg.get("wait_timeout_seconds") == 180, (
        f"clearing the secret must keep the current wait bound, got config {cfg}"
    )
    assert cfg.get("token") == "a" * 32, "the token round-trips as read"


# ---- characterization: the edit dialog already sends the config whole ------------------


def test_the_edit_dialog_webhook_put_round_trips_the_hmac_mask() -> None:
    """What the two HMAC paths must do is what the edit dialog already does: send the config whole,
    the stored secret's echo (the mask) included. This pins that baseline before the fix."""
    ctx = _ctx()
    ctx.eval(
        "MR.mount(window.TR_TriggerEditDialog, { trigger: { id: 't-1', slug: 'hook', "
        "name: 'N', description: 'd', enabled: true, "
        "config: { kind: 'webhook', token: 'tok-1', hmac_secret: 'mask-abc123', "
        "interactive: true, wait_timeout_seconds: 180 } }, "
        "onClose: function () {}, onSaved: function () {} });"
    )
    ctx.eval('clickBtn("Save changes");')
    bodies = _put_bodies(ctx)
    assert bodies, "the edit dialog PUTs the trigger"
    cfg = bodies[0]["config"]
    assert cfg["hmac_secret"] == "mask-abc123", "the stored echo round-trips (the server keeps the secret)"
    assert cfg.get("interactive") is True
    assert cfg.get("wait_timeout_seconds") == 180
    assert cfg.get("token") == "tok-1"
