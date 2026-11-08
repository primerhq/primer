"""Every exposable reserved tool must DECLARE a role: the fail-closed
'admin' default (primer.toolset.internal.InternalToolsetProvider.required_role)
is only a safety net for tools nobody has classified yet, never something
this codebase relies on for an actual authorization decision.

The fixture below builds the SAME built-in providers the MCP endpoint
enumerates: ``create_test_app`` wires the fixed built-in toolsets onto a real
``ProviderRegistry`` (the same construction ``primer/api/_app_lifespan.py``
performs) and compiles the generated per-resource route toolsets from the
mounted routers -- ``build_generated_route_toolsets`` +
``register_generated_api_toolset_ids`` + ``install_generated_api_toolsets``,
the same flow the production app build and the lifespan run. ``primer.mcp.exposure._iter_catalogue``
loops ``primer.api.registries.provider_registry.RESERVED_TOOLSET_IDS`` and
resolves each id via ``ProviderRegistry.get_toolset``. Resolving through that
union (rather than a hand-written id list) means a new reserved toolset,
fixed or generated, can't silently fall outside this test's coverage.
"""
from __future__ import annotations

import pytest

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.mcp.safety import is_exposable, tool_scoped_id


@pytest.fixture
def reserved_provider_registry(fake_storage_provider) -> ProviderRegistry:
    """A real ``ProviderRegistry`` carrying the fixed built-in toolsets and
    the generated per-resource route toolsets, wired production-shaped:
    ``create_test_app`` builds the fixed providers onto the registry (the
    lifespan's ``_<name>_toolset_provider`` wiring) and installs the
    generated providers compiled from the mounted routes into it."""
    registry = ProviderRegistry(
        fake_storage_provider,
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )
    # With a workspace registry, as the lifespan builds it: the tools that exist only then (read_doc_content,
    # reset_prime_instructions) are real tools and must be classified too.
    # The test app wires the canvas toolset with the settings row absent,
    # which defaults ``agent_tools_enabled`` on (canvas is on by default).
    create_test_app(
        storage_provider=fake_storage_provider,  # type: ignore[arg-type]
        provider_registry=registry,
    )
    return registry


@pytest.mark.asyncio
async def test_every_exposable_reserved_tool_declares_a_role(
    reserved_provider_registry: ProviderRegistry,
) -> None:
    missing: list[str] = []
    for toolset_id in RESERVED_TOOLSET_IDS:
        provider = await reserved_provider_registry.get_toolset(toolset_id)
        async for tool in provider.list_tools():
            ok, _reason = is_exposable(tool, provider=provider)
            if ok and tool.required_role is None:
                missing.append(tool_scoped_id(tool))
    assert not missing, (
        "exposable tools without an explicit required_role: "
        f"{sorted(missing)}"
    )


@pytest.mark.asyncio
async def test_call_tool_meta_dispatcher_never_exposable(
    reserved_provider_registry: ProviderRegistry,
) -> None:
    """Regression pin: ``system__call_tool`` must never pass ``is_exposable``.

    WHY THIS MATTERS (read before "fixing" this test): ``call_tool`` is
    role ``user`` (pivot P1), and its handler
    (``primer.toolset._system_crud._call_tool_tool``) dispatches the inner
    tool itself, NOT through ``primer.mcp.dispatch``'s ``invoke_exposed``
    path, which is where the MCP RBAC gate lives. The handler checks the
    INNER tool's ``required_role`` against the RUN's principal
    (``primer.agent.inner_dispatch``), read off the ``ToolContext``. The
    MCP endpoint hands handlers no context, so over MCP that check would
    rank every caller as an unattributed user rather than as the
    authenticated actor, and the approval gate and every pausing inner
    tool would have no session to park on.

    ``call_tool`` stays off MCP because it is declared ``yields=True`` (it
    parks for tool-approval, and a pausing inner tool pauses through it),
    which makes ``is_exposable`` reject it via the "yielding_unsupported"
    branch. If a future change drops ``yields=True`` (say, to make its
    non-approval-gated path exposable), this test starts failing. Do not
    just delete/relax this test to make it pass: first decide how an MCP
    call's actor reaches the inner role check and what a gated or pausing
    inner tool does with no park surface.
    """
    provider = await reserved_provider_registry.get_toolset("system")
    call_tool = None
    async for tool in provider.list_tools():
        if tool.id == "call_tool":
            call_tool = tool
            break
    assert call_tool is not None, "system toolset must still define call_tool"

    ok, reason = is_exposable(call_tool, provider=provider)

    assert ok is False, (
        "system__call_tool became exposable over MCP: its handler checks "
        "the inner tool's role against the run's context, which an MCP "
        "call does not carry; see this test's docstring before relaxing it"
    )
    assert reason == "yielding_unsupported"
