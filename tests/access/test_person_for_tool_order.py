"""``person_for_tool`` resolves the person in one fixed order across the phases (access-control chain).

1. a call with a ``ToolContext``: the run's initiator, whatever else is set;
2. a delegated (sub)agent run with no context: unattributed (only public rows read), even while a person acts;
3. the acting person (:func:`primer.access.acting.acting_person`: the request's, or the run's around its listing);
4. the MCP request's ``current_actor``;
5. nobody: unattributed.
"""

from __future__ import annotations

from primer.access.acting import acting_as
from primer.access.person import Person, person_for_tool
from primer.agent.invoke import _RUN_ID
from primer.mcp.server import current_actor
from primer.model.principal import Principal, PrincipalRef
from primer.model.yield_ import ToolContext

ACTING = Person("u-acting", "user")
MCP = Principal(type="user", id="u-mcp", display="mcp", role="user", source="local", user_id="u-mcp")
INITIATOR = PrincipalRef(type="user", id="u-run", display="run", role="user", source="local", user_id="u-run")
UNATTRIBUTED = Person(None, "user")


def test_a_context_beats_everything() -> None:
    ctx = ToolContext(tool_call_id="c", session_id="s", workspace_id="w", initiated_by=INITIATOR)
    token = current_actor.set(MCP)
    run = _RUN_ID.set("run-1")
    try:
        with acting_as(ACTING):
            assert person_for_tool(ctx) == Person("u-run", "user")
    finally:
        _RUN_ID.reset(run)
        current_actor.reset(token)


def test_a_delegated_run_with_no_context_is_unattributed_even_while_a_person_acts() -> None:
    token = current_actor.set(MCP)
    run = _RUN_ID.set("run-1")
    try:
        with acting_as(ACTING):
            assert person_for_tool(None) == UNATTRIBUTED
    finally:
        _RUN_ID.reset(run)
        current_actor.reset(token)


def test_the_acting_person_beats_the_mcp_actor() -> None:
    token = current_actor.set(MCP)
    try:
        with acting_as(ACTING):
            assert person_for_tool(None) == ACTING
    finally:
        current_actor.reset(token)


def test_the_mcp_actor_is_used_when_nobody_acts_and_no_run_is_delegated() -> None:
    token = current_actor.set(MCP)
    try:
        assert person_for_tool(None) == Person("u-mcp", "user")
    finally:
        current_actor.reset(token)


def test_nobody_is_unattributed() -> None:
    assert person_for_tool(None) == UNATTRIBUTED
