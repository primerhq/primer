"""End-to-end SDK e2e — Spec §14.

Drives the lowlevel :class:`Server` returned by
:func:`primer.mcp.server.build_mcp_server` through the MCP SDK's
in-memory transport. This exercises the full request/response loop —
JSON-RPC framing, ``initialize`` handshake, ``tools/list`` +
``tools/call`` dispatch — without spinning up an HTTP server.

The unit-level handler tests in ``test_server_handlers.py`` already
pin down the dispatch + audit + error-mapping contract; this file
proves the SDK glue is correct: handler return shapes round-trip
through the protocol, the allowlist filters ``tools/list``, and
calling a disallowed tool surfaces as a JSON-RPC error (mapped to
``McpError`` on the client) rather than a successful response.

The ``create_connected_server_and_client_session`` helper wires
client + server with a pair of in-memory anyio streams and runs the
server in the helper's task group; the test body sees a ready-to-use
``ClientSession`` with ``initialize()`` already called.
"""

from __future__ import annotations

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.server import build_mcp_server, current_actor
from primer.model.principal import Principal


def _deps(storage, registry) -> ExposureDeps:
    return ExposureDeps(storage_provider=storage, provider_registry=registry)


_SAFE = "misc__uuid_v4"


@pytest.mark.asyncio
async def test_list_returns_only_allowed(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """``tools/list`` returns only tools in ``McpExposure.allowed_tools``.

    Two safe tools exist in the fake registry (``uuid_v4`` + ``now``).
    Allowlisting only ``uuid_v4`` must filter ``now`` from the wire
    response — proving the dispatch layer's allowlist check is honoured
    end-to-end and not bypassed by the SDK.
    """
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    await update_exposure(
        enabled=True, allowed_tools=[_SAFE],
        updated_by="alice", deps=deps,
    )
    server = build_mcp_server(lambda: deps)

    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()

    names = {t.name for t in result.tools}
    assert names == {_SAFE}


@pytest.mark.asyncio
async def test_call_allowed_returns_result(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """``tools/call`` on an allowed tool returns the provider's output.

    The fake provider's default handler echoes ``f"{name}:{arguments}"``
    so the round-trip can be asserted exactly. ``isError`` must be
    ``False`` and the first content block must carry the echo.

    Sets ``current_actor`` to a system-type Principal (the auth-disabled
    bypass) before the server task is spawned so it clears the Task 3
    ``required_role`` RBAC gate — this test is about the SDK round-trip,
    not RBAC.
    """
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    await update_exposure(
        enabled=True, allowed_tools=[_SAFE],
        updated_by="alice", deps=deps,
    )
    server = build_mcp_server(lambda: deps)

    actor_tok = current_actor.set(
        Principal(
            type="system", id="test-system", display="test-system",
            role=None, source="system",
        )
    )
    try:
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool(_SAFE, arguments={"foo": "bar"})
    finally:
        current_actor.reset(actor_tok)

    assert result.isError is False
    assert result.content, "expected at least one content block"
    first = result.content[0]
    assert first.type == "text"
    # Echo from FakeToolsetProvider.call default handler.
    assert "uuid_v4" in first.text
    assert "foo" in first.text


@pytest.mark.asyncio
async def test_call_disallowed_returns_not_exposed(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """Calling outside the allowlist surfaces as a JSON-RPC error.

    The server raises :class:`McpError` with code ``METHOD_NOT_FOUND``;
    the SDK's client-side dispatch either re-raises it as
    :class:`McpError` or returns ``isError=True``. Both are valid
    failure shapes per the SDK contract — accept either, and confirm the
    response is *not* a successful tool result.
    """
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    # Enabled but nothing exposed — the safe tool is denied.
    await update_exposure(
        enabled=True, allowed_tools=[],
        updated_by="x", deps=deps,
    )
    server = build_mcp_server(lambda: deps)

    async with create_connected_server_and_client_session(server) as client:
        try:
            result = await client.call_tool(_SAFE, arguments={})
        except Exception as exc:  # noqa: BLE001 -- protocol error path
            msg = str(exc).lower()
            assert (
                "not exposed" in msg
                or "method" in msg
                or "not_exposed" in msg
            ), f"unexpected error shape: {exc!r}"
            return
        # Some SDK versions surface protocol errors as isError=True
        # rather than raising — accept that too.
        assert result.isError is True


@pytest.mark.asyncio
async def test_disabled_exposure_returns_empty_list(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """A disabled exposure makes ``tools/list`` return zero tools.

    The exposure singleton is lazy-created with ``enabled=False`` on
    first read, so the test skips the explicit ``update_exposure`` to
    prove the default state is safe.
    """
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)
    server = build_mcp_server(lambda: deps)

    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()

    assert result.tools == []
