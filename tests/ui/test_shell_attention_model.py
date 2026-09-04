"""Attention is computed from S1's envelopes plus the approvals endpoint.

Amendment m10: S6 is never consulted. Pinned decision 17: the tier is a
function of the yield's tool_name, because that is the only consequence
signal the approved backends carry.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "shell-attention.js"
FIXTURES = ROOT / "ui" / "fixtures" / "shell"


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    pending = (FIXTURES / "pending-yields.json").read_text(encoding="utf-8")
    records = (FIXTURES / "approval-records.json").read_text(encoding="utf-8")
    ctx.eval(f"var PENDING = {pending}; var RECORDS = {records};")
    return ctx


def test_module_is_pure_and_registered() -> None:
    src = MODULE.read_text(encoding="utf-8")
    for banned in ("document.", "apiFetch", "React"):
        assert banned not in src, banned
    assert 'src="foundation/shell-attention.js"' in (
        ROOT / "ui" / "index.html"
    ).read_text(encoding="utf-8")


def test_it_never_reaches_for_a_trigger_or_channel() -> None:
    """Amendment m10 in one assertion."""
    src = MODULE.read_text(encoding="utf-8").lower()
    for banned in ("trigger", "channel", "subscription"):
        assert banned not in src, banned


def test_gates_and_questions_are_interrupt_tier() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_toAttentionItems({pending: PENDING.items, '
        'records: RECORDS.items}).map(function (i) '
        '{ return [i.toolName, i.tier, i.resolved]; }))'
    ))
    by_name = {row[0]: row for row in out}
    # "approval", not "ask_approval" - GET /workspaces/{wid}/yields/
    # pending's real "kind" field (primer/api/routers/workspaces.py's
    # _extract_yield_kind maps the internal "_approval" tool name to
    # the human-facing "approval"); live-verified at gate step one.
    assert by_name["approval"][1] == "interrupt"
    assert by_name["ask_user"][1] == "interrupt"


def test_an_unknown_pending_yield_is_ambient_not_an_interrupt() -> None:
    """Interrupts are spent extremely sparingly."""
    ctx = _ctx()
    tier = ctx.eval(
        'SH_tierFor({toolName: "workspace__grep", resolved: false})'
    )
    assert tier == "ambient"


def test_resolved_records_are_digest_and_remain_queryable() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_toAttentionItems({pending: [], '
        'records: RECORDS.items}).map(function (i) '
        '{ return [i.tier, i.resolved]; }))'
    ))
    assert out and all(row == ["digest", True] for row in out)


def test_the_decision_preview_survives_into_the_item() -> None:
    """A decision card without the literal gated call is a free-text
    question, which section 8 forbids. GET /workspaces/{wid}/yields/
    pending's real payload has no free-standing diff/preview string
    (live-verified at gate step one) - SH_previewOf computes this from
    resume_metadata.original_call's name + arguments."""
    ctx = _ctx()
    preview = ctx.eval(
        'SH_toAttentionItems({pending: PENDING.items, records: []})[0].preview'
    )
    assert preview == "workspace__write_file(path=src/api.ts)"


def test_gated_tool_names_the_real_tool_not_the_yield_kind() -> None:
    """Live finding 01a064d3: item.toolName is the yield KIND ("approval"/
    "ask_user"), reserved for SH_tierFor's tier routing - the decision
    card's tool chip and the parked status strip need the actual gated
    tool id instead, which is what gatedTool (SH_gatedToolOf) exposes.
    A pending approval's gatedTool is resume_metadata.original_call.name;
    a pending question's is the literal "ask_user" (it has no gated
    call), same as toolName already read for that one case."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_toAttentionItems({pending: PENDING.items, '
        'records: []}).map(function (i) '
        '{ return [i.toolName, i.gatedTool]; }))'
    ))
    by_kind = {row[0]: row[1] for row in out}
    assert by_kind["approval"] == "workspace__write_file"
    assert by_kind["ask_user"] == "ask_user"


def test_snooze_and_mute_filter_without_deleting() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var items = SH_toAttentionItems({pending: PENDING.items, records: []});
          var triage = SH_emptyTriage();
          triage.mutedSessions["sess-77de01aa"] = true;
          var kept = SH_applyTriage(items, triage);
          return JSON.stringify([items.length, kept.length]);
        })()
        """
    ))
    assert out == [2, 1]


def test_approved_by_map_is_keyed_on_the_tool_call_id() -> None:
    """The "on behalf of" stamp needs to know which call a human let
    through."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        "JSON.stringify(SH_approvedByMap(RECORDS.items))"
    ))
    assert "tc-77" in out
    assert "tc-76" not in out, "a rejected call was never executed"


def test_the_triage_key_is_per_account() -> None:
    ctx = _ctx()
    assert ctx.eval('SH_triageKey("usman")') == "primer.shell.triage:usman"


def test_a_parked_question_in_the_ROUTE_shape_is_a_question() -> None:
    """The shape GET /workspaces/{wid}/yields/pending actually sends.

    That route answers rows of ``{session_id, kind, prompt,
    tool_call_id, parked_at}``. This module read ``tool_name``,
    ``resume_metadata`` and ``yielded_at`` instead -- none of which it
    sends -- so every parked question arrived as a nameless "approval"
    and the rail never offered a way to answer one.

    The fixture next door carries the older envelope shape on purpose,
    which is why it did not catch this: it is not the route's contract.
    """
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        JSON.stringify(SH_toAttentionItems({
          pending: [{
            session_id: "sess-1",
            kind: "ask_user",
            prompt: "What is your name?",
            tool_call_id: "tc-1",
            parked_at: "2026-08-20T09:00:00+00:00"
          }],
          records: []
        }))
        """
    ))
    assert len(out) == 1, out
    item = out[0]
    assert item["kind"] == "question", item
    assert item["title"] == "Question", item
    assert item["preview"] == "What is your name?", item
    assert item["at"] == "2026-08-20T09:00:00+00:00", item
    # A parked decision is an interrupt, not ambient: it is the tier that
    # puts the row in front of the operator.
    assert item["tier"] == "interrupt", item


def test_an_approval_park_in_the_route_shape_still_reads_as_approval() -> None:
    """kind: "approval" is the real route value (live-verified at gate
    step one) - _extract_yield_kind (primer/api/routers/workspaces.py)
    never emits "ask_approval"."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        JSON.stringify(SH_toAttentionItems({
          pending: [{
            session_id: "sess-2",
            kind: "approval",
            prompt: "run rm -rf /tmp/x",
            tool_call_id: "tc-2",
            parked_at: "2026-08-20T09:01:00+00:00"
          }],
          records: []
        }))
        """
    ))
    assert out[0]["kind"] == "approval", out
    assert out[0]["tier"] == "interrupt", out


# ---------------------------------------------------------------------------
# UX reconcile wave 3/5 (audit A item 15): response_schema is a real,
# jsonschema-enforced _AskUserArgs field (primer/toolset/_system_tools.py)
# already surfaced by GET /sessions/{id}/ask_user/pending. Wave 3 reported
# that list_pending_yields / list_session_pending_yields (primer/api/
# routers/workspaces.py) hand-built resume_metadata as {original_call}
# only and dropped it; wave 5 landed that one-key backend addition (both
# routes now include "response_schema" in resume_metadata - see
# tests/api/test_workspace_yields_pending.py /
# test_session_yields_scoped.py for the route-level coverage). These
# frontend tests were written defensively ahead of that fix and needed no
# code change once it shipped - only this comment, since the first test's
# docstring below used to describe the (now closed) gap as "the real
# route's shape".
# ---------------------------------------------------------------------------


def test_response_schema_is_absent_when_the_row_truly_has_none() -> None:
    """A row without a response_schema (e.g. a plain approval, or an
    ask_user whose _AskUserArgs never set one - both real, both still
    valid) reads as null. Must not crash or invent one."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        JSON.stringify(SH_toAttentionItems({
          pending: [{
            session_id: "sess-1", kind: "ask_user",
            prompt: "Which currency?", tool_call_id: "tc-1",
            parked_at: "2026-08-20T09:00:00+00:00"
          }],
          records: []
        }))
        """
    ))
    assert out[0]["responseSchema"] is None, out


def test_response_schema_reads_through_a_bare_item_field() -> None:
    """A bare item field (matching how "approvers" already arrives today) -
    kept as a forwards-compatible alias alongside the resume_metadata
    shape the live routes actually populate (see the next test)."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        JSON.stringify(SH_toAttentionItems({
          pending: [{
            session_id: "sess-1", kind: "ask_user",
            prompt: "Which currency?", tool_call_id: "tc-1",
            parked_at: "2026-08-20T09:00:00+00:00",
            responseSchema: {enum: ["USD", "EUR"]}
          }],
          records: []
        }))
        """
    ))
    assert out[0]["responseSchema"] == {"enum": ["USD", "EUR"]}, out


def test_response_schema_also_reads_from_resume_metadata_shape() -> None:
    """The shape every live route now actually returns: nested under
    resume_metadata (list_pending_yields / list_session_pending_yields,
    since wave 5) or as AskUserPendingResponse's own top-level field fed
    through the same key by a caller. This is the one real routes
    populate; the bare-field test above stays as a forwards-compat
    alias, not the primary path."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        JSON.stringify(SH_toAttentionItems({
          pending: [{
            session_id: "sess-1", kind: "ask_user",
            prompt: "Which currency?", tool_call_id: "tc-1",
            parked_at: "2026-08-20T09:00:00+00:00",
            resume_metadata: {response_schema: {enum: ["USD", "EUR"]}}
          }],
          records: []
        }))
        """
    ))
    assert out[0]["responseSchema"] == {"enum": ["USD", "EUR"]}, out


def test_ask_options_normalizes_the_enum() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_askOptionsOf({enum: ["Original charge currency", '
        '"Always USD", "Ask per refund"]}))'
    ))
    assert out == [
        {"value": "Original charge currency", "label": "Original charge currency"},
        {"value": "Always USD", "label": "Always USD"},
        {"value": "Ask per refund", "label": "Ask per refund"},
    ]


def test_ask_options_is_null_for_free_text() -> None:
    ctx = _ctx()
    assert ctx.eval("SH_askOptionsOf(null)") is None
    assert ctx.eval('SH_askOptionsOf({type: "string"})') is None
    assert ctx.eval('SH_askOptionsOf({enum: []})') is None


# ---------------------------------------------------------------------------
# UX reconcile wave 3 (audit A item 14): SH_routingLine/SH_viewerQualifies
# mirror primer/model/tool_approval.py's ApproverSpec.allows() exactly -
# an affordance only, the backend re-checks on the real POST.
# ---------------------------------------------------------------------------


def test_viewer_qualifies_admin_always() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'SH_viewerQualifies({kind: "roles", roles: ["ops"]}, '
        '{username: "usman", role: "admin"})'
    ) is True


def test_viewer_qualifies_anyone_spec() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'SH_viewerQualifies(null, {username: "usman", role: "engineer"})'
    ) is True
    assert ctx.eval(
        'SH_viewerQualifies({kind: "anyone"}, {username: "usman", role: "engineer"})'
    ) is True


def test_viewer_qualifies_roles_and_users() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'SH_viewerQualifies({kind: "roles", roles: ["ops"]}, '
        '{username: "usman", role: "ops"})'
    ) is True
    assert ctx.eval(
        'SH_viewerQualifies({kind: "roles", roles: ["ops"]}, '
        '{username: "usman", role: "engineer"})'
    ) is False
    assert ctx.eval(
        'SH_viewerQualifies({kind: "users", users: ["usman"]}, '
        '{username: "usman", role: "engineer"})'
    ) is True
    assert ctx.eval(
        'SH_viewerQualifies({kind: "users", users: ["someone-else"]}, '
        '{username: "usman", role: "engineer"})'
    ) is False


def test_routing_line_anyone_is_unpersonalized() -> None:
    """Qualifying is trivial and universal for "anyone" - personalizing
    it would be noise, so it stays exactly as it reads today."""
    ctx = _ctx()
    assert ctx.eval(
        'SH_routingLine({approvers: null}, {username: "usman", role: "engineer"})'
    ) == "anyone may decide"
    assert ctx.eval(
        'SH_routingLine({approvers: {kind: "anyone"}}, '
        '{username: "usman", role: "engineer"})'
    ) == "anyone may decide"


def test_routing_line_names_the_spec_and_qualification() -> None:
    ctx = _ctx()
    assert ctx.eval(
        'SH_routingLine({approvers: {kind: "roles", roles: ["admins"]}}, '
        '{username: "usman", role: "admin"})'
    ) == "who may decide: admins — you qualify"
    assert ctx.eval(
        'SH_routingLine({approvers: {kind: "roles", roles: ["ops"]}}, '
        '{username: "usman", role: "engineer"})'
    ) == "who may decide: ops"


def test_two_concurrent_gates_on_the_same_session_both_surface() -> None:
    """01a06c94: a graph fan-out can park TWO approval gates on the SAME
    session at once - list_session_pending_yields now returns one item
    per gate instead of just the primary. SH_toAttentionItems must keep
    both, each with a distinct id keyed on tool_call_id: that id is the
    React key nv-session-doc.jsx's gateItems.map uses, so an id collision
    here would silently drop one DecisionCard even though the backend
    sent both.
    """
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        JSON.stringify(SH_toAttentionItems({
          pending: [
            {
              session_id: "sess-fanout", tool_call_id: "call-0",
              kind: "approval", prompt: "delete_workspace",
              parked_at: "2026-09-04T00:00:00+00:00", approvers: null,
              resume_metadata: {
                original_call: {id: "call-0", name: "delete_workspace", arguments: {}},
                response_schema: null
              }
            },
            {
              session_id: "sess-fanout", tool_call_id: "call-1",
              kind: "approval", prompt: "delete_workspace",
              parked_at: "2026-09-04T00:00:01+00:00", approvers: null,
              resume_metadata: {
                original_call: {id: "call-1", name: "delete_workspace", arguments: {}},
                response_schema: null
              }
            }
          ],
          records: []
        }).map(function (i) { return [i.id, i.toolCallId, i.sessionId]; }))
        """
    ))
    assert len(out) == 2, out
    assert {row[0] for row in out} == {"pending:call-0", "pending:call-1"}
    assert {row[1] for row in out} == {"call-0", "call-1"}
    assert all(row[2] == "sess-fanout" for row in out)
