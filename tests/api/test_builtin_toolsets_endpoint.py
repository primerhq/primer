"""HTTP-surface tests for GET /v1/toolsets/builtin."""

from __future__ import annotations

import pytest

from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.api.routers import providers

# The built-in toolsets an agent can bind, in catalogue order (platform revamp, design section 11; pivot P5 adds the reserved canvas toolset, design 8.3).
BUILTIN_IDS = ["system", "workspaces", "collections", "misc", "web", "harness", "trigger", "workspace_ext", "crud", "canvas"]


@pytest.mark.asyncio
async def test_builtin_toolsets_each_item_has_required_fields(client):
    r = await client.get("/v1/toolsets/builtin")
    items = r.json()["items"]
    for it in items:
        assert isinstance(it["id"], str) and it["id"]
        assert isinstance(it["tagline"], str) and it["tagline"]
        assert isinstance(it["icon"], str) and it["icon"]
        assert isinstance(it["always_on"], bool)
        assert isinstance(it["available"], bool)


@pytest.mark.asyncio
async def test_builtin_toolsets_route_not_shadowed_by_crud_get_by_id(client):
    # Defence against a regression where someone refactors and breaks
    # the route-registration order: /toolsets/builtin must NOT be
    # interpreted as toolset_id=builtin by the CRUD router (which
    # would return 404 because no such row exists).
    r = await client.get("/v1/toolsets/builtin")
    assert r.status_code == 200, r.text
    # The response shape is the builtin list, not a single Toolset row.
    body = r.json()
    assert "items" in body
    assert "config" not in body  # not the CRUD Toolset row shape


# ----------------------------------------------------------------------------
# GET /v1/tools — the merged per-tool catalogue powering the agent picker
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_tools_returns_one_entry_per_toolset(client):
    """The fan-out endpoint includes every built-in toolset regardless
    of availability so the UI can render the grouped picker without a
    second round-trip."""
    r = await client.get("/v1/tools")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [it["id"] for it in body["items"]]
    # Every built-in the catalogue lists appears.
    for required in BUILTIN_IDS:
        assert required in ids, f"missing built-in {required!r} in {ids!r}"


@pytest.mark.asyncio
async def test_all_tools_emits_scoped_ids(client):
    """Every tool in the catalogue carries a scoped_id of the form
    ``<toolset_id>__<tool_name>`` — the agent picker's allowlist
    uses scoped ids directly so a typo here breaks the round-trip."""
    r = await client.get("/v1/tools")
    body = r.json()
    for ts in body["items"]:
        if not ts.get("available"):
            continue
        for tool in ts["tools"]:
            assert tool["scoped_id"] == f"{ts['id']}__{tool['id']}", (
                f"bad scoped_id for {ts['id']}.{tool['id']}: {tool['scoped_id']!r}"
            )


@pytest.mark.asyncio
async def test_all_tools_carries_capability_badges(client):
    """The y/w/r/n capability badges (primer.model.chat.Tool.yields /
    .requires_workspace / .tool_class / .required_role) surface here too,
    not just on the flat /tools/catalogue -- both share ``_catalogue_tools``."""
    r = await client.get("/v1/tools")
    body = r.json()
    by_id = {it["id"]: it for it in body["items"]}
    system_tools = {t["id"]: t for t in by_id["system"]["tools"]}

    ask_user = system_tools["ask_user"]
    assert ask_user["yields"] is True
    assert ask_user["requires_workspace"] is False
    assert ask_user["tool_class"] in ("standard", "notifying")
    assert ask_user["required_role"] is None or isinstance(
        ask_user["required_role"], str
    )

    wait_for_event = system_tools["wait_for_event"]
    assert wait_for_event["yields"] is True
    assert wait_for_event["requires_workspace"] is True


@pytest.mark.asyncio
async def test_all_tools_carries_the_interruptible_flag(client):
    """Stop slice B1: the fifth capability flag, ``interruptible``, is served wherever ``yields`` is (the same
    ``tool_catalogue_flags`` seam). A tool a Stop must not cancel says ``false``; everything else ``true``."""
    r = await client.get("/v1/tools")
    by_id = {it["id"]: it for it in r.json()["items"]}
    system_tools = {t["id"]: t for t in by_id["system"]["tools"]}
    workspace_tools = {t["id"]: t for t in by_id["workspaces"]["tools"]}

    assert system_tools["put_document"]["interruptible"] is False
    assert workspace_tools["write_workspace_file"]["interruptible"] is False
    assert workspace_tools["delete_workspace_file"]["interruptible"] is False
    assert workspace_tools["read_workspace_file"]["interruptible"] is True
    assert system_tools["ask_user"]["interruptible"] is True


@pytest.mark.asyncio
async def test_builtin_toolsets_lists_every_toolset_an_agent_can_bind(client):
    r = await client.get("/v1/toolsets/builtin")
    assert r.status_code == 200, r.text
    assert [it["id"] for it in r.json()["items"]] == BUILTIN_IDS


def test_the_catalogue_is_exactly_the_reserved_toolset_ids():
    ids = [spec["id"] for spec in providers._BUILTIN_TOOLSETS]
    assert len(ids) == len(set(ids)) == 10
    assert set(ids) == RESERVED_TOOLSET_IDS


@pytest.mark.asyncio
async def test_every_catalogue_id_resolves_through_get_toolset_without_storage(fake_storage_provider):
    from primer.api.registries import ProviderRegistry
    from primer.model.except_ import NotFoundError

    built_in = {spec["id"]: object() for spec in providers._BUILTIN_TOOLSETS}
    registry = ProviderRegistry(
        fake_storage_provider,
        llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
        **{f"{tid}_toolset_provider": provider for tid, provider in built_in.items()},
    )
    for tid, provider in built_in.items():
        assert await registry.get_toolset(tid) is provider, tid
    with pytest.raises(NotFoundError):
        await registry.get_toolset("search")


@pytest.mark.asyncio
async def test_no_route_lists_a_search_toolset(client):
    builtin = [it["id"] for it in (await client.get("/v1/toolsets/builtin")).json()["items"]]
    grouped = [it["id"] for it in (await client.get("/v1/tools")).json()["items"]]
    flat = {item["id"].split("__", 1)[0] for item in (await client.get("/v1/tools/catalogue")).json()["items"]}
    assert "search" not in builtin
    assert "search" not in grouped
    assert "search" not in flat


@pytest.mark.asyncio
async def test_every_built_in_toolset_is_always_on_and_available(client):
    items = (await client.get("/v1/toolsets/builtin")).json()["items"]
    assert all(it["always_on"] is True and it["available"] is True for it in items), items


@pytest.mark.asyncio
async def test_every_catalogue_id_opens_its_detail(client):
    for it in (await client.get("/v1/toolsets/builtin")).json()["items"]:
        r = await client.get(f"/v1/toolsets/{it['id']}")
        assert r.status_code == 200, (it["id"], r.text)
        body = r.json()
        assert body["builtin"] is True, body
        assert body["description"] == it["tagline"], body


def test_every_toolset_a_seeded_agent_binds_is_in_the_catalogue():
    from primer.agent.agent_checks import toolset_id_of
    from primer.bootstrap.operator_defaults import (
        BUILDER_TOOLS, EXPLORER_TOOLS, OPERATOR_TOOLS, PLANNER_TOOLS, TOOL_RUNNER_TOOLS,
    )

    catalogue = {spec["id"] for spec in providers._BUILTIN_TOOLSETS}
    seeded = (OPERATOR_TOOLS, BUILDER_TOOLS, PLANNER_TOOLS, EXPLORER_TOOLS, TOOL_RUNNER_TOOLS)
    bound = {toolset_id_of(tool) for tools in seeded for tool in tools}
    assert bound <= catalogue, sorted(bound - catalogue)
