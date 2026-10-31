"""``system__reset_prime_instructions``: rewrite prime's agents.md with the shipped version (pivot plan P2, task 10).

The admin tool twin of ``POST /v1/setup/reset_prime_instructions``. It takes no arguments: the file is always ``agents.md`` in the
default workspace, and the bytes are always the platform's (``shipped_prime_agents_md``), never the caller's.

Who may call it:

* the run's role must be admin (``required_role="admin"``; the tool manager, ``system__call_tool`` and the MCP endpoint all enforce it);
* the caller's person must hold full on the default workspace (prime) (access-control P2 T6: a session's reach no longer decides this,
  and a call with no calling session is checked as the MCP token's user). Which workspace the calling session runs in is no part of
  the rule; a run with no recorded initiator names no person and holds no level.

It ships ungated (no default approval policy): it can write nothing but the platform's own text, which any run with full on prime
can already write over with ``workspaces::write_workspace_file``.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import pytest

from primer.api.registries import ProviderRegistry
from primer.bootstrap.prime import shipped_prime_agents_md
from primer.model.access import Access
from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.principal import Principal, PrincipalRef
from primer.model.workspace import Workspace
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext
from primer.mcp.server import current_actor
from primer.toolset.system import build_system_toolset

from tests.bootstrap.test_prime_agents_md import _Registry, _Ws, _row, local_prime  # noqa: F401 - local_prime is a fixture

TOOL = "reset_prime_instructions"
#: The token's user of this suite's MCP calls: the person the prime row below grants full to.
U1 = "u1"
PRIME_USER = PrincipalRef(type="user", id=U1, display=U1, role="user", source="local")


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
    """Prime, recorded, with agents.md edited by a person, owned by the suite's token user (access-control P2 T6)."""
    row = _row("prime")
    row.access = Access(owners=[U1])
    await fake_storage_provider.get_storage(Workspace).create(row)
    await fake_storage_provider.set_default_workspace_id("prime")
    return _Ws({"agents.md": b"mine", "notes.md": b"keep"})


@pytest.fixture(autouse=True)
def _mcp_actor():
    """The MCP endpoint's live actor for every no-calling-session call of this suite (as ``primer.api._app_mcp`` publishes it)."""
    token = current_actor.set(
        Principal(type="user", id=f"tok-{U1}", display=U1, role="user", source="local", user_id=U1)
    )
    try:
        yield
    finally:
        current_actor.reset(token)


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


def _ctx(*, session_id: str | None = "sess-1", workspace_id: str | None = "prime",
         initiated_by: PrincipalRef | None = None) -> ToolContext:
    return ToolContext(tool_call_id="tc-1", session_id=session_id, workspace_id=workspace_id, initiated_by=initiated_by)


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
    assert json.loads(result.output) == {
        "workspace_id": "prime", "path": "agents.md", "bytes": len(shipped_prime_agents_md()), "shadows": None,
    }
    assert ws.files["agents.md"] == shipped_prime_agents_md()
    assert ws.files["notes.md"] == b"keep" and ws.writes == ["agents.md"]


async def test_from_a_session_in_prime_it_rewrites_the_file(provider, ws):
    result = await _call(provider, _ctx(workspace_id="prime", initiated_by=PRIME_USER))
    assert not result.is_error, result.output
    assert ws.files["agents.md"] == shipped_prime_agents_md()


async def test_the_legacy_default_workspace_is_reset_under_its_own_id(fake_storage_provider):
    row = _row("primer")
    row.access = Access(owners=[U1])
    await fake_storage_provider.get_storage(Workspace).create(row)
    await fake_storage_provider.set_default_workspace_id("primer")
    ws = _Ws({"agents.md": b"mine"})
    registry = _Registry(ws)
    provider = build_system_toolset(
        storage_provider=fake_storage_provider, provider_registry=_provider_registry(fake_storage_provider), workspace_registry=registry,
    )
    result = await _call(provider, _ctx(workspace_id="primer", initiated_by=PRIME_USER))
    assert not result.is_error, result.output
    assert json.loads(result.output)["workspace_id"] == "primer" and registry.asked == ["primer"]
    assert ws.files["agents.md"] == shipped_prime_agents_md()


async def test_an_AGENTS_md_is_left_in_place_and_the_result_says_it_is_shadowed(provider, ws):
    """The reset writes agents.md and nothing else; an AGENTS.md beside it stays, is not read while agents.md exists, and the result
    names it in ``shadows``."""
    ws.files = {"AGENTS.md": b"theirs"}
    result = await _call(provider)
    assert not result.is_error, result.output
    assert ws.files == {"AGENTS.md": b"theirs", "agents.md": shipped_prime_agents_md()}
    assert json.loads(result.output)["shadows"] == "AGENTS.md"


async def test_an_AGENTS_md_the_backend_refuses_to_describe_still_counts_as_beside(provider, ws):
    """A link out of the root (or a socket) at ``AGENTS.md``: the backend answers ``file_info`` with a 400, but an entry is there."""
    real_info = ws.file_info

    async def _info(path):
        if path == "AGENTS.md":
            raise BadRequestError("'AGENTS.md' resolves outside the workspace root")
        return await real_info(path)

    ws.file_info = _info
    result = await _call(provider)
    assert not result.is_error, result.output
    assert json.loads(result.output)["shadows"] == "AGENTS.md"


# ---- access ---------------------------------------------------------------------------------------


async def test_a_run_without_full_on_prime_is_refused_and_writes_nothing(provider, ws, registry):
    """The session's workspace is no longer part of the rule (access-control P2 T6): a run with no recorded initiator names no person,
    and a person with no user id holds no level, so it is refused from any session and writes nothing."""
    result = await _call(provider, _ctx(workspace_id="ws-other"))
    assert result.is_error
    body = json.loads(result.output)
    assert body["type"] == "forbidden"
    assert "default workspace" in body["message"]
    assert ws.files["agents.md"] == b"mine" and ws.writes == [] and registry.asked == []


async def test_the_calling_persons_level_on_prime_decides_from_any_session(provider, ws):
    """A person who holds full on prime may call it from whatever workspace their session runs in; the same run without a recorded
    initiator is refused."""
    in_prime = await _call(provider, _ctx(session_id="sess-in", workspace_id="prime", initiated_by=PRIME_USER))
    assert not in_prime.is_error, in_prime.output
    ws.writes.clear()
    outside = await _call(provider, _ctx(session_id="sess-out", workspace_id="ws-other", initiated_by=PRIME_USER))
    assert not outside.is_error, outside.output
    refused = await _call(provider, _ctx(session_id="sess-out", workspace_id="ws-other"))
    assert refused.is_error and json.loads(refused.output)["type"] == "forbidden"


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


async def test_any_other_failure_of_the_write_is_unavailable_in_its_own_words(provider, ws):
    """Nothing escapes the handler: a runtime error from the backend (a symlink loop is a ``RuntimeError`` on a local workspace)."""

    async def _loop(path, content):
        raise RuntimeError("Symlink loop from '/ws/prime/agents.md'")

    ws.write_file = _loop
    result = await _call(provider)
    assert result.is_error
    assert json.loads(result.output) == {"type": "unavailable", "message": "Symlink loop from '/ws/prime/agents.md'"}


async def test_a_symlink_loop_on_a_real_workspace_is_unavailable(local_prime):
    sp, real_registry = local_prime
    # The ensure step stamps no access; grant the suite's token user full on the row it creates.
    row = await sp.get_storage(Workspace).get("prime")
    row.access = Access(owners=[U1])
    await sp.get_storage(Workspace).update(row)
    provider = build_system_toolset(storage_provider=sp, provider_registry=_provider_registry(sp), workspace_registry=real_registry)
    live = await real_registry.get_workspace("prime")
    os.symlink("agents.md", f"{live._root.resolve()}/agents.md")  # noqa: SLF001 - a link the platform has no API for
    result = await _call(provider)
    body = json.loads(result.output)
    assert result.is_error and body["type"] == "unavailable" and "loop" in body["message"].lower()


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

    # The real MCP endpoint publishes its live actor (``primer.api._app_mcp``); invoke_exposed does not, so the suite does.
    token = current_actor.set(actor)
    try:
        result = await invoke_exposed(scoped_id=f"system__{TOOL}", arguments={}, principal=actor.id, actor=actor, deps=deps)
    finally:
        current_actor.reset(token)

    if allowed:
        assert not result.is_error, result.output
        assert ws.files["agents.md"] == shipped_prime_agents_md()
    else:
        assert result.is_error and "requires the 'admin' role" in result.output
        assert ws.files["agents.md"] == b"mine" and ws.writes == []
