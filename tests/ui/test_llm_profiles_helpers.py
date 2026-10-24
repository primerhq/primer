"""The plain-JS helpers of the LLM profile overlays (Platform revamp Plan 03, Task 2).

``ui/components/model-profiles.jsx`` keeps every decision of the profile forms that is not drawing in one block of plain JavaScript between the
two marker lines below: no JSX, no hook, ``function`` declarations and ``var`` constants, so each is a global in the console bundle and in a bare V8
context. The block is evaluated here exactly as written, on top of ``ui/foundation/api.js`` (``readRefusal`` and ``ApiError``), one fresh MiniRacer
context per test.

What the block pins, in the order of the plan's Review Focus:

* the body of an EDIT is the stored row with the form's fields laid over it (``MP_editBody``), because PUT is a full replace and a form that rebuilds
  the body from what it draws drops ``config``, ``harness_id`` and every field it does not show;
* the order of the members is the failover chain: ticking, unticking and moving never reorder the others;
* the defaults of the routing choices are the model's, read from ``ModelProfile.model_fields``, and the option values are the model's enum values;
* a refused save is told under the field it is about (``MP_refusalErrors``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._llm_profile_fixtures import FULL_AGG, PROVIDERS, ROWS, router_envelopes

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "ui" / "components" / "model-profiles.jsx"
API = ROOT / "ui" / "foundation" / "api.js"

START = "// ---- MP helpers: plain JS, no JSX (tests/ui/test_llm_profiles_helpers.py evaluates this block) ----"
END = "// ---- end MP helpers ----"

R = json.dumps(ROWS)
RULE = "Use letters, digits, dots, dashes and underscores, and start with a letter or a digit."
WHOLE = "Enter a whole number of tokens, 1 or more."


def helper_block() -> str:
    src = SRC.read_text(encoding="utf-8")
    assert src.count(START) == 1 and src.count(END) == 1, "the helper block is bounded by its two marker lines, once each"
    start = src.index(START) + len(START)
    return src[start : src.index(END, start)]


@pytest.fixture
def ctx():
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    try:
        c.eval("var window = globalThis;")
        c.eval(API.read_text(encoding="utf-8"))
        c.eval(helper_block())
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


# ---- the block itself --------------------------------------------------------------------------------------------------------------------------------


def test_the_helper_block_holds_no_hook_and_no_react() -> None:
    block = helper_block()
    assert "React." not in block and "useResource" not in block and "useState" not in block, "a helper is a plain function of its arguments"


def test_every_helper_is_a_global_of_the_bundle(ctx) -> None:
    for name in ("MP_fmtTokens", "MP_isSingle", "MP_isManaged", "MP_memberRows", "MP_toggleMember", "MP_toggleAllMembers", "MP_moveMember",
                 "MP_aggregateWindow", "MP_windowText", "MP_advancedIsSet", "MP_nameError", "MP_aggregateBody", "MP_editBody", "MP_refusalErrors",
                 "MP_contextError", "MP_providerOptions", "MP_routingLabel", "MP_usageText", "MP_profileCard", "MP_withWindows",
                 "MP_extendedText", "MP_parseExtended"):
        assert ctx.eval(f"typeof {name}") == "function", name
    for name in ("MP_AGGREGATE_DEFAULTS", "MP_ROUTING_OPTIONS", "MP_ROUTING_HELP", "MP_FAILOVER_POINT_OPTIONS", "MP_FAILOVER_POINT_HELP",
                 "MP_FAILOVER_ON_OPTIONS", "MP_FAILOVER_ON_HELP", "MP_ICON_SINGLE", "MP_ICON_AGGREGATED", "MP_REASONING_OPTIONS"):
        assert ctx.eval(f"typeof {name}") != "undefined", name


# ---- the routing choices ----------------------------------------------------------------------------------------------------------------------------


def test_defaults_are_the_backend_defaults(ctx) -> None:
    from primer.model.model_profile import ModelProfile

    fields = ModelProfile.model_fields
    assert js(ctx, "MP_AGGREGATE_DEFAULTS") == {k: fields[k].default.value for k in ("strategy", "failover_point", "failover_on")}
    assert js(ctx, "MP_AGGREGATE_DEFAULTS.failover_on") == "transient_and_config"


def test_option_values_are_the_backend_enum_values_and_labels_are_the_designs(ctx) -> None:
    from primer.model.model_profile import FailoverClasses, FailoverPoint, RoutingStrategy

    for name, enum in (("MP_ROUTING_OPTIONS", RoutingStrategy), ("MP_FAILOVER_POINT_OPTIONS", FailoverPoint), ("MP_FAILOVER_ON_OPTIONS", FailoverClasses)):
        assert {o["value"] for o in js(ctx, name)} == {e.value for e in enum}
    assert [o["label"] for o in js(ctx, "MP_ROUTING_OPTIONS")] == ["Sequential fallback", "Round robin"]
    assert [o["label"] for o in js(ctx, "MP_FAILOVER_POINT_OPTIONS")] == ["Before the first token", "Also mid-stream"]
    assert [o["label"] for o in js(ctx, "MP_FAILOVER_ON_OPTIONS")] == ["Transient errors", "Also auth and config errors"]
    assert js(ctx, "MP_ROUTING_HELP") == {
        "sequential": "Always try profile 1 first; if it answers 429, a 5xx or times out, move to the next.",
        "round_robin": "Rotate which profile goes first on each call.",
    }
    assert js(ctx, "MP_FAILOVER_POINT_HELP") == "Failing over mid-stream can repeat text the user has already seen."
    assert js(ctx, "MP_FAILOVER_ON_HELP") == "Transient errors are 429, 5xx, timeouts and network failures."


# ---- the Advanced fold -------------------------------------------------------------------------------------------------------------------------------


def test_advanced_is_set_by_a_failover_choice_that_is_not_the_default(ctx) -> None:
    """The overlay starts the fold open when this is true (the lead's ruling for #774): on an edit whose ``failover_point`` or ``failover_on`` differs from the model's default.
    The defaults and the other values are read from the model, so a value added to either enum is tried too."""
    from primer.model.model_profile import FailoverClasses, FailoverPoint, ModelProfile

    default = {k: ModelProfile.model_fields[k].default.value for k in ("failover_point", "failover_on")}
    assert js(ctx, f"MP_advancedIsSet({json.dumps(default)})") is False
    assert js(ctx, "MP_advancedIsSet(MP_AGGREGATE_DEFAULTS)") is False
    for field, enum in (("failover_point", FailoverPoint), ("failover_on", FailoverClasses)):
        others = [e.value for e in enum if e.value != default[field]]
        assert others, f"{field} has a value other than its default to try"
        for value in others:
            assert js(ctx, f"MP_advancedIsSet({json.dumps({**default, field: value})})") is True, (field, value)


def test_advanced_is_not_set_by_a_row_that_is_missing_or_has_no_failover_fields(ctx) -> None:
    """A create has no row; a row that carries neither field reads as the defaults, as the overlay's own state does."""
    for row in ("null", "undefined", "{}", '{ "id": "a" }', '{ "failover_point": null, "failover_on": null }', '{ "failover_point": "", "failover_on": "" }'):
        assert js(ctx, f"MP_advancedIsSet({row})") is False, row


def test_advanced_ignores_what_sits_outside_the_fold(ctx) -> None:
    """The routing switch (``strategy``) is outside the fold, and so is every other field of the row."""
    assert js(ctx, 'MP_advancedIsSet({ strategy: "round_robin", config: { reasoning: "high" }, members: ["a", "b"] })') is False


# ---- numbers ----------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "text"),
    [(262144, "262,144"), (8192, "8,192"), (999, "999"), (1000000, "1,000,000"), (0, ""), (-5, ""), (12.5, ""), (None, ""), ("abc", ""), ("8192", "")],
)
def test_fmt_tokens(ctx, n, text) -> None:
    assert js(ctx, f"MP_fmtTokens({json.dumps(n)})") == text


def test_fmt_tokens_never_asks_the_locale(ctx) -> None:
    """The grouping is the same on every machine the console runs on, and in this test."""
    assert "toLocaleString" not in helper_block()


# ---- the kinds ---------------------------------------------------------------------------------------------------------------------------------------


def test_single_and_managed(ctx) -> None:
    assert js(ctx, '[MP_isSingle({ kind: "single" }), MP_isSingle({}), MP_isSingle({ kind: "aggregated" })]') == [True, True, False]
    assert js(ctx, '[MP_isManaged({ harness_id: "h1" }), MP_isManaged({ harness_id: null }), MP_isManaged({}), MP_isManaged(null)]') == [True, False, False, False]


# ---- the members table --------------------------------------------------------------------------------------------------------------------------------


def test_member_rows_list_single_profiles_checked_first_in_member_order(ctx) -> None:
    assert js(ctx, f'MP_memberRows({R}, ["p-b", "p-a"], null)') == [
        {"id": "p-b", "checked": True, "order": 1, "window": 8192},
        {"id": "p-a", "checked": True, "order": 2, "window": 32768},
        {"id": "p-c", "checked": False, "order": None, "window": 131072},
    ]


def test_the_unticked_rows_follow_by_id_whatever_order_the_list_came_in(ctx) -> None:
    rows = json.dumps([{"id": "z", "kind": "single", "context_length": 1}, {"id": "m", "kind": "single", "context_length": 2}, {"id": "a", "kind": "single", "context_length": 3}])
    assert [r["id"] for r in js(ctx, f'MP_memberRows({rows}, ["m"], null)')] == ["m", "a", "z"]


def test_a_member_missing_from_the_list_stays_ticked_with_no_window(ctx) -> None:
    assert js(ctx, f'MP_memberRows({R}, ["p-gone", "p-a"], null)')[0] == {"id": "p-gone", "checked": True, "order": 1, "window": None}


def test_a_member_listed_twice_is_one_row(ctx) -> None:
    ids = [r["id"] for r in js(ctx, f'MP_memberRows({R}, ["p-a", "p-a", "p-b"], null)')]
    assert ids == ["p-a", "p-b", "p-c"], "a repeated key would be two checkboxes with one name"


def test_the_edited_aggregate_is_not_its_own_candidate(ctx) -> None:
    rows = json.dumps([*ROWS, {"id": "self", "kind": "single", "context_length": 10}])
    assert "self" not in [r["id"] for r in js(ctx, f'MP_memberRows({rows}, [], "self")')]


def test_an_aggregated_row_is_never_a_candidate(ctx) -> None:
    assert "agg-x" not in [r["id"] for r in js(ctx, f"MP_memberRows({R}, [], null)")]


def test_toggle_appends_or_removes_and_keeps_the_others_in_order(ctx) -> None:
    assert js(ctx, 'MP_toggleMember(["a", "b", "c"], "b")') == ["a", "c"]
    assert js(ctx, 'MP_toggleMember(["a", "c"], "b")') == ["a", "c", "b"]
    assert js(ctx, f'MP_toggleAllMembers(["p-b"], {R}, null, true)') == ["p-b", "p-a", "p-c"]
    assert js(ctx, f'MP_toggleAllMembers(["p-b"], {R}, null, false)') == []


def test_toggle_all_leaves_the_edited_aggregate_out_and_none_of_the_inputs_changed(ctx) -> None:
    rows = json.dumps([*ROWS, {"id": "self", "kind": "single", "context_length": 10}])
    assert js(ctx, f'MP_toggleAllMembers([], {rows}, "self", true)') == ["p-a", "p-b", "p-c"]
    assert js(ctx, '(function () { var m = ["a"]; MP_toggleMember(m, "b"); MP_toggleMember(m, "a"); return m; })()') == ["a"]
    assert js(ctx, f'(function () {{ var m = ["p-b"]; MP_toggleAllMembers(m, {R}, null, true); return m; }})()') == ["p-b"]


def test_move_swaps_with_a_neighbour_and_stops_at_the_ends(ctx) -> None:
    assert js(ctx, 'MP_moveMember(["a", "b", "c"], "c", -1)') == ["a", "c", "b"]
    assert js(ctx, 'MP_moveMember(["a", "b", "c"], "a", -1)') == ["a", "b", "c"]
    assert js(ctx, 'MP_moveMember(["a", "b", "c"], "c", 1)') == ["a", "b", "c"]
    assert js(ctx, 'MP_moveMember(["a", "b", "c"], "zz", 1)') == ["a", "b", "c"]
    assert js(ctx, '(function () { var m = ["a", "b"]; MP_moveMember(m, "b", -1); return m; })()') == ["a", "b"]


# ---- the window --------------------------------------------------------------------------------------------------------------------------------------


def test_the_window_is_the_smallest_member_window_or_unknown(ctx) -> None:
    assert js(ctx, f'MP_aggregateWindow(["p-a", "p-b"], {R})') == 8192
    for members in ('[]', '["p-a", "p-gone"]', '["p-a", "agg-x"]'):
        assert js(ctx, f"MP_aggregateWindow({members}, {R})") is None
    assert js(ctx, f'MP_windowText(["p-a", "p-b"], {R})') == "8,192 (the smallest of the picked profiles)"
    assert js(ctx, f"MP_windowText([], {R})") == "Pick the profiles first."
    assert js(ctx, f'MP_windowText(["p-gone"], {R})') == "Unknown: a picked profile is not loaded."


# ---- the name ---------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "error"),
    [("", "Enter a name."), ("   ", "Enter a name."), ("fast-with-fallback", None), ("Fast_1.v2", None), (" padded ", None), ("a/b", RULE), ("-a", RULE), ("a b", RULE), (".a", RULE)],
)
def test_the_name_rule(ctx, name, error) -> None:
    assert js(ctx, f"MP_nameError({json.dumps(name)})") == error


def test_the_name_rule_is_the_pattern_the_register_overlay_builds_profile_ids_from(ctx) -> None:
    """Conflict C9: ``PC_profileIdFor`` (seam I-5, ``providers/register-common.jsx``) builds the ids of imported profiles from ``PC_ID_RE``. The aggregate's name is held to the very same
    pattern, so an id the console builds is always an id the console accepts, and the one is not edited without the other."""
    common = (ROOT / "ui" / "components" / "providers" / "register-common.jsx").read_text(encoding="utf-8")
    found = re.search(r"const PC_ID_RE = /(.+)/;", common)
    assert found, "register-common.jsx no longer declares PC_ID_RE as a literal"
    assert js(ctx, "MP_NAME_RULE.source") == found.group(1)


def test_the_name_rule_accepts_and_refuses_the_ids_it_should(ctx) -> None:
    for ok in ("a", "A1", "1a", "a.b_c-d", "vllm-gx10--qwen3.8-27b"):
        assert js(ctx, f"MP_nameError({json.dumps(ok)})") is None, ok
    for bad in ("a:b", "a/b", "a\\b", "é", "a+b", "_a"):
        assert js(ctx, f"MP_nameError({json.dumps(bad)})") == RULE, bad


# ---- the bodies --------------------------------------------------------------------------------------------------------------------------------------


def test_the_create_body_has_exactly_the_aggregated_keys(ctx) -> None:
    body = 'MP_aggregateBody({ name: " fast ", members: ["p-b", "p-a"], strategy: "round_robin", failover_point: "mid_stream", failover_on: "transient" })'
    assert js(ctx, body) == {
        "id": "fast", "description": "Aggregate of p-b, p-a", "kind": "aggregated", "members": ["p-b", "p-a"],
        "strategy": "round_robin", "failover_point": "mid_stream", "failover_on": "transient",
    }


def test_the_create_body_copies_the_members(ctx) -> None:
    assert js(ctx, '(function () { var m = ["a", "b"]; var b = MP_aggregateBody({ name: "x", members: m, strategy: "sequential", failover_point: "mid_stream", failover_on: "transient" }); m.push("c"); return b.members; })()') == ["a", "b"]


def test_an_edit_body_keeps_every_field_the_form_does_not_show(ctx) -> None:
    expected = {k: v for k, v in FULL_AGG.items() if k not in ("agent_count", "graph_node_count", "_window")}
    assert js(ctx, f'MP_editBody({json.dumps(FULL_AGG)}, {{ strategy: "round_robin" }})') == {**expected, "strategy": "round_robin"}
    assert js(ctx, f'(function () {{ var e = {json.dumps(FULL_AGG)}; MP_editBody(e, {{ strategy: "x" }}); return e.strategy; }})()') == "sequential"


def test_an_edit_body_drops_only_what_the_server_adds_or_the_page_annotates(ctx) -> None:
    row = {"id": "a", "agent_count": 1, "graph_node_count": 2, "_window": 3, "_other": 4, "config": {"extended": {"k": 1}}, "harness_id": None, "members": None}
    assert js(ctx, f"MP_editBody({json.dumps(row)}, {{}})") == {"id": "a", "config": {"extended": {"k": 1}}, "harness_id": None, "members": None}


# ---- a refusal under the field it is about ---------------------------------------------------------------------------------------------------------------


@pytest.fixture
def envelopes(fake_storage_provider) -> dict:
    return router_envelopes(fake_storage_provider)


def _refusal(ctx, envelope: dict, field_map: dict) -> dict:
    return js(ctx, f"MP_refusalErrors(new window.primerApi.ApiError({json.dumps(envelope)}), {json.dumps(field_map)})")


def _message(ctx, envelope: dict) -> str:
    return js(ctx, f"window.primerApi.readRefusal(new window.primerApi.ApiError({json.dumps(envelope)})).message")


def test_a_refusal_lands_under_the_field_it_names(ctx, envelopes) -> None:
    create = {"id": "name", "members": "members"}
    assert _refusal(ctx, envelopes["member_not_found"], create) == {"fields": {"members": "member profile 'p-gone' does not exist"}, "form": None}
    conflict = _refusal(ctx, envelopes["conflict"], create)
    assert conflict == {"fields": {"name": _message(ctx, envelopes["conflict"])}, "form": None}
    assert "p-a" in conflict["fields"]["name"], "the server's own words, naming the id that is taken"
    provider = _refusal(ctx, envelopes["provider_not_found"], create)
    assert provider == {"fields": {}, "form": _message(ctx, envelopes["provider_not_found"])}, "a field the form does not draw is a form-level error"


def test_a_conflict_on_an_edit_is_not_a_name_error(ctx, envelopes) -> None:
    """The overlay passes the ``id`` key only in create mode: a 409 on an edit (a managed row, a path/body mismatch) is about the form, not the name."""
    edit = {"members": "members", "description": "description"}
    assert _refusal(ctx, envelopes["conflict"], edit) == {"fields": {}, "form": _message(ctx, envelopes["conflict"])}


def test_a_refusal_that_is_not_an_api_error_is_a_form_error(ctx) -> None:
    got = js(ctx, 'MP_refusalErrors(new Error("network down"), { id: "name", members: "members" })')
    assert got == {"fields": {}, "form": "network down"}
    assert js(ctx, 'MP_refusalErrors(null, { id: "name" })') == {"fields": {}, "form": "Request failed"}


def test_a_field_named_like_an_object_property_is_not_mistaken_for_a_mapped_one(ctx) -> None:
    envelope = {"type": "/errors/validation-error", "title": "Validation Error", "status": 422, "detail": "no", "extensions": {"error": "x_y", "field": "constructor", "message": "no"}}
    assert _refusal(ctx, envelope, {"members": "members"}) == {"fields": {}, "form": "no"}


# ---- the single profile's fields (Task 3) -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "error"),
    [("", "Enter the context window."), ("0", WHOLE), ("-1", WHOLE), ("12.5", WHOLE), ("abc", WHOLE), ("1e3", WHOLE), (" 8192", WHOLE), ("08192", WHOLE), ("1", None), ("128000", None)],
)
def test_the_context_window_rule(ctx, text, error) -> None:
    assert js(ctx, f"MP_contextError({json.dumps(text)})") == error


def test_a_context_window_that_is_not_text_is_the_blank_case(ctx) -> None:
    assert js(ctx, "MP_contextError(null)") == "Enter the context window."
    assert js(ctx, "MP_contextError(undefined)") == "Enter the context window."
    assert js(ctx, "MP_contextError(8192)") is None, "a number is its own text"


def test_provider_options_keep_a_provider_that_is_not_listed(ctx) -> None:
    assert js(ctx, f'MP_providerOptions({json.dumps(PROVIDERS)}, "gone")') == [
        {"value": "lp", "label": "lp (openchat)"},
        {"value": "other", "label": "other (anthropic)"},
        {"value": "gone", "label": "gone (not found)"},
    ]
    assert len(js(ctx, f'MP_providerOptions({json.dumps(PROVIDERS)}, "lp")')) == 2


def test_provider_options_with_no_current_provider_or_no_providers(ctx) -> None:
    assert len(js(ctx, f'MP_providerOptions({json.dumps(PROVIDERS)}, "")')) == 2, "no current provider: nothing to keep"
    assert len(js(ctx, f"MP_providerOptions({json.dumps(PROVIDERS)}, null)")) == 2
    assert js(ctx, 'MP_providerOptions([], "gone")') == [{"value": "gone", "label": "gone (not found)"}]
    assert js(ctx, 'MP_providerOptions(null, "")') == []
    assert js(ctx, 'MP_providerOptions([{ id: "bare" }], "bare")') == [{"value": "bare", "label": "bare"}], "a provider with no type is just its id"


# ---- the LLM Profiles page's cards (Task 4) ---------------------------------------------------------------------------------------------------------------


def test_the_routing_label_of_a_card(ctx) -> None:
    """The card's words for the routing: the model's default (sequential) for a row that names none or an unknown value."""
    assert js(ctx, '[MP_routingLabel("round_robin"), MP_routingLabel("sequential"), MP_routingLabel(undefined), MP_routingLabel("other")]') == [
        "round robin", "sequential fallback", "sequential fallback", "sequential fallback"]


@pytest.mark.parametrize(
    ("agents", "nodes", "text"),
    [(0, 0, "unbound"), (None, None, "unbound"), (1, 0, "bound by 1 agent"), (2, 0, "bound by 2 agents"), (2, 1, "bound by 2 agents · 1 graph node"),
     (1, 3, "bound by 1 agent · 3 graph nodes"), (0, 2, "bound by 0 agents · 2 graph nodes"), (-1, 0, "unbound"), ("3", 0, "unbound")],
)
def test_the_usage_text(ctx, agents, nodes, text) -> None:
    """The bound-by copy of the old card (P4): agents always, graph nodes only when there are some; a count that is not a whole number above 0 is none."""
    assert js(ctx, f"MP_usageText({json.dumps({'agent_count': agents, 'graph_node_count': nodes})})") == text


def test_the_card_builder_does_not_change_its_row(ctx) -> None:
    row = {"id": "agg", "kind": "aggregated", "members": ["p-a", "p-b"], "_window": 8192, "harness_id": "h"}
    assert js(ctx, f"(function () {{ var r = {json.dumps(row)}; MP_profileCard(r); return r; }})()") == row
    assert js(ctx, f"(function () {{ var r = {{ items: {R} }}; MP_withWindows(r); return r; }})()") == {"items": ROWS}


# ---- a single profile's model settings (Task 7) -----------------------------------------------------------------------------------------------------------

EXTENDED_RULE = 'Enter a JSON object, for example {"top_k": 20}.'


def test_reasoning_options_are_the_backend_levels(ctx) -> None:
    """The levels are the model's (``ReasoningLevel``); the empty value is "Vendor default", which the overlay sends as null (the model's own default)."""
    from primer.model.model_profile import ModelProfileConfig, ReasoningLevel

    options = js(ctx, "MP_REASONING_OPTIONS")
    assert {o["value"] for o in options} - {""} == {e.value for e in ReasoningLevel}
    assert options == [
        {"value": "", "label": "Vendor default"}, {"value": "off", "label": "Off"}, {"value": "minimal", "label": "Minimal"},
        {"value": "low", "label": "Low"}, {"value": "medium", "label": "Medium"}, {"value": "high", "label": "High"},
    ]
    assert ModelProfileConfig.model_fields["reasoning"].default is None, "Vendor default is the model's default"


@pytest.mark.parametrize(
    ("text", "result"),
    [("", {"value": {}}), ("  \n ", {"value": {}}), ("{}", {"value": {}}), ('{"top_k": 20}', {"value": {"top_k": 20}}),
     ('{"a": {"b": [1, 2]}, "c": null}', {"value": {"a": {"b": [1, 2]}, "c": None}}),
     ("[1]", {"error": EXTENDED_RULE}), ("null", {"error": EXTENDED_RULE}), ("3", {"error": EXTENDED_RULE}), ('"x"', {"error": EXTENDED_RULE}),
     ("{", {"error": EXTENDED_RULE}), ("{top_k: 20}", {"error": EXTENDED_RULE})],
)
def test_parse_extended(ctx, text, result) -> None:
    """Blank is the empty object (the model's default); anything else must be one JSON object, because ``extended`` is a dict on the model."""
    assert js(ctx, f"MP_parseExtended({json.dumps(text)})") == result


def test_extended_text_round_trips_through_the_parser(ctx) -> None:
    assert js(ctx, 'MP_extendedText({ top_k: 20 })') == '{\n  "top_k": 20\n}'
    assert js(ctx, "[MP_extendedText(null), MP_extendedText(undefined), MP_extendedText({})]") == ["{}", "{}", "{}"]
    nested = {"a": {"b": [1, 2.5, "x"]}, "c": None, "d": True}
    assert js(ctx, f"MP_parseExtended(MP_extendedText({json.dumps(nested)}))") == {"value": nested}
