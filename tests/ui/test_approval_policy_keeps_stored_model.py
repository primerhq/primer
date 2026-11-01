"""UI-M9: editing an LLM-judge approval policy keeps its stored judge model.

The model select's reset effect used to run on mount, before the provider list had loaded:
``selectedProvider`` was undefined, the effect wiped ``model``, and when the profiles then
arrived it landed on ``modelOptions[0]``. An operator who changed only a policy's name found
the judge repointed from the stored model to the provider's first. The fix skips both reset
branches until the provider and profile lists have both loaded, so a still-offered stored
model survives the slow fetches.

Ported from the review probe (/tmp/review-ui/forkA/probe_ap_model.py): the lists arrive late by
serving them from a mutable object the useResource stub reads on each render.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

APPROVALS = Path(__file__).resolve().parents[2] / "ui" / "components" / "approvals.jsx"

PRELUDE_JS = r"""
var document = { addEventListener: function () {} };
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], disabled: p.disabled, onClick: p.onClick }, p.children); }
function Banner(p) { return React.createElement("div", null, p.title); }
function Modal(p) { return React.createElement("div", { "data-testid": "modal" }, p.children, p.footer); }
function Icon() { return null; }
function PR_PagedNote() { return null; }
function PR_fetchAllPages() { return new Promise(function () {}); }
var DATA = {};
var SENT = null;
window.ToolPicker = function () { return null; };
window.primerApi = {
  useResource: function (key) { return { data: DATA[key], loading: !DATA[key], error: null, refetch: function () {} }; },
  useMutation: function (fn) { return { mutate: function (b) { SENT = b; return Promise.resolve(); }, loading: false }; },
  apiFetch: function () { return Promise.resolve({}); },
};
"""

EXISTING = """
{ id: "p1", toolset_id: "workspaces", tool_name: "write_file", enabled: true,
  approval: { type: "llm", provider_id: "prov", model: "judge-large", prompt: "be careful" } }
"""

# Two providers; the second offers only its own model, so a switch away from "prov" has a
# model to reset to if (and only if) the reset-on-provider-change still works.
LISTS = """
DATA['approvals-modal:llm'] = { items: [{ id: 'prov', provider: 'openai' }, { id: 'prov2', provider: 'openai' }] };
DATA['approvals-modal:model-profiles'] = { items: [
  { id: 'a', provider_id: 'prov', model_name: 'judge-small' },
  { id: 'b', provider_id: 'prov', model_name: 'judge-large' },
  { id: 'c', provider_id: 'prov2', model_name: 'prov2-m' }
] };
"""

MODEL_VALUE = """
JSON.stringify((function () {
  var sel = MR.findProp("data-testid", "approval-policy-model");
  return sel ? sel.props.value : null;
})())
"""


def _ctx():
    ctx = mini_react_context(transpile(APPROVALS), PRELUDE_JS)
    OPEN.append(ctx)
    return ctx


OPEN: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while OPEN:
        OPEN.pop().close()


def _model_value(ctx) -> str | None:
    return json.loads(ctx.eval(MODEL_VALUE))


def test_editing_a_policy_keeps_the_stored_model_when_the_lists_arrive_late() -> None:
    ctx = _ctx()
    ctx.eval(
        "MR.mount(window.AP_NewPolicyModal, { existing: " + EXISTING +
        ", onClose: function () {}, pushToast: function () {} });"
    )
    # The modal mounted while both lists were still loading. They arrive now, as the two
    # fetches answer: the stored model is still offered by the provider, so it must stay.
    ctx.eval(LISTS + "\nMR.rerender();")
    assert _model_value(ctx) == "judge-large", (
        "the judge must keep its stored model: the stored model is still offered"
    )


def test_switching_the_provider_still_resets_the_model_to_the_first_offered() -> None:
    """Characterization of the reset the effect exists for (green before the fix, must stay green):
    it must survive the late-lists guard."""
    ctx = _ctx()
    ctx.eval(
        "MR.mount(window.AP_NewPolicyModal, { existing: " + EXISTING +
        ", onClose: function () {}, pushToast: function () {} });"
    )
    ctx.eval(LISTS + "\nMR.rerender();")
    ctx.eval(
        'MR.findProp("data-testid", "approval-policy-provider").props.onChange({ target: { value: "prov2" } });'
        "MR.rerender();"
    )
    assert _model_value(ctx) == "prov2-m", (
        "switching to a provider that does not offer the stored model resets to its first offered model"
    )
