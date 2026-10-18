"""The agent overlay on the REAL platform kit (platform revamp plan 05, Task 4).

``test_agent_overlay_layout.py`` runs the form on stand-ins for the kit parts, which is quick and says what the form PASSES them. The plan assumes four things the
kit does with those props, and a stand-in cannot prove them, so these tests load the real ``ui/components/shared/platform-kit.jsx`` and the real ``FormField``:

* a ``PK_Field`` whose first child is an ``input``, ``select`` or ``textarea`` labels it (``<label for>``), and a field whose child is a component (the prompt
  tabs, the tool picker) becomes a named ``role="group"``;
* an error is FormField's ``role="alert"`` line and marks the control ``aria-invalid``;
* ``PK_Fold.open`` is the STARTING state, and a folded section is not mounted (so a refusal inside it must reopen it);
* ``PK_FormOverlay`` carries its test id and its footer's two sides.

The ``Modal`` it is built on is the harness's stand-in (it keeps the test id and the title); the journeys in ``tests/ui_e2e`` run the real one.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._agent_form_harness import FULL, agent_form, calls, click, mount, prop, reject_next, testids_under, texts, type_into

# Every element at or under the one with that test id, as {type, id, htmlFor, role, text, labelledby}: the walk the harness cannot do for elements without a test id.
_WALK = r"""
(function (start) {
  var out = [];
  function join(n) {
    if (n == null || typeof n === "boolean") return "";
    if (typeof n === "string" || typeof n === "number") return String(n);
    if (Array.isArray(n)) return n.map(join).join("");
    return n.__el ? join(typeof n.type === "function" && n.type !== React.Fragment ? n.out : n.children) : "";
  }
  (function walk(n) {
    if (n == null || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(walk); return; }
    if (!n.__el) return;
    if (typeof n.type === "string") {
      out.push({ type: n.type, id: n.props.id, htmlFor: n.props.htmlFor, role: n.props.role, labelledby: n.props["aria-labelledby"],
                 invalid: n.props["aria-invalid"], text: join(n.children) });
    }
    walk(typeof n.type === "function" && n.type !== React.Fragment ? n.out : n.children);
  })(MR.find(start));
  return JSON.stringify(out);
})("agent-form")
"""


def elements(ctx) -> list[dict]:
    return json.loads(ctx.eval(_WALK))


@pytest.fixture
def ctx():
    with agent_form(real_kit=True) as c:
        yield c


def fill_required(ctx) -> None:
    type_into(ctx, "agent-id", "refund-triage")
    type_into(ctx, "agent-description", "Triages refunds")


def test_the_overlay_is_the_kits_with_its_test_id_title_and_both_footer_sides(ctx):
    mount(ctx)
    assert prop(ctx, "agent-form", "data-title") == "New agent"
    ids = testids_under(ctx, "agent-form")
    assert ids.index("agent-form-cancel") < ids.index("agent-form-submit")


def test_each_plain_control_is_labelled_by_its_row_and_the_widgets_are_named_groups(ctx):
    mount(ctx)
    found = elements(ctx)
    labels = {e["text"]: e.get("htmlFor") for e in found if e["type"] == "label"}
    ids = {e.get("id") for e in found if e["type"] in ("input", "select", "textarea")}
    expected = {
        "Id": "na-id", "Model profile *": "na-model-profile", "Description *": "na-description", "Compaction prompt": "na-compaction-prompt",
        "Temperature": "na-temperature", "Max tool turns": "na-max-tool-turns", "Max output tokens": "na-max-output-tokens",
    }
    assert {k: labels.get(k) for k in expected} == expected
    assert set(expected.values()) <= ids, "and each id is on a control that is drawn"
    groups = {e.get("labelledby"): e for e in found if e.get("role") == "group"}
    named = {e.get("id"): e["text"] for e in found if e["type"] == "label"}
    assert {"System prompt", "Tools"} <= {named.get(label_id) for label_id in groups}, "the prompt tabs and the tool picker are named groups"


def test_the_folds_start_closed_and_do_not_mount_their_controls_until_opened(ctx):
    mount(ctx)
    folded = ("agent-tts-voice", "agent-response-format", "na-compaction-tool-access", "na-allow-external-tools")
    assert not set(folded) & set(testids_under(ctx, "agent-form"))
    assert prop(ctx, "agent-voice-fold:toggle", "aria-expanded") == "false"
    assert prop(ctx, "agent-advanced-fold:toggle", "aria-expanded") == "false"
    click(ctx, "agent-voice-fold:toggle")
    click(ctx, "agent-advanced-fold:toggle")
    assert set(folded) <= set(testids_under(ctx, "agent-form"))
    assert prop(ctx, "agent-advanced-fold:toggle", "aria-expanded") == "true"


def test_a_value_typed_in_a_fold_survives_closing_it(ctx):
    """The values live in the form, not in the fold, so a closed fold still saves what it held."""
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-advanced-fold:toggle")
    type_into(ctx, "agent-response-format", '{"type": "object"}')
    click(ctx, "agent-advanced-fold:toggle")
    assert "agent-response-format" not in testids_under(ctx, "agent-form")
    click(ctx, "agent-form-submit")
    assert calls(ctx)[0]["body"]["response_format"] == {"type": "object"}


def test_a_bad_response_format_inside_a_closed_fold_reopens_it_and_says_why(ctx):
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-advanced-fold:toggle")
    type_into(ctx, "agent-response-format", "{")
    click(ctx, "agent-advanced-fold:toggle")  # the operator folds it away, then presses Create
    click(ctx, "agent-form-submit")
    assert calls(ctx) == []
    assert prop(ctx, "agent-advanced-fold:toggle", "aria-expanded") == "true"
    assert "agent-response-format" in testids_under(ctx, "agent-form")
    assert any(t.startswith("JSON parse:") for t in texts(ctx))
    alert = [e for e in elements(ctx) if e.get("role") == "alert"]
    assert alert and alert[0]["text"].startswith("JSON parse:")


def test_a_422_on_a_field_in_a_closed_fold_opens_it_and_shows_the_message(ctx):
    reject_next(ctx, {"status": 422, "fieldErrors": [{"loc": ["body", "response_format"], "msg": "not a JSON Schema"}]})
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-form-submit")
    assert prop(ctx, "agent-advanced-fold:toggle", "aria-expanded") == "true"
    assert "not a JSON Schema" in texts(ctx)


def test_an_error_marks_its_control_invalid_and_is_an_alert_line_under_it(ctx):
    reject_next(ctx, {"status": 422, "fieldErrors": [{"loc": ["body", "temperature"], "msg": "Input should be greater than or equal to 0"}]})
    mount(ctx, existing=FULL)
    click(ctx, "agent-form-submit")
    assert prop(ctx, "agent-temperature", "aria-invalid") == "true"
    alert = [e for e in elements(ctx) if e.get("role") == "alert"]
    assert [a["text"] for a in alert] == ["Input should be greater than or equal to 0"]
    described = prop(ctx, "agent-temperature", "aria-describedby")
    assert described and described.endswith(next(e["id"] for e in elements(ctx) if e.get("role") == "alert"))


def test_the_full_screen_prompt_editor_sits_in_the_kits_field(ctx):
    mount(ctx)
    click(ctx, "agent-system-prompt-fullscreen")
    assert prop(ctx, "agent-system-prompt", "data-fullscreen") == "true"
    assert prop(ctx, "agent-system-prompt", "style")["position"] == "fixed"


def test_the_required_asterisk_is_drawn_for_assistive_technology_as_decoration(ctx):
    mount(ctx)
    stars = [e for e in elements(ctx) if e["type"] == "span" and e["text"] == " *"]
    assert len(stars) == 2, "Model profile and Description"
