"""``list_exposed_tools`` + ``invoke_exposed`` contract — Spec §8.

These are the two service functions the MCP ``tools/list`` and
``tools/call`` handlers delegate to. The lowlevel SDK plumbing
(``Server.list_tools`` / ``Server.call_tool``) is exercised via the
in-memory transport in the Phase 7 e2e test; this file pins down the
business-logic contract that those handlers depend on.
"""

from __future__ import annotations

import logging

import pytest

from primer.mcp.dispatch import NotExposed, invoke_exposed, list_exposed_tools
from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.server import (
    build_mcp_server,
    current_api_token_id,
    current_principal,
)
from primer.model.chat import ToolCallResult


def _deps(storage, registry) -> ExposureDeps:
    return ExposureDeps(storage_provider=storage, provider_registry=registry)


# ---- list_exposed_tools ----------------------------------------------------


@pytest.mark.asyncio
async def test_list_disabled_returns_empty(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """When ``enabled=False`` the catalogue is empty — short-circuit before iter."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)

    out = await list_exposed_tools(deps)

    assert out == []


@pytest.mark.asyncio
async def test_list_returns_allowed_tools(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """Only allowlist members surface, with their owning provider attached."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    await update_exposure(
        enabled=True, allowed_tools=["misc__uuid_v4"],
        updated_by="alice", deps=deps,
    )

    out = await list_exposed_tools(deps)

    assert len(out) == 1
    tool, provider = out[0]
    assert tool.id == "uuid_v4"
    assert tool.toolset_id == "misc"
    # Provider plumbed through so the call layer doesn't re-resolve.
    assert provider.toolset_id == "misc"


@pytest.mark.asyncio
async def test_list_drops_unexposable_even_if_in_allowlist(
    fake_storage_provider, fake_provider_registry_with_tools,
    fake_misc_tools, monkeypatch,
) -> None:
    """``is_exposable`` veto wins over the operator allowlist (defence in depth)."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    # Allow uuid_v4 first (validation passes — provider is clean), THEN
    # flip the provider's yielding flag so the live check denies it on
    # the read path. This proves the live filter runs.
    await update_exposure(
        enabled=True, allowed_tools=["misc__uuid_v4"],
        updated_by="alice", deps=deps,
    )
    provider = await fake_provider_registry_with_tools.get_toolset("misc")
    provider._yielding.add("uuid_v4")

    out = await list_exposed_tools(deps)

    assert out == []


# ---- invoke_exposed --------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_disabled_raises_not_exposed(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """Endpoint disabled → NotExposed with ``not_in_allowlist`` reason."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)

    with pytest.raises(NotExposed) as excinfo:
        await invoke_exposed(
            scoped_id="misc__uuid_v4", arguments={},
            principal=None, deps=deps,
        )
    assert excinfo.value.reason == "not_in_allowlist"


@pytest.mark.asyncio
async def test_invoke_disallowed_raises_not_exposed(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """A scoped id outside the allowlist raises before any provider work."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    await update_exposure(
        enabled=True, allowed_tools=[], updated_by="x", deps=deps,
    )

    with pytest.raises(NotExposed) as excinfo:
        await invoke_exposed(
            scoped_id="misc__uuid_v4", arguments={},
            principal=None, deps=deps,
        )
    assert excinfo.value.reason == "not_in_allowlist"


@pytest.mark.asyncio
async def test_invoke_malformed_id_raises_not_exposed(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """A scoped id without ``__`` separator is rejected as malformed."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    # We can't put a malformed id through update_exposure (validation
    # would reject it), so seed storage directly with a forged allowlist.
    from primer.model.mcp_exposure import McpExposure
    from datetime import datetime, timezone
    storage = fake_storage_provider.get_storage(McpExposure)
    await storage.create(McpExposure(
        enabled=True,
        allowed_tools=["no_separator_here"],
        updated_at=datetime.now(timezone.utc),
    ))

    with pytest.raises(NotExposed) as excinfo:
        await invoke_exposed(
            scoped_id="no_separator_here", arguments={},
            principal=None, deps=deps,
        )
    assert excinfo.value.reason == "malformed_id"


@pytest.mark.asyncio
async def test_invoke_missing_provider_raises_not_exposed(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """Allowlisted toolset id with no live provider → ``provider_missing``."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    from primer.model.mcp_exposure import McpExposure
    from datetime import datetime, timezone
    storage = fake_storage_provider.get_storage(McpExposure)
    await storage.create(McpExposure(
        enabled=True,
        allowed_tools=["ghost__noop"],
        updated_at=datetime.now(timezone.utc),
    ))

    with pytest.raises(NotExposed) as excinfo:
        await invoke_exposed(
            scoped_id="ghost__noop", arguments={},
            principal=None, deps=deps,
        )
    assert excinfo.value.reason == "provider_missing"


@pytest.mark.asyncio
async def test_invoke_missing_tool_raises_not_exposed(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """Live provider that no longer emits the tool → ``tool_missing``."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    from primer.model.mcp_exposure import McpExposure
    from datetime import datetime, timezone
    storage = fake_storage_provider.get_storage(McpExposure)
    await storage.create(McpExposure(
        enabled=True,
        allowed_tools=["misc__vanished"],
        updated_at=datetime.now(timezone.utc),
    ))

    with pytest.raises(NotExposed) as excinfo:
        await invoke_exposed(
            scoped_id="misc__vanished", arguments={},
            principal=None, deps=deps,
        )
    assert excinfo.value.reason == "tool_missing"


@pytest.mark.asyncio
async def test_invoke_unexposable_raises_not_exposed(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """Live ``is_exposable`` veto on call path → ``yielding_unsupported``."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    await update_exposure(
        enabled=True, allowed_tools=["misc__uuid_v4"],
        updated_by="alice", deps=deps,
    )
    # Flip the flag AFTER PUT so we can prove the dispatch-time check runs.
    provider = await fake_provider_registry_with_tools.get_toolset("misc")
    provider._yielding.add("uuid_v4")

    with pytest.raises(NotExposed) as excinfo:
        await invoke_exposed(
            scoped_id="misc__uuid_v4", arguments={},
            principal=None, deps=deps,
        )
    assert excinfo.value.reason == "yielding_unsupported"


@pytest.mark.asyncio
async def test_invoke_allowed_returns_result(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """Happy path: provider's ``call`` runs with bare name + principal."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    await update_exposure(
        enabled=True, allowed_tools=["misc__uuid_v4"],
        updated_by="alice", deps=deps,
    )

    result = await invoke_exposed(
        scoped_id="misc__uuid_v4", arguments={"foo": 1},
        principal="user@example.com", deps=deps,
    )

    assert isinstance(result, ToolCallResult)
    assert result.is_error is False
    provider = await fake_provider_registry_with_tools.get_toolset("misc")
    assert provider.calls == [{
        "tool_name": "uuid_v4",
        "arguments": {"foo": 1},
        "principal": "user@example.com",
        "ctx": None,
    }]


# ---- build_mcp_server smoke ------------------------------------------------


@pytest.mark.asyncio
async def test_build_mcp_server_registers_handlers(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """The returned Server has list_tools + call_tool handlers installed."""
    from mcp.types import CallToolRequest, ListToolsRequest

    def factory() -> ExposureDeps:
        return _deps(fake_storage_provider, fake_provider_registry_with_tools)

    server = build_mcp_server(factory)

    # The SDK keys its request dispatcher by request-type; presence of
    # both keys proves the decorators registered the handlers.
    assert ListToolsRequest in server.request_handlers
    assert CallToolRequest in server.request_handlers


@pytest.mark.asyncio
async def test_audit_log_records_invoke(
    fake_storage_provider, fake_provider_registry_with_tools, caplog,
) -> None:
    """``log_invoke`` lands on the ``primer.mcp.audit`` logger with extras."""
    from primer.mcp.audit import log_invoke

    caplog.set_level(logging.INFO, logger="primer.mcp.audit")
    log_invoke(
        principal="alice", api_token_id="tok_1",
        scoped_id="misc__uuid_v4", ok=True, duration_ms=12.3456,
    )

    records = [r for r in caplog.records if r.name == "primer.mcp.audit"]
    assert len(records) == 1
    rec = records[0]
    assert rec.message == "mcp.invoke"
    assert rec.principal == "alice"
    assert rec.api_token_id == "tok_1"
    assert rec.scoped_id == "misc__uuid_v4"
    assert rec.ok is True
    assert rec.duration_ms == 12.35
    assert rec.error_code is None


@pytest.mark.asyncio
async def test_context_vars_default_to_none() -> None:
    """Defaults let unit tests + dev REPL call handlers without auth wiring."""
    assert current_principal.get() is None
    assert current_api_token_id.get() is None
