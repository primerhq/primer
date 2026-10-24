"""``system__reset_prime_instructions``: rewrite prime's agents.md with the shipped version (pivot plan P2, task 10).

The admin tool twin of ``POST /v1/setup/reset_prime_instructions``. It takes no arguments: the file is always ``agents.md`` in the
default workspace, and the bytes are always the platform's (``shipped_prime_agents_md``), never the caller's.

Who may call it:

* the run's role must be admin (``required_role="admin"``; the tool manager, ``system__call_tool`` and the MCP endpoint all enforce it);
* a session must be IN the default workspace: the reach rule of #870/#874 (``primer.session.peer_messages.reach_of``) lets a session
  outside prime reach only its own workspace, and this tool writes into prime, so from any other workspace it is refused and writes
  nothing. A call with no calling session (a person over MCP) reaches every workspace, as with every workspace tool.

It ships ungated (no default approval policy): it can write nothing but the platform's own text, which any run in prime, at any role,
can already write over with ``workspaces::write_workspace_file``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from primer.api.registries import ProviderRegistry
from primer.bootstrap.prime import shipped_prime_agents_md
from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.principal import Principal
from primer.model.workspace import Workspace
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext
from primer.toolset.system import build_system_toolset

from tests.bootstrap.test_prime_agents_md import _Registry, _Ws, _row

TOOL = "reset_prime_instructions"


def _provider_registry(sp) -> ProviderRegistry:
    return ProviderRegistry(
        sp,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )


@pytest.fixture
async def ws(fake_storage_provider) -> _Ws:
    """Prime, recorded, with agents.md edited by a person."""
    await fake_storage_provider.get_storage(Workspace).create(_row("prime"))
    await fake_storage_provider.set_default_workspace_id("prime")
    return _Ws({"agents.md": b"mine", "notes.md": b"keep"})


@pytest.fixture
def registry(ws) -> _Registry:
    return _Registry(ws)


@pytest.fixture
def provider(fake_storage_provider, registry):
    return build_system_toolset(
        storage_provider=fake_storage_provider,
        provider_registry=_provider_registry(fake_storage_provider),
        workspace_registry=registry,
    )


async def _tool(provider):
    async for tool in provider.list_tools():
        if tool.id == TOOL:
            return tool
    raise AssertionError(f"{TOOL} is not listed")


def _ctx(*, session_id: str | None = "sess-1", workspace_id: str | None = "prime") -> ToolContext:
    return ToolContext(tool_call_id="tc-1", session_id=session_id, workspace_id=workspace_id)


async def _call(provider, ctx=None, arguments=None):
    return await provider.call(tool_name=TOOL, arguments=arguments if arguments is not None else {}, ctx=ctx)


# ---- the declaration ----------------------------------------------------------------------------------------------------------------


async def test_the_tool_is_admin_only(provider):
    """Declared, not the provider's fail-closed default for a name it does not know (which is also ``admin``)."""
    assert (await _tool(provider)).required_role == "admin"
    assert provider.required_role(TOOL) == "admin"


async def test_the_tool_takes_no_arguments_and_works_outside_a_workspace(provider):
    """No argument names a workspace or a text; and it is not workspace-only, so MCP and a session anywhere can see it."""
    tool = await _tool(provider)
    assert tool.args_schema.get("properties") == {}
    assert tool.args_schema.get("additionalProperties") is False
    assert tool.requires_workspace is False and tool.yields is False


async def test_a_stop_does_not_cancel_the_write(provider):
    """The write is ``Workspace.write_file``, which a local workspace runs in a thread under its scope lock; cancelling the await would
    release the lock while the write still runs (the reason ``write_workspace_file`` is not interruptible either)."""
    assert (await _tool(provider)).interruptible is False


async def test_the_tool_ships_ungated():
    from primer.toolset.crud import DEFAULT_GATED_TOOLS, shipped_gate

    assert ("system", TOOL) not in DEFAULT_GATED_TOOLS
    assert shipped_gate("system", TOOL) is None


async def test_the_tool_is_absent_without_a_workspace_registry(fake_storage_provider):
    provider = build_system_toolset(
        storage_provider=fake_storage_provider, provider_registry=_provider_registry(fake_storage_provider), workspace_registry=None,
    )
    names = [tool.id async for tool in provider.list_tools()]
    assert TOOL not in names


# ---- what it does -------------------------------------------------------------------------------------------------------------------


async def test_the_tool_rewrites_the_file(provider, ws):
    """With no calling session (the MCP endpoint hands handlers no context)."""
    result = await _call(provider)
    assert not result.is_error, result.output
    assert json.loads(result.output) == {"workspace_id": "prime", "path": "agents.md", "bytes": len(shipped_prime_agents_md())}
    assert ws.files["agents.md"] == shipped_prime_agents_md()
    assert ws.files["notes.md"] == b"keep" and ws.writes == ["agents.md"]


async def test_from_a_session_in_prime_it_rewrites_the_file(provider, ws):
    result = await _call(provider, _ctx(workspace_id="prime"))
    assert not result.is_error, result.output
    assert ws.files["agents.md"] == shipped_prime_agents_md()


async def test_the_legacy_default_workspace_is_reset_under_its_own_id(fake_storage_provider):
    await fake_storage_provider.get_storage(Workspace).create(_row("primer"))
    await fake_storage_provider.set_default_workspace_id("primer")
    ws = _Ws({"agents.md": b"mine"})
    registry = _Registry(ws)
    provider = build_system_toolset(
        storage_provider=fake_storage_provider, provider_registry=_provider_registry(fake_storage_provider), workspace_registry=registry,
    )
    result = await _call(provider, _ctx(workspace_id="primer"))
    assert not result.is_error, result.output
    assert json.loads(result.output)["workspace_id"] == "primer" and registry.asked == ["primer"]
    assert ws.files["agents.md"] == shipped_prime_agents_md()


async def test_an_AGENTS_md_is_left_in_place(provider, ws):
    """The reset writes agents.md and nothing else; an AGENTS.md beside it stays (and is not read while agents.md exists)."""
    ws.files = {"AGENTS.md": b"theirs"}
    result = await _call(provider)
    assert not result.is_error, result.output
    assert ws.files == {"AGENTS.md": b"theirs", "agents.md": shipped_prime_agents_md()}


# ---- reach ------------------------------------------------------------------------------------------------------------------------


async def test_from_a_session_outside_prime_it_is_refused_and_writes_nothing(provider, ws, registry):
    result = await _call(provider, _ctx(workspace_id="ws-other"))
    assert result.is_error
    body = json.loads(result.output)
    assert body["type"] == "forbidden"
    assert "default workspace" in body["message"]
    assert ws.files["agents.md"] == b"mine" and ws.writes == [] and registry.asked == []


async def test_a_session_found_by_its_row_is_judged_by_the_row_s_workspace(provider, ws, fake_storage_provider):
    """A context that names the session but not its workspace: the session row says where it runs, as the workspace tools read it."""
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    for session_id, workspace_id in (("sess-in", "prime"), ("sess-out", "ws-other")):
        await sessions.create(WorkspaceSession(
            id=session_id, workspace_id=workspace_id, binding=AgentSessionBinding(agent_id="operator"),
            status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc),
        ))

    refused = await _call(provider, _ctx(session_id="sess-out", workspace_id=None))
    assert refused.is_error and json.loads(refused.output)["type"] == "forbidden"
    assert ws.writes == []

    allowed = await _call(provider, _ctx(session_id="sess-in", workspace_id=None))
    assert not allowed.is_error, allowed.output
    assert ws.files["agents.md"] == shipped_prime_agents_md()


async def test_a_session_with_no_known_workspace_is_refused(provider, ws):
    result = await _call(provider, _ctx(session_id="sess-gone", workspace_id=None))
    assert result.is_error and json.loads(result.output)["type"] == "forbidden"
    assert ws.writes == []


# ---- failures -----------------------------------------------------------------------------------------------------------------------


async def test_no_default_workspace_is_not_found(fake_storage_provider):
    registry = _Registry(_Ws())
    provider = build_system_toolset(
        storage_provider=fake_storage_provider, provider_registry=_provider_registry(fake_storage_provider), workspace_registry=registry,
    )
    result = await _call(provider)
    assert result.is_error
    assert json.loads(result.output) == {"type": "not-found", "message": "no default workspace is recorded"}
    assert registry.asked == []


async def test_a_workspace_the_backend_cannot_find_is_not_found(provider, registry):
    async def _gone(workspace_id):
        raise NotFoundError(f"Workspace {workspace_id!r} does not exist")

    registry.get_workspace = _gone
    result = await _call(provider)
    assert result.is_error and json.loads(result.output)["type"] == "not-found"


async def test_a_directory_in_the_way_is_a_bad_request(provider, ws):
    async def _refuse(path, content):
        raise BadRequestError(f"{path!r} is a directory; cannot overwrite with file content")

    ws.write_file = _refuse
    result = await _call(provider)
    assert result.is_error and json.loads(result.output)["type"] == "bad-request"


async def test_a_reattach_that_hangs_is_unavailable_and_writes_nothing(provider, ws, registry, monkeypatch):
    """Reaching the workspace (and the AGENTS.md check) is bounded as in the seed: past the bound the tool answers type=unavailable."""
    from primer.bootstrap import prime as prime_module

    monkeypatch.setattr(prime_module, "PRIME_AGENTS_MD_CHECK_TIMEOUT_S", 0.05)

    async def _hung(workspace_id):
        await asyncio.Event().wait()

    registry.get_workspace = _hung
    async with asyncio.timeout(5):
        result = await _call(provider)
    assert result.is_error and json.loads(result.output)["type"] == "unavailable"
    assert ws.writes == []


async def test_a_write_that_hangs_is_unavailable(provider, ws, monkeypatch):
    from primer.bootstrap import prime as prime_module

    monkeypatch.setattr(prime_module, "PRIME_AGENTS_MD_CHECK_TIMEOUT_S", 0.05)

    async def _hung(path, content):
        await asyncio.Event().wait()

    ws.write_file = _hung
    async with asyncio.timeout(5):
        result = await _call(provider)
    body = json.loads(result.output)
    assert result.is_error and body["type"] == "unavailable"
    assert "may still" in body["message"]


async def test_an_argument_is_refused(provider, ws):
    """Nothing can name another workspace or a text: an argument is a validation error, not silently ignored."""
    result = await _call(provider, arguments={"workspace_id": "ws-other"})
    assert result.is_error and json.loads(result.output)["type"] == "validation-error"
    assert ws.writes == []


# ---- over MCP -----------------------------------------------------------------------------------------------------------------------


class _OneToolsetRegistry:
    def __init__(self, provider) -> None:
        self._provider = provider

    async def get_toolset(self, toolset_id: str):
        return self._provider if toolset_id == "system" else None


@pytest.mark.parametrize(("role", "allowed"), [("admin", True), ("user", False)])
async def test_over_mcp_only_an_admin_resets(fake_storage_provider, provider, ws, role, allowed):
    from primer.mcp.dispatch import invoke_exposed
    from primer.mcp.exposure import ExposureDeps, update_exposure

    deps = ExposureDeps(storage_provider=fake_storage_provider, provider_registry=_OneToolsetRegistry(provider))
    await update_exposure(enabled=True, allowed_tools=[f"system__{TOOL}"], updated_by="admin", deps=deps)
    actor = Principal(type="user", id=f"u-{role}", display=role, role=role, source="local")

    result = await invoke_exposed(scoped_id=f"system__{TOOL}", arguments={}, principal=actor.id, actor=actor, deps=deps)

    if allowed:
        assert not result.is_error, result.output
        assert ws.files["agents.md"] == shipped_prime_agents_md()
    else:
        assert result.is_error and "requires the 'admin' role" in result.output
        assert ws.files["agents.md"] == b"mine" and ws.writes == []
