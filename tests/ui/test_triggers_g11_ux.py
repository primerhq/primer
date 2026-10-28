"""Red probes for the G11 triggers console batch (G11-01..05).

The audit (/var/tmp/revamp/audit/G11.md) found the console under-exposing and
mis-promising what the trigger service already does:

* G11-01 the catchup "all" label promises one fire per missed tick while the
  tick planner caps replay at 64 (oldest first);
* G11-02 the edit dialog edits only name/description/enabled and tells the
  operator to delete + recreate to change a schedule, while the service
  updates a same-kind config in place (only the kind discriminator is
  immutable);
* G11-03 the fire-result session id renders as a bare span where every other
  surface links sessions;
* G11-04 a channel-kind trigger gets the "no schedule" dash and no metadata
  row on the generic surface;
* G11-05 the webhook ``interactive`` / ``wait_timeout_seconds`` config is
  never exposed by the console.

MiniRacer over the real source, scalar eval returns only; the harness is the
register-overlay one (``tests/ui/_register_harness.py``) with the triggers
file appended, driven through ``data-testid``/``id`` props and the Modal's
``role="dialog"`` anchor.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.ui._mini_react import mini_react_context
from tests.ui._register_harness import PRELUDE, base_code, transpiled, COMPONENTS

TRIGGERS = COMPONENTS / "triggers.jsx"
SRC = TRIGGERS.read_text(encoding="utf-8")

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


def _collapsed() -> str:
    return re.sub(r"\s+", " ", SRC)


# ---- G11-01: the catchup "all" label names the 64 cap ----------------------------


def test_the_catchup_all_option_names_the_cap():
    m = re.search(r'\{\s*value:\s*"all",\s*label:\s*"([^"]*)"', SRC)
    assert m, "TR_CATCHUP_OPTIONS all option"
    assert "64" in m.group(1), m.group(1)


# ---- G11-02: the edit dialog edits a same-kind config in place --------------------


def test_the_delete_then_recreate_copy_is_gone_from_the_dialog():
    dialog = SRC[SRC.index("function TR_TriggerEditDialog("):]
    dialog = dialog[: dialog.index("\n// =====", 10)]
    assert re.search(r"delete\s+this\s+trigger\s+and\s+create\s+a\s+new\s+one", dialog, re.I) is None, (
        "the old immutable-copy must go: the service updates a same-kind config in place"
    )


def test_the_edit_dialog_carries_the_changed_schedule_in_the_put_body():
    ctx = _ctx()
    ctx.eval(
        "MR.mount(window.TR_TriggerEditDialog, { trigger: { id: 't-1', slug: 'nightly', "
        "name: 'N', description: 'd', enabled: true, config: { kind: 'scheduled', "
        "cron: '0 2 * * *', timezone: 'UTC', catchup: 'one' } }, "
        "onClose: function () {}, onSaved: function () {} });"
    )
    assert ctx.eval('MR.findProp("id", "tr-edit-cron") != null'), (
        "the edit dialog renders a cron field for a scheduled trigger"
    )
    ctx.eval('change("tr-edit-cron", "0 3 * * *");')
    ctx.eval('clickBtn("Save changes");')
    ctx.eval("void 0;")
    ctx.eval("void 0;")
    calls = json.loads(ctx.eval("JSON.stringify(CALLS)"))
    put = [c for c in calls if c["m"] == "PUT" and c["p"] == "/triggers/t-1"]
    assert put, f"the edit dialog PUTs the trigger: {calls}"
    body = put[0]["b"]
    assert body["config"]["kind"] == "scheduled"
    assert body["config"]["cron"] == "0 3 * * *"
    assert body["name"] == "N" and body["enabled"] is True


# ---- G11-03: the fire result links the session it started ------------------------


def test_the_fire_result_artefact_deep_links_the_session():
    collapsed = _collapsed()
    i = collapsed.index("r.artefact_id && (")
    block = collapsed[i : i + 400]
    assert "onClick" in block and '"/sessions/"' in block, (
        "the fired session id must link to /sessions/<id> like every other session-naming surface"
    )


# ---- G11-04: channel triggers describe themselves on the generic surface ---------


def test_the_schedule_label_names_the_channel_binding():
    ctx = _ctx()
    label = json.loads(ctx.eval('JSON.stringify(TR_scheduleLabel({ config: { kind: "channel", provider_id: "p1", channel_id: "c1" } }))'))
    dash = json.loads(ctx.eval("JSON.stringify(TR_relTime(null))"))
    assert label != dash, "a channel trigger is not the no-schedule dash"
    assert "p1" in label, label


def test_the_detail_metadata_has_a_channel_row():
    detail = SRC[SRC.index("function TR_TriggerDetail("):]
    assert 'data-testid="tr-detail-channel"' in detail, "the detail <dl> gains a channel row"


# ---- G11-05: the wizard and the detail expose the webhook interactive config -----


def test_the_wizard_webhook_step_carries_interactive_and_wait():
    ctx = _ctx()
    ctx.eval("MR.mount(window.TR_CreateTriggerDialog, { onClose: function () {}, onCreated: function () {} });")
    ctx.eval('change("tr-kind-select", "webhook");')
    ctx.eval('clickBtn("Next");')
    assert ctx.eval("MR.find('tr-step-webhook') != null"), "webhook step 2"
    ctx.eval('change("tr-wh-interactive", true);')
    ctx.eval('change("tr-wh-wait", "120");')
    ctx.eval('clickBtn("Next");')
    ctx.eval('change("tr-name", "wh trigger");')
    ctx.eval('clickBtn("Create");')
    ctx.eval("void 0;")
    ctx.eval("void 0;")
    calls = json.loads(ctx.eval("JSON.stringify(CALLS)"))
    post = [c for c in calls if c["m"] == "POST" and c["p"] == "/triggers"]
    assert post, f"the wizard POSTs the trigger: {calls}"
    config = post[0]["b"]["config"]
    assert config["kind"] == "webhook"
    assert config["interactive"] is True
    assert config["wait_timeout_seconds"] == 120


def test_the_detail_renders_the_webhook_interactive_rows():
    detail = SRC[SRC.index("function TR_TriggerDetail("):]
    assert 'data-testid="tr-detail-wh-interactive"' in detail
    assert 'data-testid="tr-detail-wh-wait"' in detail
