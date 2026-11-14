"""W7 tier 1: executable workspace lifecycle guidance over the current tools."""

import ast
import inspect
import json
import re
from types import SimpleNamespace
from typing import get_args

import jsonschema
import pytest

from primer.api.registries.workspace_registry import new_workspace_row
from primer.model.access import Access
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta, WorkspaceTemplate
from primer.toolset import workspaces
from tests.docs._wiki import repo_wiki, tool_entries

PATH = "runbooks/manage-a-workspace"
FLOW = (
    "workspaces__list_workspace_templates",
    "workspaces__get_workspace_template",
    "workspaces__create_workspace",
    "workspaces__get_workspace",
    "workspaces__list_workspaces",
    "workspaces__list_workspace_files",
    "workspaces__read_workspace_file",
    "workspaces__write_workspace_file",
    "workspaces__get_workspace_log",
    "workspaces__create_workspace_session",
    "workspaces__get_workspace_session",
    "workspaces__delete_workspace",
    "system__get_agent",
    "system__list_model_profile_choices",
    "system__describe_tool",
)
WRITES = ("create_workspace", "write_workspace_file", "create_workspace_session", "delete_workspace")


@pytest.mark.parametrize("scoped", FLOW)
def test_flow_tools_keep_the_documented_floor_and_shipped_gate(scoped):
    entry = {entry.scoped_id: entry for entry in tool_entries()}[scoped]
    assert entry.required_role == "user"
    assert entry.default_gate is None
    assert entry.via_call_tool


def test_boot_page_and_each_exact_reference_are_present():
    pages = repo_wiki().pages
    page = pages[PATH]
    assert page.kind == "guide"
    assert page.meta["tools"] == list(FLOW)
    assert page.meta["audience"] == "admin"
    headings = re.findall(r"^## (.+)$", page.body, re.M)
    assert headings == ["Goal", "Preconditions", "Roles and gates", "Steps", "Verification", "Refusal and failure", "Related"]
    for scoped in FLOW:
        reference = "reference/tools/" + scoped.replace("__", "/")
        assert reference in pages
        assert f"]({reference})" in page.body
    assert chr(0x2014) not in page.body


def test_every_example_is_complete_json_and_validates_including_the_live_gate_reads():
    entries = {entry.scoped_id: entry for entry in tool_entries()}
    body = repo_wiki().pages[PATH].body
    blocks = re.findall(r"```json\n(.*?)\n```", body, re.S)
    assert blocks
    calls = [json.loads(block) for block in blocks]
    assert {call["toolset_id"] + "__" + call["tool_name"] for call in calls} == set(FLOW)
    for call in calls:
        # No invalid JSON block or unrecognised envelope can evade validation.
        jsonschema.validate(call, entries["system__call_tool"].tool.args_schema)
        scoped = call["toolset_id"] + "__" + call["tool_name"]
        jsonschema.validate(call["arguments"], entries[scoped].tool.args_schema)
        assert "<" not in json.dumps(call)  # worked values, not placeholders
    described = [call["arguments"]["tool_name"] for call in calls if call["tool_name"] == "describe_tool"]
    assert sorted(described) == sorted(WRITES)
    for name in WRITES:
        gate_index = next(i for i, call in enumerate(calls) if call["tool_name"] == "describe_tool" and call["arguments"]["tool_name"] == name)
        write_index = next(i for i, call in enumerate(calls) if call["tool_name"] == name)
        assert gate_index < write_index
        assert f"| `workspaces__{name}` | none |" in body
    assert calls[-1]["tool_name"] == "list_workspaces"


def test_creation_readback_uses_pending_snapshot_and_known_creator_ownership():
    template = WorkspaceTemplate(id="local-default", provider_id="local", description="Tier 1 fixture")
    access = Access.owned_by("workspace-user")
    row = new_workspace_row(
        workspace_id="w7-workspace", template=template, overrides=None,
        live=SimpleNamespace(runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:1", token="fixture")), access=access,
    )
    assert row.phase == "pending"
    assert get_args(Workspace.model_fields["phase"].annotation) == ("pending", "running", "failed", "terminating")
    assert row.template_snapshot == template
    assert row.access.owners == ["workspace-user"]
    body = repo_wiki().pages[PATH].body
    assert "phase=pending" in body and "usable immediately" in body
    assert "known user" in body and "access.owners" in body


def test_documented_access_levels_and_delete_refusals_match_the_handlers():
    functions = {
        node.name: ast.unparse(node)
        for node in ast.walk(ast.parse(inspect.getsource(workspaces)))
        if isinstance(node, ast.AsyncFunctionDef)
    }
    for name, level in (("_list_files", "READ"), ("_read_file", "READ"), ("_get_log", "READ"), ("_write_file", "FULL"), ("_create_session", "FULL"), ("_delete_workspace", "OWNER")):
        assert f"AccessLevel.{level}" in functions[name]
    delete = functions["_delete_workspace"]
    create = functions["_create_workspace"]
    assert "Access.owned_by(person.user_id)" in create
    assert "person.user_id is not None" in create and "patch_if" in create
    assert "ReservedWorkspaceError" in delete and "error_type='forbidden'" in delete
    assert "NotFoundError" in delete and "error_type='not-found'" in delete
    assert "error_type='backend-error'" in delete
    body = repo_wiki().pages[PATH].body
    for fact in ("owner", "read", "full", "phase=terminating", "phase=failed", "failure_reason", "workspace_lost", "default workspace", "is_default", "cannot be undone", "template retirement", "provider cannot be resolved", "do not retry", "truncated", "state-repo"):
        assert fact in body, fact


def test_runbook_is_discoverable_from_both_purpose_tables_and_the_index():
    pages = repo_wiki().pages
    link = f"]({PATH})"
    assert pages["purpose-map"].body.count(link) == 2
    # The build appends a generated child listing as well as the authored table.
    authored_table = pages["runbooks"].body.split("## Related", 1)[0]
    assert authored_table.count(link) == 1
    assert "create or manage a workspace (from a template; its files; starting a session; what deletion destroys)" in pages["purpose-map"].body
