"""The tool catalog: every tool an agent can call, in one list (pivot P1, task 5).

``primer.toolset.tool_catalog`` is what the generated wiki reference pages (a later P1 task) and ``system__describe_tool`` read. It covers the nine
built-in toolsets, the seven session file tools (``workspace__*``, one per session and not a registry's) and the client tool (``client__*``, a
per-turn grant), and says for each tool who may run it, whether it pauses the turn, whether it needs a workspace, which approval gate ships on it and
whether ``call_tool`` can reach it. The two direct scopes are the ones ``call_tool`` cannot reach: the ``ProviderRegistry`` has no provider for them.

What is pinned here: the session tools are the very descriptors the tool manager shows a model; the shipped gate is what the seed writes; the whole
catalog is sorted and unique; a toolset that cannot list its tools is skipped and logged without hiding the others; and the lookup is bounded, forwards
the run's principal, and lets an OAuth consent request through (it is the caller's to handle).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from primer.agent.tool_manager import _workspace_tool_descriptor
from primer.agent.tool_schemas import WORKSPACE_TOOL_ARGS
from primer.api.registries import ProviderRegistry
from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.bootstrap.seed import ensure_crud_approval_policies
from primer.model.chat import Tool
from primer.model.except_ import AuthRequiredError, NotFoundError, ProviderError
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalPolicy
from primer.toolset import tool_catalog
from primer.toolset.crud import CRUD_TOOL_NAMES, CRUD_TWINS, build_crud_toolset
from primer.toolset.misc import build_misc_toolset
from primer.toolset.tool_catalog import (
    DIRECT_CALL_TOOLSETS,
    ToolEntry,
    client_tool_descriptors,
    collect_tool_entries,
    default_gate,
    find_tool_entry,
    list_toolset_entries,
    session_tool_descriptors,
)
from primer.workspace.local import tools as local_tools
from tests.docs._live_toolsets import built_in_providers
from tests.toolset._scripted_toolset import Registry, Scripted, scripted_tool

@pytest.fixture
def registry(fake_storage_provider) -> ProviderRegistry:
    """A real registry with the crud and misc toolsets wired, as the app's lifespan does; any other id falls to the (empty) Toolset storage."""
    return ProviderRegistry(
        fake_storage_provider,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        toolset_factory=lambda t: object(),  # type: ignore[arg-type,return-value]
        misc_toolset_provider=build_misc_toolset(),
        crud_toolset_provider=build_crud_toolset(storage_provider=fake_storage_provider),  # type: ignore[arg-type]
    )


# ---- the session and client scopes ---------------------------------------------------------------------------------------------------------------------


def test_the_session_tools_are_the_seven_workspace_tools():
    tools = session_tool_descriptors()
    assert sorted(t.id for t in tools) == ["edit", "exec", "glob", "grep", "ls", "read", "write"]
    assert all(t.toolset_id == "workspace" and t.required_role == "user" and t.requires_workspace for t in tools)
    assert {t.id: t.args_schema for t in tools} == {k: m.model_json_schema() for k, m in WORKSPACE_TOOL_ARGS.items()}


def test_the_session_descriptors_are_what_the_tool_manager_shows_the_model():
    """The catalog and the model must read one description: the manager builds a session tool's descriptor from the tool class, and so does the catalog."""
    classes = [local_tools.Edit, local_tools.Exec, local_tools.Glob, local_tools.Grep, local_tools.Ls, local_tools.Read, local_tools.Write]
    shown = {cls.id: _workspace_tool_descriptor(object.__new__(cls), scoped_id=cls.id) for cls in classes}
    ours = {t.id: t for t in session_tool_descriptors()}
    assert set(ours) == set(shown)
    for tool_id, manager_view in shown.items():
        mine = ours[tool_id]
        assert (mine.description, mine.args_schema, mine.examples, mine.interruptible, mine.preview_args) == (
            manager_view.description, manager_view.args_schema, manager_view.examples, manager_view.interruptible, manager_view.preview_args,
        ), tool_id
    # The two file mutators keep their Stop declaration: a Stop waits for them instead of cancelling a write half done.
    assert {t_id for t_id, t in ours.items() if not t.interruptible} == {"edit", "write"}


async def test_the_client_tool_is_the_client_toolsets_vocabulary():
    tools = await client_tool_descriptors()
    assert [t.id for t in tools] == ["open_file"]
    assert tools[0].toolset_id == "client" and tools[0].required_role == "user"


# ---- the shipped gate ----------------------------------------------------------------------------------------------------------------------------------


def test_the_crud_tools_and_their_twins_are_gated_by_default_and_nothing_else_is():
    """Audit G6-1: the system and trigger tools that make the crud tools' writes (and delete agents, graphs and triggers) ship gated too; this test
    pinned the old shipped set, in which ``system::create_agent`` was the ungated twin of ``crud::create_agent``."""
    assert default_gate("crud", "create_agent") == "required"
    assert default_gate("system", "call_tool") is None and default_gate("workspace", "write") is None
    assert {name for name in CRUD_TOOL_NAMES if default_gate("crud", name) == "required"} == set(CRUD_TOOL_NAMES)
    assert default_gate("crud", "not_a_crud_tool") is None
    assert {key for key in CRUD_TWINS if default_gate(*key) == "required"} == set(CRUD_TWINS)
    assert default_gate("system", "create_agent") == "required" and default_gate("trigger", "delete") == "required"
    assert default_gate("system", "create_collection") is None and default_gate("trigger", "create_subscription") is None


async def test_the_default_gate_is_what_the_seed_gates(fake_storage_provider):
    """``default_gate`` is a statement about ``ensure_crud_approval_policies``; this fails when either moves without the other."""
    await ensure_crud_approval_policies(fake_storage_provider)
    seeded = (await fake_storage_provider.get_storage(ToolApprovalPolicy).list(OffsetPage(offset=0, length=200))).items
    assert {p.approval.type.value for p in seeded} == {"required", "policy"}

    entries = await collect_tool_entries(built_in_providers())

    assert {(e.toolset_id, e.tool.id, e.default_gate) for e in entries if e.default_gate} == {
        (p.toolset_id, p.tool_name, p.approval.type.value) for p in seeded
    }
    assert {(t, n) for t, n, gate in ((e.toolset_id, e.tool.id, e.default_gate) for e in entries) if gate == "policy"} == {
        ("system", "create_toolset"), ("system", "update_toolset"),
    }


# ---- the whole catalog ---------------------------------------------------------------------------------------------------------------------------------


async def test_the_catalog_covers_every_builtin_toolset_and_both_direct_scopes():
    assert set(built_in_providers()) == set(RESERVED_TOOLSET_IDS), "the helper must build all nine, collections included"
    entries = await collect_tool_entries(built_in_providers())
    assert {e.toolset_id for e in entries} == set(RESERVED_TOOLSET_IDS) | {"workspace", "client"}
    assert [e.scoped_id for e in entries] == sorted({e.scoped_id for e in entries})   # sorted, unique
    assert {e.toolset_id for e in entries if not e.via_call_tool} == {"workspace", "client"} == set(DIRECT_CALL_TOOLSETS)
    assert next(e for e in entries if e.scoped_id == "system__call_tool").pauses_turn is True


async def test_an_entry_carries_its_providers_declarations():
    by_id = {e.scoped_id: e for e in await collect_tool_entries(built_in_providers())}

    waits = by_id["system__wait_for_event"]
    assert (waits.pauses_turn, waits.needs_workspace) == (True, True)
    assert by_id["workspace_ext__sleep"].pauses_turn is True and by_id["workspace_ext__sleep"].required_role == "user"
    plain = by_id["misc__get_datetime"]
    assert (plain.required_role, plain.pauses_turn, plain.needs_workspace, plain.default_gate, plain.via_call_tool) == ("user", False, False, None, True)
    assert by_id["system__get_llm_provider"].required_role == "admin"
    assert by_id["crud__create_agent"].default_gate == "required"
    write = by_id["workspace__write"]
    assert (write.required_role, write.needs_workspace, write.pauses_turn, write.default_gate, write.via_call_tool) == ("user", True, False, None, False)
    assert by_id["client__open_file"].via_call_tool is False
    assert isinstance(write, ToolEntry) and write.scoped_id == "workspace__write"


async def test_a_toolset_that_fails_to_list_is_skipped(caplog):
    class _Raising(Scripted):
        async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
            raise RuntimeError("cannot list")
            yield  # pragma: no cover - makes this an async generator

    with caplog.at_level(logging.WARNING, logger=tool_catalog.logger.name):
        entries = await collect_tool_entries({"misc": build_misc_toolset(), "boom": _Raising([])})
    assert {e.toolset_id for e in entries} == {"misc", "workspace", "client"}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "boom" in r.getMessage()]
    assert len(warnings) == 1, "one WARNING, naming the toolset"


async def test_a_toolset_that_fails_halfway_contributes_nothing(caplog):
    broken = Scripted([scripted_tool("a"), scripted_tool("b")], fail_after=1)
    with caplog.at_level(logging.WARNING, logger=tool_catalog.logger.name):
        entries = await collect_tool_entries({"rec": broken, "misc": build_misc_toolset()})
    assert "rec" not in {e.toolset_id for e in entries}, "half a toolset would document tools the next build then drops"
    assert any("rec" in r.getMessage() for r in caplog.records)


async def test_the_roles_and_pauses_come_from_the_provider_and_the_descriptor():
    provider = Scripted([scripted_tool("plain"), scripted_tool("waits"), scripted_tool("flagged", yields=True)], roles={"plain": "user"}, yielding=frozenset({"waits"}))
    by_id = {e.tool.id: e for e in await collect_tool_entries({"rec": provider})}
    assert by_id["plain"].required_role == "user" and by_id["waits"].required_role == "admin", "an undeclared tool fails closed, as call_tool ranks it"
    assert (by_id["plain"].pauses_turn, by_id["waits"].pauses_turn, by_id["flagged"].pauses_turn) == (False, True, True)


# ---- the lookup ----------------------------------------------------------------------------------------------------------------------------------------


async def test_find_tool_entry_reaches_every_scope(registry):
    assert (await find_tool_entry(registry, "workspace", "read")).required_role == "user"
    assert (await find_tool_entry(registry, "crud", "create_agent")).default_gate == "required"
    assert await find_tool_entry(registry, "crud", "nope") is None
    assert await find_tool_entry(registry, "nope", "x") is None


async def test_find_tool_entry_knows_the_direct_scopes_without_a_registry():
    """``workspace`` and ``client`` are no registry's: a registry that knows nothing of them still answers."""
    read = await find_tool_entry(Registry({}), "workspace", "read")
    assert read is not None and (read.scoped_id, read.via_call_tool, read.needs_workspace) == ("workspace__read", False, True)
    opened = await find_tool_entry(Registry({}), "client", "open_file")
    assert opened is not None and (opened.scoped_id, opened.via_call_tool, opened.pauses_turn) == ("client__open_file", False, False)
    assert await find_tool_entry(Registry({}), "workspace", "nope") is None


async def test_find_tool_entry_ranks_a_tool_by_its_providers_role_and_pause():
    provider = Scripted([scripted_tool("go"), scripted_tool("wait")], roles={"go": "user"}, yielding=frozenset({"wait"}))
    go = await find_tool_entry(Registry({"rec": provider}), "rec", "go")
    wait = await find_tool_entry(Registry({"rec": provider}), "rec", "wait")
    assert (go.scoped_id, go.required_role, go.pauses_turn, go.via_call_tool) == ("rec__go", "user", False, True)
    assert (wait.required_role, wait.pauses_turn) == ("admin", True)


async def test_find_tool_entry_forwards_the_principal_to_the_listing():
    provider = Scripted([scripted_tool("go")])
    await find_tool_entry(Registry({"rec": provider}), "rec", "go", principal="alice")
    await find_tool_entry(Registry({"rec": provider}), "rec", "go")
    assert provider.listed_as == ["alice", None]


async def test_find_tool_entry_gives_none_for_a_toolset_that_will_not_list(caplog):
    with caplog.at_level(logging.WARNING, logger=tool_catalog.logger.name):
        entry = await find_tool_entry(Registry({"rec": Scripted([scripted_tool("go")], fail_after=0)}), "rec", "go")
    assert entry is None
    assert any("rec" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_find_tool_entry_lets_an_oauth_consent_request_through():
    """``AuthRequiredError`` is for the caller to handle (the consent URL must reach the person), so it is not folded into "not found"."""
    with pytest.raises(AuthRequiredError):
        await find_tool_entry(Registry({"rec": Scripted([scripted_tool("go")], auth=True)}), "rec", "go")


async def test_find_tool_entry_does_not_wait_forever_for_a_toolset(monkeypatch):
    monkeypatch.setattr(tool_catalog, "LIST_TIMEOUT_S", 0.05)
    assert await asyncio.wait_for(find_tool_entry(Registry({"rec": Scripted([scripted_tool("go")], block=True)}), "rec", "go"), timeout=5) is None


# ---- one toolset's entries -----------------------------------------------------------------------------------------------------------------------------


async def test_list_toolset_entries_gives_every_tool_of_one_toolset(registry):
    entries = await list_toolset_entries(registry, "misc")
    assert {e.toolset_id for e in entries} == {"misc"} and "get_datetime" in {e.tool.id for e in entries}
    assert {e.tool.id for e in await list_toolset_entries(registry, "workspace")} == set(WORKSPACE_TOOL_ARGS)
    assert [e.tool.id for e in await list_toolset_entries(registry, "client")] == ["open_file"]


async def test_list_toolset_entries_says_why_it_cannot():
    with pytest.raises(NotFoundError):
        await list_toolset_entries(Registry({}), "nope")
    with pytest.raises(ProviderError, match="rec"):
        await list_toolset_entries(Registry({"rec": Scripted([scripted_tool("go")], fail_after=0)}), "rec")
    with pytest.raises(AuthRequiredError):
        await list_toolset_entries(Registry({"rec": Scripted([scripted_tool("go")], auth=True)}), "rec")


async def test_a_not_found_from_the_listing_is_not_an_unknown_toolset():
    """``NotFoundError`` means "no such toolset" to the callers, so the one a toolset's own listing raises must not look like it."""
    gone = Scripted([scripted_tool("go")], raises=NotFoundError("the upstream server has no such resource"))
    with pytest.raises(ProviderError, match="upstream server"):
        await list_toolset_entries(Registry({"rec": gone}), "rec")
    assert await find_tool_entry(Registry({"rec": gone}), "rec", "go") is None


async def test_list_toolset_entries_times_out_as_a_provider_error(monkeypatch):
    monkeypatch.setattr(tool_catalog, "LIST_TIMEOUT_S", 0.05)
    with pytest.raises(ProviderError, match="rec"):
        await asyncio.wait_for(list_toolset_entries(Registry({"rec": Scripted([scripted_tool("go")], block=True)}), "rec"), timeout=5)
