"""UI-L14: a trigger's description can be cleared from the edit dialog.

The edit dialog sent ``description: description || null`` and the service ignores ``None``
(``if description is not None``), so clearing the field reported success and the old
description stayed. The dialog now sends the description as typed: an empty string clears.
The create dialog sends it the same way, for one rule across both forms.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context
from tests.ui._register_harness import PRELUDE, base_code, transpiled, COMPONENTS

TRIGGERS = COMPONENTS / "triggers.jsx"

OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while OPEN_CONTEXTS:
        OPEN_CONTEXTS.pop().close()


STUBS = r"""
var CALLS = [];
window.primerApi.useRouter = function () {
  return { navigate: function (to) {}, params: {} };
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
function change(id, value) {
  var n = MR.findProp("id", id);
  if (!n) throw new Error("no control with id " + id);
  n.props.onChange({ target: { value: value, checked: value } });
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


def test_clearing_the_description_sends_an_empty_string() -> None:
    ctx = _ctx()
    ctx.eval(
        "MR.mount(window.TR_TriggerEditDialog, { trigger: { id: 't-1', slug: 'nightly', "
        "name: 'N', description: 'the old one', enabled: true, "
        "config: { kind: 'scheduled', cron: '0 2 * * *', timezone: 'UTC', catchup: 'one' } }, "
        "onClose: function () {}, onSaved: function () {} });"
    )
    assert ctx.eval('MR.findProp("id", "tr-edit-description").props.value') == "the old one"
    ctx.eval('change("tr-edit-description", "");')
    ctx.eval('clickBtn("Save changes");')
    calls = json.loads(ctx.eval("JSON.stringify(CALLS)"))
    put = [c for c in calls if c["m"] == "PUT" and c["p"] == "/triggers/t-1"]
    assert put, f"the edit dialog PUTs the trigger: {calls}"
    assert put[0]["b"]["description"] == "", (
        f"a cleared description must be sent as an empty string, got {put[0]['b']}"
    )


def test_a_typed_description_still_sends_what_was_typed() -> None:
    """Characterization: the fix must not turn a kept description into something else."""
    ctx = _ctx()
    ctx.eval(
        "MR.mount(window.TR_TriggerEditDialog, { trigger: { id: 't-1', slug: 'nightly', "
        "name: 'N', description: 'the old one', enabled: true, "
        "config: { kind: 'scheduled', cron: '0 2 * * *', timezone: 'UTC', catchup: 'one' } }, "
        "onClose: function () {}, onSaved: function () {} });"
    )
    ctx.eval('change("tr-edit-description", "the new one");')
    ctx.eval('clickBtn("Save changes");')
    calls = json.loads(ctx.eval("JSON.stringify(CALLS)"))
    put = [c for c in calls if c["m"] == "PUT" and c["p"] == "/triggers/t-1"]
    assert put[0]["b"]["description"] == "the new one"
