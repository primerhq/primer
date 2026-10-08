"""Creating or updating an agent that names a model profile or a toolset that does not exist is refused (finding A-09, the create/update half).

``POST /v1/agents`` answered 201 for an agent whose ``model.profile_id`` and ``tools`` named things that did not exist; ``GET
/v1/agents/{id}/status`` then said so, and the agent failed at its first turn. The rule is now one shared check
(``primer/agent/agent_checks.py``) behind the REST router, the system ``create_agent`` / ``update_agent`` tools and the builder's
``crud`` toolset, and it is the same question ``/status`` asks:

* ``model.profile_id`` must name a stored ModelProfile;
* every toolset a tool id names (the part before the last ``__``; a tool id with no ``__`` is its own toolset) must resolve: either an
  in-process built-in (``system``, ``workspaces``, ``misc``, ``web``, ``harness``, ``trigger``, ``collections``, ``crud``, including
  their legacy ``_``-prefixed aliases) or a stored Toolset row.

An UPDATE refuses only a reference it ADDS: an agent that already names a toolset which has since been deleted can still have its
description edited; changing the profile to a missing one, or adding a tool of a missing toolset, is refused.
"""

from __future__ import annotations

import pytest

from primer.model.agent import Agent
from tests._support.agent_check_rows import (
    agent_body,
    profile_missing_message,
    profile_row,
    toolset_row,
    toolsets_missing_message,
)
from tests.api.conftest import app, client, fake_provider_registry  # noqa: F401


async def _seed(app, row) -> None:
    await app.state.storage_provider.get_storage(type(row)).create(row)


async def _seed_agent_directly(app, body: dict) -> None:
    """Store an agent WITHOUT the router's checks: how a dangling reference exists in the first place (a toolset deleted afterwards)."""
    await _seed(app, Agent.model_validate(body))


# ---- create ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_naming_a_missing_profile_is_refused_and_not_stored(client, app) -> None:
    r = await client.post("/v1/agents", json=agent_body("ag-1", profile_id="ghost-profile"))

    assert r.status_code == 422, r.text
    assert profile_missing_message("ghost-profile") in r.text
    assert "model.profile_id" in r.text and "model_profile_not_found" in r.text
    assert await app.state.storage_provider.get_storage(Agent).get("ag-1") is None


@pytest.mark.asyncio
async def test_an_agent_naming_an_existing_profile_with_no_tools_is_created(client, app) -> None:
    await _seed(app, profile_row("mp-1"))

    r = await client.post("/v1/agents", json=agent_body("ag-1"))

    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_an_agent_with_a_tool_of_a_missing_toolset_is_refused_and_not_stored(client, app) -> None:
    await _seed(app, profile_row("mp-1"))

    r = await client.post("/v1/agents", json=agent_body("ag-1", tools=["ghost-ts__do_it"]))

    assert r.status_code == 422, r.text
    assert toolsets_missing_message("ghost-ts") in r.text and "toolset_not_found" in r.text
    assert await app.state.storage_provider.get_storage(Agent).get("ag-1") is None


@pytest.mark.asyncio
async def test_every_missing_toolset_is_named_once_in_sorted_order(client, app) -> None:
    await _seed(app, profile_row("mp-1"))

    r = await client.post(
        "/v1/agents", json=agent_body("ag-1", tools=["zeta__a", "alpha__a", "zeta__b", "alpha__b", "mid__x"]),
    )

    assert r.status_code == 422, r.text
    assert toolsets_missing_message("alpha", "mid", "zeta") in r.text, r.text


@pytest.mark.asyncio
async def test_the_profile_is_reported_before_the_toolsets(client, app) -> None:
    r = await client.post("/v1/agents", json=agent_body("ag-1", profile_id="ghost-profile", tools=["ghost-ts__x"]))

    assert r.status_code == 422
    assert "model_profile_not_found" in r.text and "toolset_not_found" not in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool",
    [
        "system__list_agents", "workspaces__read_file", "misc__sleep", "web__web_fetch", "harness__list_harnesses",
        "trigger__create_trigger", "collections__list_documents", "crud__create_agent",
        # the legacy underscore-prefixed built-in ids that agents persisted before the rename still carry
        "_system__list_agents", "_workspaces__read_file", "_misc__sleep",
    ],
)
async def test_a_tool_of_an_in_process_built_in_toolset_needs_no_row(client, app, tool: str) -> None:
    await _seed(app, profile_row("mp-1"))

    r = await client.post("/v1/agents", json=agent_body("ag-1", tools=[tool]))

    assert r.status_code == 201, f"{tool}: {r.text}"


@pytest.mark.asyncio
async def test_a_tool_of_a_stored_toolset_is_accepted(client, app) -> None:
    await _seed(app, profile_row("mp-1"))
    await _seed(app, toolset_row("my-ts"))

    r = await client.post("/v1/agents", json=agent_body("ag-1", tools=["my-ts__do_it"]))

    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_a_tool_id_with_no_scope_prefix_is_its_own_toolset(client, app) -> None:
    """The rule ``/agents/{id}/status`` has always applied: a tool id without ``__`` names a toolset of that id."""
    await _seed(app, profile_row("mp-1"))

    r = await client.post("/v1/agents", json=agent_body("ag-1", tools=["bare-tool"]))

    assert r.status_code == 422 and "'bare-tool'" in r.text, r.text


@pytest.mark.asyncio
async def test_search_is_a_stored_toolset_not_a_built_in(client, app) -> None:
    """``search`` is resolved from storage (it exists once Internal Collections is configured), so naming it needs its row."""
    await _seed(app, profile_row("mp-1"))

    refused = await client.post("/v1/agents", json=agent_body("ag-1", tools=["search__search_agents"]))
    await _seed(app, toolset_row("search"))
    accepted = await client.post("/v1/agents", json=agent_body("ag-2", tools=["search__search_agents"]))

    assert refused.status_code == 422 and accepted.status_code == 201, (refused.text, accepted.text)


# ---- update: only a reference the update ADDS is refused ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_that_already_names_a_deleted_toolset_can_still_be_edited(client, app) -> None:
    await _seed(app, profile_row("mp-1"))
    await _seed_agent_directly(app, agent_body("ag-1", tools=["gone-ts__x"]))
    edited = {**agent_body("ag-1", tools=["gone-ts__x"]), "description": "a new description"}

    r = await client.put("/v1/agents/ag-1", json=edited)

    assert r.status_code == 200, r.text
    assert r.json()["description"] == "a new description"


@pytest.mark.asyncio
async def test_an_update_that_adds_a_tool_of_a_missing_toolset_is_refused_and_leaves_the_row(client, app) -> None:
    await _seed(app, profile_row("mp-1"))
    await _seed_agent_directly(app, agent_body("ag-1", tools=[]))

    r = await client.put("/v1/agents/ag-1", json=agent_body("ag-1", tools=["ghost-ts__x"]))

    assert r.status_code == 422 and "'ghost-ts'" in r.text, r.text
    assert (await app.state.storage_provider.get_storage(Agent).get("ag-1")).tools == []


@pytest.mark.asyncio
async def test_an_update_that_adds_one_missing_toolset_does_not_name_the_one_that_was_already_missing(client, app) -> None:
    await _seed(app, profile_row("mp-1"))
    await _seed_agent_directly(app, agent_body("ag-1", tools=["old-gone__x"]))

    r = await client.put("/v1/agents/ag-1", json=agent_body("ag-1", tools=["old-gone__x", "new-gone__y"]))

    assert r.status_code == 422, r.text
    assert "'new-gone'" in r.text and "'old-gone'" not in r.text, r.text


@pytest.mark.asyncio
async def test_an_update_that_points_the_agent_at_a_missing_profile_is_refused(client, app) -> None:
    await _seed(app, profile_row("mp-1"))
    await _seed_agent_directly(app, agent_body("ag-1"))

    r = await client.put("/v1/agents/ag-1", json=agent_body("ag-1", profile_id="ghost-profile"))

    assert r.status_code == 422 and "model_profile_not_found" in r.text, r.text
    assert (await app.state.storage_provider.get_storage(Agent).get("ag-1")).model.profile_id == "mp-1"


@pytest.mark.asyncio
async def test_an_update_that_switches_to_another_existing_profile_is_accepted(client, app) -> None:
    await _seed(app, profile_row("mp-1"))
    await _seed(app, profile_row("mp-2"))
    await _seed_agent_directly(app, agent_body("ag-1"))

    r = await client.put("/v1/agents/ag-1", json=agent_body("ag-1", profile_id="mp-2"))

    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_an_agent_whose_profile_was_deleted_can_keep_that_profile_on_an_edit(client, app) -> None:
    await _seed_agent_directly(app, agent_body("ag-1", profile_id="gone-profile"))
    edited = {**agent_body("ag-1", profile_id="gone-profile"), "description": "edited"}

    r = await client.put("/v1/agents/ag-1", json=edited)

    assert r.status_code == 200, r.text


# ---- the create check and /status ask the same question -------------------------------------------------------------------------


STATUS_CASES = {
    "healthy": ("mp-1", []),
    "missing profile": ("ghost-profile", []),
    "missing toolset": ("mp-1", ["ghost-ts__x"]),
    "built-in toolsets": ("mp-1", ["system__list_agents", "web__web_fetch"]),
    "legacy alias": ("mp-1", ["_system__list_agents", "_misc__sleep"]),
    "bare tool id": ("mp-1", ["bare-tool"]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(STATUS_CASES))
async def test_the_create_check_refuses_exactly_what_status_flags_for_a_profile_or_a_toolset(client, app, case: str) -> None:
    """For each shape of agent: store it directly, ask ``/status``, then try to create the same agent through the router. The router
    must accept it exactly when ``/status`` has no missing-ModelProfile / missing-Toolset issue, so the two cannot drift."""
    profile_id, tools = STATUS_CASES[case]
    await _seed(app, profile_row("mp-1"))
    await _seed_agent_directly(app, agent_body("probe", profile_id=profile_id, tools=tools))

    status = (await client.get("/v1/agents/probe/status")).json()
    dangling = [i for i in status["issues"] if i.startswith("ModelProfile") or i.startswith("Toolset")]
    created = await client.post("/v1/agents", json=agent_body("fresh", profile_id=profile_id, tools=tools))

    assert (created.status_code == 201) == (not dangling), (case, dangling, created.status_code, created.text)
