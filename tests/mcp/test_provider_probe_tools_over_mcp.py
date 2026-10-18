"""The provider probe tools are reachable over the MCP mount, and only by an admin (revamp plan 02, tasks 3 and 4).

``check_huggingface_models``, ``test_embedding_provider`` and ``test_cross_encoder_provider`` are new tools of the reserved ``system`` toolset. The definition of done for a
new tool is that it is callable over ``POST /v1/mcp``: an operator can expose it (``is_exposable`` accepts it: not yielding, no workspace needed), an MCP client lists and calls
it, and the dispatch gate holds the caller to the tool's declared ``admin`` role before the handler runs. These tests drive the real system toolset through
``primer.mcp``'s server and dispatch with the Hub mocked by ``respx``; nothing reaches the real Hugging Face Hub.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.lowlevel import Server

from primer.api.registries import ProviderRegistry
from primer.mcp.dispatch import invoke_exposed
from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.server import build_mcp_server, current_actor
from primer.model.principal import Principal
from primer.toolset.system import build_system_toolset

HUB = "https://hub.test"
MODEL = "BAAI/bge-small-en-v1.5"
PROBE_TOOLS = ("check_huggingface_models", "test_embedding_provider", "test_cross_encoder_provider")
SCOPED = {name: f"system__{name}" for name in PROBE_TOOLS}
ARGUMENTS = {
    "check_huggingface_models": {"models": [MODEL]},
    "test_embedding_provider": {"provider": "huggingface", "models": [{"name": MODEL}]},
    "test_cross_encoder_provider": {"models": [{"name": MODEL}]},
}


@pytest.fixture(autouse=True)
def _hub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_ENDPOINT", HUB)


@pytest.fixture
def deps(fake_storage_provider) -> ExposureDeps:
    registry = ProviderRegistry(
        fake_storage_provider,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )
    registry._system_toolset_provider = build_system_toolset(  # noqa: SLF001
        storage_provider=fake_storage_provider,  # type: ignore[arg-type]
        provider_registry=registry,
    )
    return ExposureDeps(storage_provider=fake_storage_provider, provider_registry=registry)


@asynccontextmanager
async def _connected_client_session(server: Server) -> AsyncIterator[ClientSession]:
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _actor(role: str) -> Principal:
    return Principal(type="user", id=f"{role}-1", display=role, role=role, source="local")


@pytest.mark.asyncio
async def test_the_probe_tools_can_be_exposed_and_are_listed_with_their_schemas(deps: ExposureDeps) -> None:
    """``update_exposure`` refuses a tool that does not exist or cannot be exposed (a yielding one, one that needs a workspace); it accepts these."""
    await update_exposure(enabled=True, allowed_tools=list(SCOPED.values()), updated_by="admin", deps=deps)
    server = build_mcp_server(lambda: deps)

    async with _connected_client_session(server) as client:
        listed = await client.list_tools()

    by_name = {tool.name: tool for tool in listed.tools}
    assert set(by_name) == set(SCOPED.values())
    assert by_name[SCOPED["check_huggingface_models"]].input_schema["required"] == ["models"]
    assert "provider" in by_name[SCOPED["test_embedding_provider"]].input_schema["properties"]


@pytest.mark.asyncio
@respx.mock
async def test_check_huggingface_models_is_callable_over_mcp_by_an_admin(deps: ExposureDeps) -> None:
    respx.get(f"{HUB}/api/models/{MODEL}/auth-check").mock(return_value=httpx.Response(200))
    await update_exposure(enabled=True, allowed_tools=[SCOPED["check_huggingface_models"]], updated_by="admin", deps=deps)
    server = build_mcp_server(lambda: deps)

    actor_token = current_actor.set(_actor("admin"))
    try:
        async with _connected_client_session(server) as client:
            result = await client.call_tool(SCOPED["check_huggingface_models"], arguments=ARGUMENTS["check_huggingface_models"])
    finally:
        current_actor.reset(actor_token)

    assert result.is_error is False
    assert json.loads(result.content[0].text) == {
        "models": [{"name": MODEL, "ok": True, "status": "ok", "detail": "Can be pulled"}],
    }


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("name", PROBE_TOOLS)
async def test_a_user_role_caller_is_refused_in_band_and_nothing_is_asked(deps: ExposureDeps, name: str) -> None:
    """The dispatch gate reads each tool's declared ``required_role`` (admin); the handler never runs, so the Hub is not asked."""
    await update_exposure(enabled=True, allowed_tools=[SCOPED[name]], updated_by="admin", deps=deps)
    actor = _actor("user")

    result = await invoke_exposed(scoped_id=SCOPED[name], arguments=ARGUMENTS[name], principal=actor.id, actor=actor, deps=deps)

    assert result.is_error is True
    assert "requires" in result.output
    assert respx.calls.call_count == 0


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("name", PROBE_TOOLS)
async def test_an_admin_caller_clears_the_gate_on_every_probe_tool(deps: ExposureDeps, name: str) -> None:
    respx.get(f"{HUB}/api/models/{MODEL}/auth-check").mock(return_value=httpx.Response(200))
    await update_exposure(enabled=True, allowed_tools=[SCOPED[name]], updated_by="admin", deps=deps)
    actor = _actor("admin")

    result = await invoke_exposed(scoped_id=SCOPED[name], arguments=ARGUMENTS[name], principal=actor.id, actor=actor, deps=deps)

    assert result.is_error is False, result.output
    assert json.loads(result.output)  # an answer, not an error envelope
    assert respx.calls.call_count == 1
