"""Current trigger runbook calls, ownership, gates and actual acceptance."""

import json
import re

import jsonschema
import pytest

from tests.docs._wiki import repo_wiki, tool_entries

PAGE = "runbooks/wire-a-trigger"
TOOLS = tuple("trigger__" + n for n in (
    "create", "update", "delete", "get", "list", "fire_now", "create_subscription", "get_subscription",
    "list_subscriptions", "update_subscription", "delete_subscription",
)) + ("system__describe_tool", "system__get_agent", "workspaces__get_workspace",
      "workspaces__list_workspace_sessions", "workspaces__get_workspace_session")


def calls():
    return [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", repo_wiki().pages[PAGE].body, re.S)]


def test_trigger_runbook_has_current_citations_structure_and_both_index_rows():
    pages = repo_wiki().pages;page = pages[PAGE]
    assert page.kind == "guide" and page.meta["audience"] == "user" and page.meta["tools"] == list(TOOLS)
    assert re.findall(r"^## (.+)$", page.body, re.M) == [
        "Goal", "Preconditions", "Roles and gates", "Steps", "Verification", "Refusal and failure", "Related",
    ]
    for scoped in TOOLS:
        path = "reference/tools/" + scoped.replace("__", "/")
        assert path in pages and f"]({path})" in page.body
    assert pages["purpose-map"].body.count(f"]({PAGE})") == 2
    assert f"]({PAGE})" in pages["runbooks"].body


def test_every_worked_call_and_inner_argument_match_the_live_schema():
    entries = {e.scoped_id: e for e in tool_entries()};seen = set()
    for call in calls():
        scoped = call["toolset_id"] + "__" + call["tool_name"]
        jsonschema.validate(call, entries["system__call_tool"].tool.args_schema)
        assert scoped in TOOLS
        jsonschema.validate(call["arguments"], entries[scoped].tool.args_schema)
        seen.add(scoped)
    assert seen == set(TOOLS)


@pytest.mark.parametrize("scoped", TOOLS)
def test_trigger_runbook_preserves_all_user_roles_and_only_three_required_defaults(scoped):
    entry = next(e for e in tool_entries() if e.scoped_id == scoped)
    assert entry.required_role == "user"
    assert entry.default_gate == ("required" if scoped in {"trigger__create", "trigger__update", "trigger__delete"} else None)


def test_worked_flow_separates_source_subscription_and_disabled_delivery():
    variants = [c["arguments"] for c in calls() if c["tool_name"] == "create"]
    assert {c["config"]["kind"] for c in variants} == {"delayed", "scheduled", "webhook"}
    delayed = next(c for c in variants if c["config"]["kind"] == "delayed")
    assert delayed["config"]["fire_at"] == "2099-01-01T09:00:00Z" and "interactive" not in delayed["config"]
    assert "token" not in next(c for c in variants if c["config"]["kind"] == "webhook")["config"]
    sub = next(c["arguments"] for c in calls() if c["tool_name"] == "create_subscription")
    assert sub["enabled"] is False and sub["parallelism"] == "queue" and sub["config"]["kind"] == "agent_fresh_session"
    assert "{{ fire_id }}" in sub["payload_template"] and "{{ trigger_id }}" in sub["payload_template"]
    assert next(c["arguments"] for c in calls() if c["tool_name"] == "update_subscription")["enabled"] is True
    assert next(c["arguments"] for c in calls() if c["tool_name"] == "update")["enabled"] is False


def test_verification_requires_real_identity_input_answer_token_boundary_and_default_preservation():
    body = repo_wiki().pages[PAGE].body
    for text in ("actual transcript", "input-derived", "session_id", "last_fire_error", "no new session",
                 "owner/admin", "masked token is not a credential", "default workspace", "original caller",
                 "read-only", "do not retry", "does not prove an external webhook sender or a clock tick"):
        assert text in body
