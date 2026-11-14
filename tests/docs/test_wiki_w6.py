"""W6 (wiki-first): the moderator and bookkeeper runbooks, and the composition
contract they walk (the RED probe verdict: composition is sufficient, NO new
operation or tool).

The verdict is pinned here at four levels:

* the catalogue level - every scoped id the two flows walk exists with the
  role floor the runbooks state, and the shipped gate states are the ones
  the Roles-and-gates tables name (ungated workspace creates, a required
  gate on agent creation);
* the model level - the session-state fields the bookkeeper flow reads
  (the derived state vocabulary, the park key, the binding union);
* the source level - the two visibility scopes (list_workspaces confined to
  the workspaces the person may read; find_sessions confined to readable
  workspaces);
* the wiki level - the boot build includes the two runbooks, they cite the
  exact scoped ids and reference pages, and the index rows (purpose-map
  roles and runbook set, the runbooks index set) replaced the pending rows.

The generic page and coverage tests pin the shared contract every page
holds (frontmatter rules, link resolution, worked calls against the live
schemas, no unknown tool names, no em dash); these tests pin what W6 itself
must hold.
"""

from datetime import UTC, datetime

from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionInfo,
    SessionState,
    SessionStatus,
    WorkspaceSession,
)
from primer.toolset import workspaces as workspaces_toolset
from primer.toolset.tool_catalog import default_gate
from tests.docs._wiki import repo_wiki, tool_entries

PAGES = repo_wiki().pages
ENTRIES = {e.scoped_id: e for e in tool_entries()}

#: The flow tools both runbooks walk, and the role floor each ships with
#: (the operator reaches them through the core-kit door, so every one of
#: them must be user-tier).
FLOW_TOOLS = (
    "system__invoke_agent",
    "workspaces__list_workspace_templates",
    "workspaces__create_workspace",
    "workspaces__create_workspace_session",
    "workspaces__get_workspace_session",
    "workspaces__list_workspaces",
    "workspaces__find_sessions",
    "workspaces__list_workspace_sessions",
    "system__describe_tool",
)


def test_the_composition_contract_holds_at_the_catalogue_level():
    # RED probe, rung 1+2: every flow tool is live with the runbooks' role
    # floor, and the shipped gate states are the ones the gate tables name.
    for scoped in FLOW_TOOLS:
        assert scoped in ENTRIES, f"{scoped} is not a live tool"
        assert ENTRIES[scoped].required_role == "user", scoped
    for toolset_id, tool_name in (
        ("workspaces", "create_workspace"),
        ("workspaces", "create_workspace_session"),
        ("workspaces", "list_workspaces"),
        ("workspaces", "list_workspace_sessions"),
        ("workspaces", "get_workspace_session"),
        ("workspaces", "find_sessions"),
        ("system", "invoke_agent"),
        ("system", "describe_tool"),
    ):
        assert default_gate(toolset_id, tool_name) is None, f"{toolset_id}__{tool_name}"
    # the one default-gated step the moderator runbook names (a missing agent)
    assert default_gate("system", "create_agent") == "required"


def test_the_bookkeeper_state_fields_are_served():
    # RED probe, rung 3: the bookkeeper flow reads the derived state and the
    # park key off the session row; the runbook's state filters are exactly
    # the derived-state vocabulary.
    assert "status" in SessionInfo.model_fields
    assert "parked_event_key" in WorkspaceSession.model_fields
    import typing

    assert typing.get_args(SessionState) == ("waiting", "running", "parked", "ended")
    assert {m.value for m in SessionStatus} == {
        "created", "running", "waiting", "paused", "ended",
    }
    # the binding union the moderator runbook's launch step cites
    assert AgentSessionBinding.model_fields["kind"].default == "agent"
    assert "agent_id" in AgentSessionBinding.model_fields
    assert GraphSessionBinding.model_fields["kind"].default == "graph"
    assert "graph_id" in GraphSessionBinding.model_fields
    rested = WorkspaceSession(
        id="w6-rested", workspace_id="w6-workspace",
        binding=AgentSessionBinding(agent_id="operator"), status=SessionStatus.WAITING, turn_no=1,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert rested.session_state == "parked" and rested.parked_event_key is None


def test_the_two_visibility_scopes_are_pinned_in_source():
    # RED probe, rung 4: the bookkeeper flow reaches "all visible workspaces"
    # because list_workspaces is scoped to the person's readable workspaces
    # and find_sessions confines a workspace-less search to readable ones.
    # (Both closures live inside build_workspaces_toolset, so the pins are on
    # the module source, not on importable attributes.)
    import inspect

    module_src = inspect.getsource(workspaces_toolset)
    visible = module_src[module_src.index("async def _workspaces_visible"):]
    assert "visible_predicate(person_for_tool(ctx))" in visible[:400]  # the list scope
    assert "scope=_workspaces_visible" in module_src  # wired onto list_workspaces
    find_src = module_src[module_src.index("async def _find_sessions"):]
    assert "readable_parent_filter" in find_src[:2000]


def test_the_boot_build_includes_the_w6_runbooks():
    kinds = {p: v.kind for p, v in PAGES.items()}
    assert kinds["runbooks/moderate-a-request"] == "guide"
    assert kinds["runbooks/workspace-status"] == "guide"


def test_the_moderator_runbook_walks_the_pinned_surface():
    page = PAGES["runbooks/moderate-a-request"]
    assert page.meta["tools"] == [
        "system__invoke_agent",
        "workspaces__list_workspace_templates",
        "workspaces__create_workspace",
        "workspaces__create_workspace_session",
        "workspaces__get_workspace_session",
        "system__create_agent",
        "system__describe_tool",
    ]
    for ref in (
        "reference/tools/system/invoke_agent",
        "reference/tools/workspaces/list_workspace_templates",
        "reference/tools/workspaces/create_workspace",
        "reference/tools/workspaces/create_workspace_session",
        "reference/tools/workspaces/get_workspace_session",
        "reference/tools/system/create_agent",
        "reference/tools/system/describe_tool",
    ):
        assert f"]({ref})" in page.body, f"{ref}: no link to the reference page"
    body = page.body
    # the flow shape the verdict composes: planner, separate workspace, launch
    assert '"planner"' in body and "separate workspace" in body
    # the gate table states the shipped states the catalogue pins
    assert "shipped gate: none" in body and "shipped gate: required" in body
    # the live gate is asserted, not assumed (the B3 acceptance)
    assert "describe_tool" in body


def test_the_workspace_status_runbook_walks_the_pinned_surface():
    page = PAGES["runbooks/workspace-status"]
    assert page.meta["tools"] == [
        "workspaces__list_workspaces",
        "workspaces__find_sessions",
        "workspaces__list_workspace_sessions",
        "workspaces__get_workspace_session",
    ]
    for ref in (
        "reference/tools/workspaces/list_workspaces",
        "reference/tools/workspaces/find_sessions",
        "reference/tools/workspaces/list_workspace_sessions",
        "reference/tools/workspaces/get_workspace_session",
    ):
        assert f"]({ref})" in page.body, f"{ref}: no link to the reference page"
    body = page.body
    # the bookkeeper question answered from live state, with the park reason
    assert "state\": \"parked\"" in body and "state\": \"running\"" in body
    assert "parked_event_key" in body
    # the own-workspace view is named as the complement, not the flow
    assert "list_my_sessions" in body
    assert "without a park key" in body  # a rested/paused session is not necessarily blocked
    assert "no offset or cursor" in body and "remaining truncation" in body


def test_the_index_rows_replaced_the_pending_rows():
    purpose = PAGES["purpose-map"].body
    # the operator's own roles table: the two pending rows are live now
    assert "| [runbooks/moderate-a-request](runbooks/moderate-a-request) |" in purpose
    assert "| [runbooks/workspace-status](runbooks/workspace-status) |" in purpose
    assert "pending: runbooks/moderate-a-request" not in purpose
    assert "pending: runbooks/workspace-status" not in purpose
    # the runbook set table gained a row per runbook, in the same PR
    assert "| [runbooks/moderate-a-request](runbooks/moderate-a-request) |" in purpose
    assert "| [runbooks/workspace-status](runbooks/workspace-status) |" in purpose
    index = PAGES["runbooks"].body
    assert "](runbooks/moderate-a-request)" in index
    assert "](runbooks/workspace-status)" in index
