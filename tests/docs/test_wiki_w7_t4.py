"""Current channel onboarding mechanisms, credentials and real event acceptance."""

import json
import re

import jsonschema
import pytest

from primer.trigger.payload import render_payload
from tests.docs._wiki import repo_wiki, tool_entries

PAGE = "runbooks/onboard-a-channel"
TOOLS = (
    "system__list_channel_providers", "system__create_channel_provider", "system__get_channel_provider",
    "workspaces__get_workspace", "system__get_agent", "system__describe_tool",
    "system__create_channel", "system__get_channel", "trigger__create", "trigger__get",
    "trigger__list", "trigger__fire_now", "system__create_channel_binding",
    "system__list_channel_bindings", "system__set_reply_binding", "workspaces__list_workspace_sessions",
    "workspaces__get_workspace_session", "system__delete_channel_binding",
    "system__clear_reply_binding", "trigger__delete",
)


def calls():
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", repo_wiki().pages[PAGE].body, re.S)]


def test_channel_runbook_cites_current_tools_and_follows_the_authoring_contract():
    pages = repo_wiki().pages
    page = pages[PAGE]
    assert page.kind == "guide" and page.meta["audience"] == "admin"
    assert page.meta["tools"] == list(TOOLS)
    assert re.findall(r"^## (.+)$", page.body, re.M) == [
        "Goal", "Preconditions", "Roles and gates", "Steps", "Verification", "Refusal and failure", "Related",
    ]
    for scoped in TOOLS:
        path = "reference/tools/" + scoped.replace("__", "/")
        assert path in pages and f"]({path})" in page.body
    assert pages["purpose-map"].body.count(f"]({PAGE})") == 2
    assert f"]({PAGE})" in pages["runbooks"].body


def test_every_worked_call_and_inner_argument_validates_against_the_current_schema():
    entries = {e.scoped_id: e for e in tool_entries()}
    seen = set()
    for call in calls():
        scoped = call["toolset_id"] + "__" + call["tool_name"]
        jsonschema.validate(call, entries["system__call_tool"].tool.args_schema)
        assert scoped in TOOLS
        jsonschema.validate(call["arguments"], entries[scoped].tool.args_schema)
        seen.add(scoped)
    # Provider creation is the private-credential admin precondition. No
    # fictitious secret is presented as a working connection in the runbook.
    assert set(TOOLS) - {"system__create_channel_provider"} <= seen


@pytest.mark.parametrize("scoped", TOOLS)
def test_channel_roles_and_the_two_required_trigger_gates_are_current(scoped):
    entry = next(e for e in tool_entries() if e.scoped_id == scoped)
    assert entry.required_role == ("admin" if scoped in {
        "system__list_channel_providers", "system__create_channel_provider", "system__get_channel_provider",
    } else "user")
    assert entry.default_gate == ("required" if scoped in {"trigger__create", "trigger__delete"} else None)


def test_worked_subscription_consumes_actual_event_text_and_separates_reply_scopes():
    sample = next(c["arguments"] for c in calls() if c["tool_name"] == "create_channel_binding")
    assert sample["config"] == {"kind": "agent_fresh_session", "workspace_id": "team-work", "agent_id": "team-helper"}
    assert sample["event_matcher"] == {"event_type": "message.posted", "text_pattern": "^help:"}
    assert sample["reply_target"] == "source_thread"
    assert render_payload(sample["payload_template"], {"event": {"text": "help: Cedar request"}}) == "help: Cedar request"
    standing = next(c["arguments"] for c in calls() if c["tool_name"] == "set_reply_binding")
    assert standing == {"workspace_id": "team-work", "channel_id": "team-room"}
    room = next(c["arguments"]["entity"] for c in calls() if c["tool_name"] == "create_channel")
    assert room["provider"] == "slack" and room["config"]["chats"]["enabled"] is True


def test_runbook_requires_listener_activation_and_actual_inbound_answer():
    page = repo_wiki().pages[PAGE].body
    assert "tool save requests listener refresh automatically" in page
    assert "skipped_no_match" in page and "no ChannelEvent" in page
    assert "do not remove the matcher" in page
    assert "actual transcript" in page and "correct room/thread" in page
    assert "served mask" in page and "Credentials are deliberately absent" in page
    assert "does not itself prove a live listener" in page
    assert "Live channel diagnostics" in repo_wiki().pages["not-agent-callable"].body
