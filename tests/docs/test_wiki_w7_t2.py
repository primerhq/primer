"""T2 runbooks agree with current schemas, gates and runnable graph contracts."""

import json
import re

import jsonschema
import pytest

from primer.model.agent import Agent
from primer.model.graph import Graph
from tests.docs._wiki import repo_wiki, tool_entries

AGENT_TOOLS = tuple("system__" + name for name in (
    "create_agent", "update_agent", "delete_agent", "get_agent", "find_agents", "list_agents",
    "invoke_agent", "list_model_profile_choices", "describe_tool",
)) + ("crud__create_agent", "crud__update_agent")
GRAPH_TOOLS = tuple("system__" + name for name in (
    "create_graph", "update_graph", "delete_graph", "get_graph", "find_graphs", "list_graphs",
    "create_graph_thread", "get_graph_thread", "get_agent", "describe_tool",
)) + ("crud__create_graph", "crud__update_graph", "workspace_ext__invoke_graph") + tuple(
    "workspaces__" + name for name in ("get_workspace", "create_workspace_session", "get_workspace_session", "read_workspace_session_messages")
)
FLOWS = {"runbooks/create-an-agent": AGENT_TOOLS, "runbooks/create-and-run-a-graph": GRAPH_TOOLS}
GATED = {
    "system__create_agent", "system__update_agent", "system__delete_agent", "crud__create_agent", "crud__update_agent",
    "system__create_graph", "system__update_graph", "system__delete_graph", "crud__create_graph", "crud__update_graph",
}


def calls(path):
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", repo_wiki().pages[path].body, re.S)]


@pytest.mark.parametrize("path", FLOWS)
def test_runbooks_have_the_authoring_structure_exact_tools_and_references(path):
    pages = repo_wiki().pages
    page = pages[path]
    assert page.kind == "guide" and page.meta["audience"] == "admin"
    assert page.meta["tools"] == list(FLOWS[path])
    assert re.findall(r"^## (.+)$", page.body, re.M) == ["Goal", "Preconditions", "Roles and gates", "Steps", "Verification", "Refusal and failure", "Related"]
    for scoped in FLOWS[path]:
        reference = "reference/tools/" + scoped.replace("__", "/")
        assert reference in pages and f"]({reference})" in page.body
    assert chr(0x2014) not in page.body


@pytest.mark.parametrize("scoped", sorted(set(AGENT_TOOLS + GRAPH_TOOLS)))
def test_live_role_and_shipped_gate_match_the_two_twin_surfaces(scoped):
    entry = {entry.scoped_id: entry for entry in tool_entries()}[scoped]
    assert entry.required_role == "user" and entry.via_call_tool
    assert entry.default_gate == ("required" if scoped in GATED else None)


@pytest.mark.parametrize("path", FLOWS)
def test_every_worked_call_validates_and_all_mutators_have_a_prior_live_gate_read(path):
    entries = {entry.scoped_id: entry for entry in tool_entries()}
    page_calls = calls(path)
    seen = set()
    for call in page_calls:
        jsonschema.validate(call, entries["system__call_tool"].tool.args_schema)
        scoped = call["toolset_id"] + "__" + call["tool_name"]
        jsonschema.validate(call["arguments"], entries[scoped].tool.args_schema)
        seen.add(scoped)
        assert "<" not in json.dumps(call)
    missing = set(FLOWS[path]) - seen
    assert missing == ({"system__create_graph_thread", "system__get_graph_thread"} if "graph" in path else set())
    writes = (GATED & seen) | ({"workspaces__create_workspace_session"} & seen)
    for scoped in writes:
        write_position = next(i for i, call in enumerate(page_calls) if call["toolset_id"] + "__" + call["tool_name"] == scoped)
        toolset, name = scoped.split("__")
        describe_position = next(i for i, call in enumerate(page_calls) if call["tool_name"] == "describe_tool" and call["arguments"] == {"toolset_id": toolset, "tool_name": name})
        assert describe_position < write_position
        assert f"| `{scoped}` | {'required' if scoped in GATED else 'none'} |" in repo_wiki().pages[path].body


@pytest.mark.parametrize("path", FLOWS)
def test_worked_definitions_validate_as_real_models_and_graphs_are_runnable(path):
    for call in calls(path):
        if call["tool_name"] in {"create_agent", "update_agent"}:
            entity = Agent.model_validate(call["arguments"]["entity"])
            assert entity.model.profile_id == "team-chat"
            assert entity.tools == ["system__wiki_read"]
            assert entity.max_tool_turns == 8 and entity.harness_id is None
        if call["tool_name"] in {"create_graph", "update_graph"}:
            graph = Graph.model_validate(call["arguments"]["entity"])
            graph.assert_runnable()
            begin, node, end = graph.nodes
            assert begin.input_schema == {"type": "string", "minLength": 1}
            assert node.agent_id == "w7-agent" and node.input_template == "{{ nodes.begin.text }}"
            assert end.output_template == "GRAPH-ANSWER:{{ nodes.answer.text }}"


def test_graph_binding_and_nested_invocation_use_their_actual_distinct_input_surfaces():
    page_calls = calls("runbooks/create-and-run-a-graph")
    bound = next(c["arguments"] for c in page_calls if c["tool_name"] == "create_workspace_session")
    nested = next(c["arguments"] for c in page_calls if c["tool_name"] == "invoke_graph")
    assert bound["binding"] == {"kind": "graph", "graph_id": "w7-graph"}
    assert bound["graph_input"] == nested["input"] and isinstance(nested["input"], str)
    body = repo_wiki().pages["runbooks/create-and-run-a-graph"].body
    for fact in ("do not create GraphThread", "does not run a graph", "standalone MCP", "bad-request", "draft", "exactly one Begin", "reachable End", "max_iterations", "in_use_by", "does not delete its workspaces"):
        assert fact in body, fact


def test_agent_replacement_identity_caps_and_approval_caveats_are_explicit():
    body = repo_wiki().pages["runbooks/create-an-agent"].body
    for fact in ("default policy", "not a guarantee", "do not retry", "same required gate", "whole entity", "immutable id", "access.owners", "read", "full", "owner", "partial_output", "stateless", "prompt text and description"):
        assert fact in body, fact


@pytest.mark.parametrize("path", FLOWS)
def test_each_flow_has_both_purpose_rows_and_one_authored_index_row(path):
    pages = repo_wiki().pages
    assert pages["purpose-map"].body.count(f"]({path})") == 2
    authored = pages["runbooks"].body.split("## Related", 1)[0]
    assert authored.count(f"]({path})") == 1
