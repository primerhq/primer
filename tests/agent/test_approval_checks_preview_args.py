"""A policy's ``preview_args`` must name arguments of THE TOOL it gates (design note 01a11cd3-66b0, slice 1 condition (c)).

``ToolApprovalPolicy.preview_args`` is checked for shape by the model; whether each path names an argument of the tool needs the tool's schema, so it is the shared
pre-write check (``primer/agent/approval_checks.py``), the one the REST router and the system ``create_`` / ``update_tool_approval_policy`` tools both call. A path that
names nothing is a typo and a typo hides more than the operator meant, silently; a policy on a tool that cannot be found at write time cannot be checked, and the
operator is told so rather than trusted.

``find_tool_schema`` (``primer/agent/tool_schemas.py``) is how the checks reach a schema: through the provider registry for every toolset it holds (reserved or user-made),
and from the workspace tools' argument models for the ``workspace`` toolset, which no registry holds (they exist per workspace session).
"""

from __future__ import annotations

import asyncio

import pytest

from primer.agent.approval_checks import check_policy, check_preview_args
from primer.agent.tool_schemas import WORKSPACE_TOOL_ARGS, find_tool_schema
from primer.common.entity_checks import EntityCheckError
from primer.model.chat import Tool
from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy

SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "entity": {"type": "object", "properties": {"id": {"type": "string"}}}},
}


def _policy(**fields) -> ToolApprovalPolicy:
    base = dict(id="p-1", toolset_id="ts", tool_name="t", approval=RequiredApprovalConfig())
    base.update(fields)
    return ToolApprovalPolicy(**base)


class _Calls:
    """A ``tool_schema_of`` that records what it was asked and answers from a table."""

    def __init__(self, table: dict[tuple[str, str], dict | None]) -> None:
        self.table, self.asked = table, []

    async def __call__(self, toolset_id: str, tool_name: str):
        self.asked.append((toolset_id, tool_name))
        return self.table.get((toolset_id, tool_name))


# ---- the check -------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paths_that_name_arguments_of_the_tool_pass() -> None:
    schema_of = _Calls({("ts", "t"): SCHEMA})

    await check_preview_args(_policy(preview_args=["path", "entity.id"]), tool_schema_of=schema_of)

    assert schema_of.asked == [("ts", "t")]


@pytest.mark.asyncio
async def test_a_path_the_tool_lacks_is_refused_naming_the_path_the_tool_and_what_it_has() -> None:
    with pytest.raises(EntityCheckError) as raised:
        await check_preview_args(_policy(preview_args=["path", "nope", "entity.nope"]), tool_schema_of=_Calls({("ts", "t"): SCHEMA}))

    error = raised.value
    assert error.kind == "validation" and error.field == "preview_args"
    assert error.message == (
        "preview_args ['nope', 'entity.nope'] name no argument of the tool 't' of toolset 'ts' (top-level arguments: ['entity', 'path'])"
    )


@pytest.mark.asyncio
async def test_a_tool_that_cannot_be_found_cannot_have_its_paths_checked() -> None:
    with pytest.raises(EntityCheckError) as raised:
        await check_preview_args(_policy(preview_args=["path"]), tool_schema_of=_Calls({}))

    assert raised.value.kind == "validation" and raised.value.field == "preview_args"
    assert raised.value.message == (
        "tool 't' of toolset 'ts' is not in the catalogue right now, so preview_args cannot be checked against its arguments; set preview_args once the tool is reachable"
    )


@pytest.mark.parametrize("declared", [None, []])
@pytest.mark.asyncio
async def test_a_policy_that_names_no_path_is_never_checked_against_a_tool(declared) -> None:
    """No path, nothing to be wrong: and a policy on a tool that is not (yet) in the catalogue stays writable, as it always was."""
    schema_of = _Calls({})

    await check_preview_args(_policy(preview_args=declared), tool_schema_of=schema_of)

    assert schema_of.asked == []


@pytest.mark.asyncio
async def test_a_caller_with_paths_to_check_and_no_way_to_find_the_tool_is_an_error_not_a_pass() -> None:
    with pytest.raises(EntityCheckError) as raised:
        await check_preview_args(_policy(preview_args=["path"]), tool_schema_of=None)

    assert raised.value.field == "preview_args" and "cannot be checked" in raised.value.message


# ---- check_policy runs it last, after the router's own two ---------------------------------------------------------------------------------------------------


class _EmptyStorage:
    async def find(self, predicate, page):
        class _Page:
            items = []

        return _Page()


class _StorageProvider:
    def get_storage(self, model):
        return _EmptyStorage()


@pytest.mark.asyncio
async def test_check_policy_runs_the_preview_check_after_uniqueness_and_the_config() -> None:
    with pytest.raises(EntityCheckError) as raised:
        await check_policy(
            _policy(preview_args=["nope"]), storage_provider=_StorageProvider(), tool_schema_of=_Calls({("ts", "t"): SCHEMA}),  # type: ignore[arg-type]
        )

    assert raised.value.field == "preview_args"


@pytest.mark.asyncio
async def test_check_policy_without_preview_args_needs_no_resolver() -> None:
    await check_policy(_policy(), storage_provider=_StorageProvider())  # type: ignore[arg-type]


# ---- how a schema is found --------------------------------------------------------------------------------------------------------------------------------


class _Provider:
    def __init__(self, *tools: Tool, delay: float = 0.0, fail: bool = False) -> None:
        self._tools, self._delay, self._fail = tools, delay, fail

    async def list_tools(self, *, principal=None):
        if self._fail:
            raise RuntimeError("the MCP server is unreachable")
        await asyncio.sleep(self._delay)
        for tool in self._tools:
            yield tool


class _Registry:
    def __init__(self, providers: dict[str, _Provider]) -> None:
        self._providers = providers

    async def get_toolset(self, toolset_id: str):
        if toolset_id not in self._providers:
            raise KeyError(toolset_id)
        return self._providers[toolset_id]


def _tool(tool_id: str) -> Tool:
    return Tool(id=tool_id, toolset_id="ts", description="d", schema=SCHEMA)


@pytest.mark.asyncio
async def test_a_schema_is_found_through_the_registry() -> None:
    registry = _Registry({"ts": _Provider(_tool("a"), _tool("t"))})

    assert await find_tool_schema(registry, "ts", "t") == SCHEMA


@pytest.mark.asyncio
@pytest.mark.parametrize("toolset_id,tool_name", [("ts", "missing"), ("nope", "t")])
async def test_an_unknown_tool_or_toolset_has_no_schema(toolset_id: str, tool_name: str) -> None:
    assert await find_tool_schema(_Registry({"ts": _Provider(_tool("t"))}), toolset_id, tool_name) is None


@pytest.mark.asyncio
async def test_a_toolset_that_fails_to_list_has_no_schema_instead_of_failing_the_write() -> None:
    assert await find_tool_schema(_Registry({"ts": _Provider(fail=True)}), "ts", "t") is None


@pytest.mark.asyncio
async def test_a_toolset_that_is_slow_to_list_is_given_up_on(monkeypatch) -> None:
    import primer.agent.tool_schemas as module

    monkeypatch.setattr(module, "LIST_TIMEOUT_S", 0.05)

    assert await find_tool_schema(_Registry({"ts": _Provider(_tool("t"), delay=1.0)}), "ts", "t") is None


@pytest.mark.asyncio
async def test_the_workspace_toolset_resolves_from_the_argument_models_not_the_registry() -> None:
    schema = await find_tool_schema(_Registry({}), "workspace", "exec")

    assert schema is not None and "command" in schema["properties"]
    assert await find_tool_schema(_Registry({}), "workspace", "no_such_tool") is None


def test_the_workspace_table_names_every_local_workspace_tool_with_its_real_argument_model() -> None:
    """The table is the only copy of "which argument model belongs to which workspace tool id"; pinned against the classes so a new tool or a renamed model fails here."""
    from primer.workspace.local import tools as local

    classes = [local.Edit, local.Exec, local.Glob, local.Grep, local.Ls, local.Read, local.Write]
    assert sorted(WORKSPACE_TOOL_ARGS) == sorted(cls.id for cls in classes)
    for cls in classes:
        assert WORKSPACE_TOOL_ARGS[cls.id] is object.__new__(cls).parameters(), cls.id
