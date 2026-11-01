"""UI-L12: retrying "Create rule" after its subscription POST failed must not re-POST the trigger.

CR_RuleModal lazily creates the room's channel trigger, then the rule's subscription. When the
subscription POST failed, the created trigger lived only in the submit closure: the dialog
stayed open, ``allTriggers`` was not refreshed, and the retry re-POSTed the same slug and hit
409 trigger_slug_conflict, stranding an orphan trigger and the operator's filled-in fields.
The fix keeps the created trigger in a ref (reused when it matches the room) and refreshes the
trigger list as soon as the trigger POST succeeds.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._mini_react import mini_react_context, transpile
from tests.ui._register_harness import COMPONENTS

CHANNEL_RULES = COMPONENTS / "channel_rules.jsx"

OPEN: list = []

PRELUDE_JS = r"""
var document = { addEventListener: function () {}, removeEventListener: function () {} };
var CALLS = [], TOASTS = [];
var CHANGED = 0;
var __triggerCreated = { id: "tr-1", slug: "ch-prov-all", config: { kind: "channel", provider_id: "prov" } };
var __subAttempts = 0;
window.primerApi = {
  apiFetch: function (m, p, b) {
    CALLS.push({ m: m, p: p, b: b });
    if (m === "POST" && p === "/triggers") return Promise.resolve(__triggerCreated);
    if (m === "POST" && p.indexOf("/subscriptions") >= 0) {
      __subAttempts += 1;
      // The first subscription POST fails; the retry's must succeed.
      if (__subAttempts === 1) return Promise.reject({ title: "Conflict", detail: "binding already exists" });
      return Promise.resolve({});
    }
    return Promise.resolve({});
  },
  useResource: function () { return { data: { items: [] }, loading: false, error: null, refetch: function () {} }; },
  useMutation: function (fn) { return { mutate: function () { return Promise.resolve(); }, loading: false }; },
  useViewport: function () { return { isMobile: false }; },
};
window.confirmDialog = function (o) { return Promise.resolve(true); };
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], disabled: p.disabled, onClick: p.onClick }, p.children); }
function Modal(p) { return React.createElement("div", { "data-testid": "modal" }, p.children, p.footer); }
function Banner(p) { return React.createElement("div", { "data-testid": "banner" }, p.title); }
function Icon() { return null; }
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
// Walk the modal for a <button> by its visible label and click it.
function clickBtn(label) {
  var hit = null;
  (function walk(n) {
    if (!n || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(walk); return; }
    if (n.__el) {
      if (n.type === "button" && textOf(n.props && n.props.children) === label) hit = n;
      if (typeof n.type === "function" && n.type !== React.Fragment) walk(n.out); else walk(n.children);
    }
  })(MR.find("modal"));
  if (!hit) throw new Error("no button " + label);
  hit.props.onClick({ preventDefault: function () {}, stopPropagation: function () {} });
  MR.rerender();
}
// The action fields render as <label><span>{label}</span><input/></label> with no ids: find the
// input by its label text and drive its onChange.
function setField(labelText, value) {
  var hit = null;
  (function walk(n) {
    if (!n || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(walk); return; }
    if (n.__el) {
      if (n.type === "label" && textOf(n.children) === labelText) {
        (function findInput(k) {
          if (!k || typeof k !== "object") return;
          if (Array.isArray(k)) { k.forEach(findInput); return; }
          if (k.__el) { if (k.type === "input") hit = k; else findInput(k.children); }
        })(n.children);
      }
      if (typeof n.type === "function" && n.type !== React.Fragment) walk(n.out); else walk(n.children);
    }
  })(MR.find("modal"));
  if (!hit) throw new Error("no field " + labelText);
  hit.props.onChange({ target: { value: value, checked: value } });
  MR.rerender();
}
"""


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while OPEN:
        OPEN.pop().close()


def _ctx():
    ctx = mini_react_context(transpile(CHANNEL_RULES), PRELUDE_JS)
    OPEN.append(ctx)
    return ctx


def _mount(ctx) -> None:
    ctx.eval(
        "MR.mount(CR_RuleModal, { providers: [{ id: 'prov', provider: 'slack' }], "
        "allTriggers: [], roomStartsSessionsById: function () { return true; }, "
        "initialProviderId: 'prov', onClose: function () {}, "
        "onChanged: function () { CHANGED += 1; }, "
        "pushToast: function (t) { TOASTS.push(t); } });"
    )
    # The default action (agent_fresh_session) needs both of its fields before the button enables.
    ctx.eval('setField("Workspace id", "ws-1");')
    ctx.eval('setField("Agent id", "ag-1");')


def _calls(ctx) -> list:
    return json.loads(ctx.eval("JSON.stringify(CALLS)"))


def test_a_retried_rule_create_makes_no_second_trigger_post() -> None:
    ctx = _ctx()
    _mount(ctx)
    ctx.eval('clickBtn("Create rule");')
    first = _calls(ctx)
    assert [c for c in first if c["m"] == "POST" and c["p"] == "/triggers"], (
        "the first submit creates the room's channel trigger"
    )
    assert ctx.eval("TOASTS.length") == 1, "the failed subscription POST toasts"
    assert ctx.eval('!!MR.find("modal")'), "the dialog stays open so the operator keeps their fields"
    ctx.eval('clickBtn("Create rule");')
    second = _calls(ctx)
    trigger_posts = [c for c in second if c["m"] == "POST" and c["p"] == "/triggers"]
    assert len(trigger_posts) == 1, (
        f"the retry must reuse the created trigger, not re-POST the slug: {second}"
    )
    subs = [c for c in second if c["m"] == "POST" and "/subscriptions" in c["p"]]
    assert len(subs) == 2 and subs[1]["p"] == "/triggers/tr-1/subscriptions", (
        f"the retry posts the subscription to the same trigger: {second}"
    )
    assert ctx.eval("CHANGED") >= 1, "the trigger list is refreshed once the trigger exists"


def test_a_successful_create_posts_the_trigger_once_and_refreshes() -> None:
    """Characterization of the happy path the fix must not disturb."""
    ctx = _ctx()
    ctx.eval("__subAttempts = 99;")  # let the subscription POST succeed
    _mount(ctx)
    ctx.eval('clickBtn("Create rule");')
    calls = _calls(ctx)
    assert len([c for c in calls if c["m"] == "POST" and c["p"] == "/triggers"]) == 1
    assert len([c for c in calls if c["m"] == "POST" and "/subscriptions" in c["p"]]) == 1
    assert ctx.eval("CHANGED") >= 1
