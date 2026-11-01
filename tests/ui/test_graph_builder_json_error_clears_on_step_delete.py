"""Deleting a step must clear the JSON parse error that keeps Save disabled (UI-M12).

``GR_JsonField`` reports a parse error to its parent keyed by ``errorKey`` (the
builder's ``hasJsonError`` disables Save while any key holds an error) and
clears the key when the text commits or the value changes from outside - but
not when the field unmounts. So a step with an unparseable schema that is then
deleted kept its error key behind: the box was gone and Save stayed disabled
for the rest of the draft. The field now clears its key on unmount, the same
contract ``GB_branchValueEdit`` already keeps (``gb-branches.jsx``).

Ported from the 2026-10-31 UI review probe with the last check inverted: the
parse error of a deleted step must not outlive the step.
"""

from __future__ import annotations

import json

from tests.ui._graph_builder_v8 import DRIVER, PRELUDE, builder_code
from tests.ui._mini_react import mini_react_context

LOADED = {
    "id": "g", "description": "d",
    "nodes": [
        {"id": "start", "kind": "begin"},
        {"id": "a1", "kind": "agent", "agent_id": "x", "description": "A",
         "response_format": {"oneOf": [{"type": "object"}]}},
        {"id": "a2", "kind": "agent", "agent_id": "x", "description": "B"},
        {"id": "fin", "kind": "end"},
    ],
    "edges": [
        {"kind": "static", "from_node": "start", "to_node": "a1"},
        {"kind": "static", "from_node": "a1", "to_node": "a2"},
        {"kind": "static", "from_node": "a2", "to_node": "fin"},
    ],
}

PRELUDE_EXT = r"""
var confirmDialog = function () { return Promise.resolve(true); }; window.confirmDialog = confirmDialog;
"""

DRIVER_LOCAL = r"""
var LOADED = __loaded__;
function __mount() { mountBuilder(LOADED); selectNode("a1"); }
// Type invalid JSON into the step's schema box and leave it (blur commits the error).
function __typeBadSchema() {
  var ta = MR.findProp("aria-label", "Schema as JSON");
  ta.props.onChange({ target: { value: "{ not json" } });
  MR.rerender();
  MR.findProp("aria-label", "Schema as JSON").props.onBlur();
  MR.rerender();
}
// Delete the step through its own button: open the Advanced section, then
// press the button. The press awaits confirmDialog, so the press and the
// rerender that flushes the delete are separate evals.
function __openAdvanced() {
  globalThis.document = { activeElement: null, querySelector: function () { return null; }, querySelectorAll: function () { return []; }, getElementById: function () { return null; } };
  var adv = MR.findProp("aria-expanded", false);
  adv.props.onClick();
  MR.rerender();
}
function __pressDelete() { MR.find("gb-delete-step").props.onClick(); }
function __saveDisabled() { return MR.find("gb-save").props.disabled; }
function __a1StillThere() { return MR.findAll("gb-outline-row").some(function (el) { return el.props["data-node-id"] === "a1"; }); }
function __schemaFieldMounted() { return MR.findProp("aria-label", "Schema as JSON") !== null; }
""".replace("__loaded__", json.dumps(LOADED))


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _ctx():
    ctx = mini_react_context(builder_code(), PRELUDE + PRELUDE_EXT)
    ctx.eval(DRIVER + "\n" + DRIVER_LOCAL)
    return ctx


def test_a_parse_error_disables_save_while_the_step_is_there() -> None:
    # Characterization: the error shows and Save is off while the step is
    # there (the draft itself is unchanged, so Save would be off anyway).
    ctx = _ctx()
    try:
        ctx.eval("__mount(); __typeBadSchema()")
        assert _js(ctx, "__saveDisabled()") is True
        assert _js(ctx, "__schemaFieldMounted()") is True
    finally:
        ctx.close()


def test_deleting_the_step_clears_the_error_and_re_enables_save() -> None:
    # The inverted probe: the box is gone with the step, so its error key must
    # go with it - Save comes back on the draft that still needs saving.
    ctx = _ctx()
    try:
        ctx.eval("__mount(); __typeBadSchema(); __openAdvanced()")
        ctx.eval("__pressDelete()")
        ctx.eval("MR.rerender()")
        assert _js(ctx, "__a1StillThere()") is False
        assert _js(ctx, "__schemaFieldMounted()") is False
        assert _js(ctx, "__saveDisabled()") is False, "the deleted step's parse error must not keep Save disabled"
    finally:
        ctx.close()
